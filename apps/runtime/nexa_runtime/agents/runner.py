"""The in-process run worker (docs/architecture/agents.md#workers).

Runtime has no Celery (that is Core's, and it has no AI Gateway), so runs execute in Runtime
itself: each process starts `AGENTS_WORKER_CONCURRENCY` worker loops that claim queued runs
from PostgreSQL with `FOR UPDATE SKIP LOCKED` and hold a lease while they execute. The lease
is renewed every few seconds; a run whose lease expired belongs to a worker that died and is
claimed again by any worker, which resumes it from its recorded steps (the engine settles
whatever that worker left open). Several Runtime processes share the queue safely; no run is
ever executed by two workers at once.

A submission in this process wakes the loops at once; otherwise they poll every
`AGENTS_POLL_INTERVAL_SECONDS`. On shutdown, running steps get a grace period to reach a
checkpoint, then the runs go back to the queue.
"""

import asyncio
import contextlib
import os
import socket
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_common.logs import get_logger
from nexa_runtime.agents import repository as repo
from nexa_runtime.agents.engine import AgentEngine
from nexa_runtime.config import RuntimeSettings

log = get_logger("nexa.runtime.agents.runner")

_SWEEP_SECONDS = 60.0


class AgentRunner:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        agent_engine: AgentEngine,
        settings: RuntimeSettings,
        worker_id: str | None = None,
    ) -> None:
        self._engine = engine
        self._agent_engine = agent_engine
        self._settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"
        self._shutdown = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        self._shutdown = asyncio.Event()
        for index in range(self._settings.agents_worker_concurrency):
            self._tasks.append(asyncio.create_task(self._loop(index), name=f"agent-worker-{index}"))
        self._tasks.append(asyncio.create_task(self._sweep(), name="agent-sweep"))
        log.info(
            "agents.runner.started",
            worker=self.worker_id,
            concurrency=self._settings.agents_worker_concurrency,
        )

    async def stop(self) -> None:
        """Stop taking runs; let running steps reach a checkpoint (the engine then puts the
        run back in the queue), then cancel what is left."""
        if not self.started:
            return
        self._shutdown.set()
        self._wake.set()
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            _, still = await asyncio.wait(
                pending, timeout=self._settings.agents_shutdown_grace_seconds
            )
            for task in still:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks = []
        self.started = False
        log.info("agents.runner.stopped", worker=self.worker_id)

    def wake(self) -> None:
        """A run was queued in this process: do not wait for the next poll."""
        self._wake.set()

    async def run_once(self) -> bool:
        """Claim and execute one run, if any (tests and scripts). Returns whether one ran."""
        run = await self._claim()
        if run is None:
            return False
        await self._execute(run)
        return True

    async def _loop(self, index: int) -> None:
        while not self._shutdown.is_set():
            try:
                run = await self._claim()
            except Exception as exc:
                log.error("agents.runner.claim_failed", error=type(exc).__name__)
                await asyncio.sleep(self._settings.agents_poll_interval_seconds * 5)
                continue
            if run is None:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self._settings.agents_poll_interval_seconds
                    )
                continue
            await self._execute(run)

    async def _claim(self) -> repo.RunRow | None:
        async with self._engine.begin() as conn:
            return await repo.claim_next(
                conn, worker=self.worker_id, lease_seconds=self._settings.agents_lease_seconds
            )

    async def _execute(self, run: repo.RunRow) -> None:
        queued_for = (repo.now() - run.updated_at).total_seconds()
        log.info(
            "agents.runner.claimed",
            run_id=str(run.id),
            status=run.status,
            attempt=run.attempts,
            queue_delay_ms=max(0, int(queued_for * 1000)),
        )
        try:
            outcome = await self._agent_engine.execute(
                run, worker=self.worker_id, shutdown=self._shutdown
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the engine reports its own failures; this is a bug
            log.error("agents.runner.bug", run_id=str(run.id), error=type(exc).__name__)
            return
        log.info("agents.runner.finished", run_id=str(run.id), outcome=outcome.status)

    async def _sweep(self) -> None:
        """Runs left waiting for an approval that expired are closed."""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=_SWEEP_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                await self.expire_waiting_runs()
            except Exception as exc:
                log.error("agents.runner.sweep_failed", error=type(exc).__name__)

    async def expire_waiting_runs(self) -> int:
        """End the runs whose pending approval expired."""
        stamp = repo.now()
        stale = stamp - timedelta(seconds=self._settings.agents_approval_ttl_seconds)
        ended = []
        async with self._engine.begin() as conn:
            for run in await repo.expired_waiting(conn, at=stamp, stale_before=stale):
                row = await repo.fail_waiting(conn, run.id)
                if row is not None:
                    ended.append(row)
        for row in ended:
            await self._agent_engine.audit.record(
                "AGENT_RUN_FAILED",
                organization_id=row.organization_id,
                actor_id=row.user_id,
                resource_type="agent_run",
                resource_id=str(row.id),
                request_id=row.request_id or None,
                metadata={
                    "agent_id": str(row.agent_id),
                    "status": "failed",
                    "failure_reason": "approval_expired",
                },
            )
        if ended:
            log.info("agents.runner.approvals_expired", count=len(ended))
        return len(ended)

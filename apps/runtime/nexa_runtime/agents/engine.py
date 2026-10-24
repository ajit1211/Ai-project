"""The agent execution engine: one bounded decision loop per run
(docs/architecture/agents.md#execution).

    prepare   re-read the user's permissions, load the run's configuration snapshot, offer
              the agent's tools (registry ∩ snapshot ∩ switches ∩ permissions)
    settle    finish what an earlier worker left half-done (recovery, an approval's outcome)
    loop      checkpoint (cancel, pause, deadline, token and step budgets)
              ─► ask the model for one decision (structured output, validated)
              ─► record the decision step
              ─► act: run a tool through the executor | note progress | complete | fail
              ─► record the observation; next

Every step is written before the next one starts, and the model's context is rebuilt from
the recorded steps, so another worker can continue a run where this one stopped. The engine
never trusts the model: tools are resolved, authorized, validated and run by the tool
executor; outputs are validated against the agent's schema; limits are enforced by code.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog
from jsonschema import Draft202012Validator
from referencing import Registry as SchemaRegistry
from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_ai.context import AIRequestContext
from nexa_ai.cost import usd_to_micros
from nexa_ai.errors import (
    AIContextTooLarge,
    AIError,
    AIInvalidRequest,
    AIModelUnavailable,
    AIOutputInvalid,
    AIQuotaExceeded,
)
from nexa_ai.gateway import Gateway
from nexa_ai.structured import check_schema
from nexa_ai.types import (
    ChatRequest,
    ChatResponse,
    Feature,
    JSONSchemaFormat,
    ToolCallPart,
    Usage,
)
from nexa_common.access import Principal
from nexa_common.errors import PermissionDenied
from nexa_common.logs import get_logger
from nexa_runtime.agents import repository as repo
from nexa_runtime.agents.context import (
    build_context,
    goal_message,
    history_messages,
    system_message,
)
from nexa_runtime.agents.decisions import (
    DECISION_SCHEMA,
    Decision,
    InvalidDecision,
    parse_decision,
)
from nexa_runtime.auth.dependencies import reload_principal
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.tools import repository as tools_repo
from nexa_runtime.tools.audit import ToolAudit
from nexa_runtime.tools.executor import (
    Finished,
    ToolExecutor,
    Turn,
    finished_from_row,
    planned_from_row,
)
from nexa_runtime.tools.registry import ToolDefinition
from nexa_runtime.tools.validation import (
    ArgumentsRejected,
    arguments_hash,
    redacted,
    validate_arguments,
)

log = get_logger("nexa.runtime.agents")

REQUIRED_PERMISSIONS = ("ai.chat", "agents.execute", "ai.agent.execute")
DECISION_FORMAT = "agent_decision"
_CITATION = re.compile(r"\[(R\d{1,3})\]")
_OFFLINE: SchemaRegistry[Any] = SchemaRegistry()
_FALLBACK_CONTEXT_WINDOW = 100_000


@dataclass(frozen=True, slots=True)
class Outcome:
    """How one execution of a run ended on this worker."""

    status: str  # completed | failed | timed_out | cancelled | paused | waiting_for_approval | queued | lost
    reason: str = ""


class _RunFailed(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class _InvalidOutput(InvalidDecision):
    """The model's answer did not match the decision schema even after the gateway's repair;
    the call was metered all the same."""

    def __init__(self, reason: str, usage: Usage | None, call_id: UUID | None) -> None:
        super().__init__(reason)
        self.usage = usage
        self.call_id = call_id


@dataclass
class _Run:
    row: Any
    worker: str
    snapshot: dict[str, Any]
    principal: Principal | None = None
    offered: dict[str, ToolDefinition] = field(default_factory=dict)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: bool = False
    pause: bool = False
    lost: bool = False
    shutdown: bool = False
    # ref -> {ref, document_id, chunk_id, title}: the passages the answer may cite
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    identical: dict[str, int] = field(default_factory=dict)
    invalid_streak: int = 0
    started: float = field(default_factory=time.perf_counter)

    @property
    def id(self) -> UUID:
        run_id: UUID = self.row.id
        return run_id

    def limit(self, name: str) -> int:
        return int(self.snapshot["limits"][name])


class AgentEngine:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        gateway: Gateway,
        tools: ToolExecutor,
        settings: RuntimeSettings,
        audit: ToolAudit,
    ) -> None:
        self._engine = engine
        self._gateway = gateway
        self._tools = tools
        self._settings = settings
        self._audit = audit

    @property
    def audit(self) -> ToolAudit:
        return self._audit

    # --- Entry -------------------------------------------------------------------------------

    async def execute(self, row: Any, *, worker: str, shutdown: asyncio.Event) -> Outcome:
        """Run `row` (claimed by `worker`) until it ends or has to wait. Never raises for a
        run's own failure; a CancelledError (the worker is stopping) puts it back in the
        queue."""
        state = _Run(row=row, worker=worker, snapshot=dict(row.agent_snapshot or {}))
        with structlog.contextvars.bound_contextvars(
            organization_id=str(row.organization_id),
            user_id=str(row.user_id),
            request_id=row.request_id or None,
            run_id=str(row.id),
        ):
            return await self._execute(state, shutdown)

    async def _execute(self, state: _Run, shutdown: asyncio.Event) -> Outcome:
        try:
            prepared = await self._prepare(state)
        except _RunFailed as failed:
            return await self._finish(state, "failed", failed.reason, failed.message)
        except asyncio.CancelledError:
            await asyncio.shield(self._release(state, "queued"))
            raise
        except Exception as exc:
            # Never leave a claimed run behind: it would be claimed and fail again forever.
            log.error("agents.run.bug", run_id=str(state.id), error=type(exc).__name__)
            return await self._finish(
                state, "failed", "internal_error", "The agent stopped because of an internal error."
            )
        if prepared is not None:
            return prepared
        heartbeat = asyncio.create_task(self._heartbeat(state))
        watch = asyncio.create_task(self._watch(state, shutdown))
        try:
            outcome = await self._settle_pending(state)
            while outcome is None:
                outcome = await self._checkpoint(state)
                if outcome is None:
                    outcome = await self._step(state)
            return outcome
        except _RunFailed as failed:
            return await self._finish(state, "failed", failed.reason, failed.message)
        except asyncio.CancelledError:
            await asyncio.shield(self._release(state, "queued"))
            raise
        except Exception as exc:
            log.error("agents.run.bug", run_id=str(state.id), error=type(exc).__name__)
            return await self._finish(
                state, "failed", "internal_error", "The agent stopped because of an internal error."
            )
        finally:
            heartbeat.cancel()
            watch.cancel()
            await asyncio.gather(heartbeat, watch, return_exceptions=True)

    # --- Preparation ---------------------------------------------------------------------------

    async def _prepare(self, state: _Run) -> Outcome | None:
        row = state.row
        snapshot = state.snapshot
        if not snapshot or "limits" not in snapshot:
            return await self._finish(
                state, "failed", "invalid_configuration", "The run has no agent configuration."
            )
        try:
            async with self._engine.connect() as conn:
                fresh = await reload_principal(
                    conn,
                    Principal(
                        user_id=row.user_id, session_id=None, organization_id=row.organization_id
                    ),
                )
        except PermissionDenied:
            fresh = None
        if fresh is None or not all(fresh.has(p) for p in REQUIRED_PERMISSIONS):
            return await self._finish(
                state,
                "failed",
                "permission_denied",
                "The user may no longer run agents in this organization.",
            )
        if row.deadline_at is not None and repo.now() >= row.deadline_at:
            # Also ends a run whose every attempt crashes its worker: the clock runs meanwhile.
            return await self._finish(state, "timed_out", "timeout", "The agent ran out of time.")
        principal = fresh.for_agent(row.agent_id, fresh.permissions)
        state.principal = principal
        ctx = self._context(state)
        try:
            capabilities = await self._gateway.model_capabilities(
                ctx, snapshot.get("model") or None
            )
        except AIError as exc:
            raise _RunFailed("model_unavailable", exc.message) from exc
        offered = await self._tools.offer(
            principal,
            capabilities,
            scope="agent",
            allowed=list(snapshot.get("allowed_tools") or []),
            require_native=False,
        )
        state.offered = {d.id: d for d in offered}
        stamp = repo.now()
        values: dict[str, Any] = {}
        if row.status == "queued":
            values["status"] = "running"
        if row.started_at is None:
            values["started_at"] = stamp
            values["deadline_at"] = stamp + timedelta(seconds=state.limit("max_duration_seconds"))
        if values:
            async with self._engine.begin() as conn:
                updated = await repo.transition(
                    conn,
                    row.id,
                    from_states=("queued", "running", "cancel_requested"),
                    worker=state.worker,
                    **values,
                )
            if updated is None:
                return Outcome("lost")
            state.row = updated
        log.info(
            "agents.run.started",
            run_id=str(row.id),
            agent_id=str(row.agent_id),
            agent_version=row.agent_version,
            attempt=row.attempts,
            tools=sorted(state.offered),
            worker=state.worker,
        )
        return None

    def _context(self, state: _Run) -> AIRequestContext:
        assert state.principal is not None  # noqa: S101 - set by _prepare
        return replace(
            AIRequestContext.from_principal(
                state.principal, request_id=state.row.request_id or None
            ),
            source_type="agent_run",
            source_id=state.id,
        )

    # --- Control -------------------------------------------------------------------------------

    async def _heartbeat(self, state: _Run) -> None:
        interval = min(
            self._settings.agents_lease_seconds / 3, self._settings.agents_control_poll_seconds
        )
        while True:
            await asyncio.sleep(interval)
            try:
                await self._refresh(state)
            except Exception as exc:  # the database blinked: the lease still has time
                log.warning(
                    "agents.run.heartbeat_failed", run_id=str(state.id), error=type(exc).__name__
                )
            if state.stop.is_set():
                return

    async def _watch(self, state: _Run, shutdown: asyncio.Event) -> None:
        await shutdown.wait()
        state.shutdown = True
        state.stop.set()

    async def _refresh(self, state: _Run) -> None:
        """Renew the lease and read the control flags; the stop event wakes a running call."""
        async with self._engine.begin() as conn:
            row = await repo.renew_lease(
                conn,
                state.id,
                worker=state.worker,
                lease_seconds=self._settings.agents_lease_seconds,
            )
        if row is None:
            state.lost = True
            state.stop.set()
            return
        if row.cancel_requested or row.status == "cancel_requested":
            state.cancel = True
            state.stop.set()
        if row.pause_requested:
            state.pause = True
        if row.deadline_at is not None and repo.now() >= row.deadline_at:
            state.stop.set()
        state.row = row

    async def _checkpoint(self, state: _Run) -> Outcome | None:
        await self._refresh(state)
        row = state.row
        if state.lost:
            return Outcome("lost")
        if state.cancel:
            return await self._finish(state, "cancelled", "cancelled", "Cancelled by the user.")
        if state.shutdown:
            return await self._release(state, "queued")
        if row.deadline_at is not None and repo.now() >= row.deadline_at:
            return await self._finish(state, "timed_out", "timeout", "The agent ran out of time.")
        if state.pause:
            return await self._release(
                state, "paused", pause_requested=False, audit="AGENT_RUN_PAUSED"
            )
        if row.input_tokens + row.output_tokens >= state.limit("max_tokens"):
            return await self._finish(
                state, "failed", "token_budget", "The agent used up its token budget."
            )
        if row.step_count >= state.limit("max_steps"):
            return await self._finish(
                state, "failed", "step_limit", "The agent did not finish within its step limit."
            )
        return None

    # --- One step ------------------------------------------------------------------------------

    async def _step(self, state: _Run) -> Outcome | None:
        row = state.row
        snapshot = state.snapshot
        steps_left = state.limit("max_steps") - row.step_count
        tool_calls_left = max(0, state.limit("max_tool_calls") - row.tool_call_count)
        forced = steps_left <= 1
        async with self._engine.connect() as conn:
            steps = await repo.steps_of(conn, row.id)
        system = system_message(
            snapshot.get("instructions", ""),
            list(state.offered.values()),
            steps_left=steps_left,
            tool_calls_left=tool_calls_left,
            forced=forced,
            output_schema=snapshot.get("output_schema"),
        )
        goal = goal_message(row.goal, dict(row.input or {}))
        messages, dropped = build_context(
            system, goal, history_messages(steps), max_tokens=await self._context_budget(state)
        )
        request = ChatRequest(
            model=snapshot.get("model") or None,
            messages=messages,
            response_format=JSONSchemaFormat(name=DECISION_FORMAT, json_schema=DECISION_SCHEMA),
            max_output_tokens=self._settings.agents_max_output_tokens,
            feature=Feature.AGENT,
        )
        started = time.perf_counter()
        begun = repo.now()
        try:
            response = await self._ask(state, request)
        except _InvalidOutput as invalid:
            return await self._invalid(
                state, invalid.reason, started, usage=invalid.usage, usage_event_id=invalid.call_id
            )
        if response is None:
            return None  # stopped: the checkpoint says why
        try:
            decision = parse_decision(response.parsed)
        except InvalidDecision as invalid:
            return await self._invalid(
                state,
                invalid.reason,
                started,
                usage=response.usage,
                usage_event_id=UUID(response.id),
                model=response.model,
            )
        if decision.action != "complete":
            state.invalid_streak = 0  # a 'complete' counts once its output passed (_complete)
        summary = decision.summary or _default_summary(decision)
        traced = _traced(decision, state.offered.get(decision.tool))
        async with self._engine.begin() as conn:
            step = await repo.add_step(
                conn,
                organization_id=row.organization_id,
                run_id=row.id,
                kind="decision",
                status="succeeded",
                summary=summary,
                detail={"action": decision.action, "decision": traced, "context_dropped": dropped},
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                usage_event_id=UUID(response.id),
                started_at=begun,
                duration_ms=_ms(started),
            )
            await self._meter(
                conn, state, response.usage, steps=1, model_calls=1, model=response.model
            )
        log.info(
            "agents.run.decision",
            run_id=str(row.id),
            step=step.sequence,
            action=decision.action,
            tool=decision.tool or None,
            latency_ms=_ms(started),
        )
        if decision.action in ("call_tool", "request_approval"):
            if forced:
                raise _RunFailed("step_limit", "The agent did not finish within its step limit.")
            return await self._call_tool(state, decision, step.sequence)
        if decision.action == "report_progress":
            async with self._engine.begin() as conn:
                await repo.add_step(
                    conn,
                    organization_id=row.organization_id,
                    run_id=row.id,
                    kind="progress",
                    summary=decision.message[:300],
                    detail={
                        "message": decision.message,
                        "observation": "Progress noted. Continue with the task.",
                    },
                )
                await repo.update_run(conn, row.id, progress_message=decision.message[:300])
            return None
        if decision.action == "complete":
            return await self._complete(state, decision)
        async with self._engine.begin() as conn:
            await repo.add_step(
                conn,
                organization_id=row.organization_id,
                run_id=row.id,
                kind="error",
                status="failed",
                summary="The agent gave up",
                detail={"reason": decision.reason},
                error_code="agent_reported",
            )
        return await self._finish(state, "failed", "agent_reported", decision.reason)

    async def _ask(self, state: _Run, request: ChatRequest) -> ChatResponse | None:
        """One model call, retried once after a transient provider failure; None when a stop
        arrived meanwhile. Raises _InvalidOutput when the answer did not match the schema even
        after the gateway's repair, and _RunFailed for everything that ends the run. Every call
        the gateway metered is added to the run, whatever its outcome (a call interrupted by
        a stop is the exception: the gateway records it as cancelled)."""
        ctx = self._context(state)
        attempts = self._settings.agents_step_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await _unless_stopped(self._gateway.chat(request, ctx), state.stop)
            except AIOutputInvalid as exc:
                raise _InvalidOutput(exc.message, exc.usage, exc.call_id) from exc
            except AIQuotaExceeded as exc:
                await self._meter_failed(state, exc)
                raise _RunFailed("quota_exceeded", exc.message) from exc
            except AIContextTooLarge as exc:
                await self._meter_failed(state, exc)
                raise _RunFailed("context_too_large", exc.message) from exc
            except AIModelUnavailable as exc:
                # The agent's model was switched off or withdrawn from the organization.
                await self._meter_failed(state, exc)
                raise _RunFailed("model_unavailable", exc.message) from exc
            except PermissionDenied as exc:
                # AI was switched off for the organization (or the user lost ai.chat) meanwhile.
                raise _RunFailed("permission_denied", exc.message) from exc
            except AIError as exc:
                await self._meter_failed(state, exc)
                transient = exc.retryable or exc.fallback_eligible
                log.warning(
                    "agents.run.model_failed",
                    run_id=str(state.id),
                    attempt=attempt,
                    transient=transient,
                    **exc.log_fields(),
                )
                if transient and attempt < attempts and not state.stop.is_set():
                    await asyncio.sleep(min(2.0 * attempt, 10.0))
                    continue
                raise _RunFailed("provider_error", exc.message) from exc
        raise _RunFailed("provider_error", "The model could not be reached.")  # pragma: no cover

    async def _invalid(
        self,
        state: _Run,
        reason: str,
        started: float,
        *,
        usage: Usage | None = None,
        usage_event_id: UUID | None = None,
        model: str | None = None,
        steps: int = 1,
    ) -> Outcome | None:
        """The model's decision was not valid: tell it, count it, and give up after two in a
        row (`validation_failed`). `steps` is 0 when the decision was already counted (a
        'complete' whose output failed the schema)."""
        state.invalid_streak += 1
        row = state.row
        observation = json.dumps(
            {
                "status": "error",
                "error": {
                    "code": "invalid_decision",
                    "message": f"Your previous decision was not valid: {reason} "
                    "Answer with exactly one valid decision object.",
                },
            }
        )
        async with self._engine.begin() as conn:
            await repo.add_step(
                conn,
                organization_id=row.organization_id,
                run_id=row.id,
                kind="error",
                status="failed",
                summary="The decision was not valid",
                detail={"reason": reason[:500], "observation": observation},
                error_code="invalid_decision",
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                usage_event_id=usage_event_id,
                duration_ms=_ms(started),
            )
            await self._meter(
                conn,
                state,
                usage,
                steps=steps,
                model_calls=1 if usage_event_id else 0,
                model=model,
            )
        if state.invalid_streak >= 2:
            raise _RunFailed(
                "validation_failed", "The agent kept answering with invalid decisions."
            )
        return None

    async def _context_budget(self, state: _Run) -> int:
        registry = await self._gateway.catalog.registry()
        model = state.snapshot.get("model") or None
        alias = registry.get(model) if model else registry.default_alias
        window = (
            alias.context_window if alias and alias.context_window else _FALLBACK_CONTEXT_WINDOW
        )
        return max(
            4_000,
            min(
                self._settings.agents_context_max_tokens,
                window - self._settings.agents_max_output_tokens - 1_000,
            ),
        )

    async def _meter(
        self,
        conn: Any,
        state: _Run,
        usage: Usage | None,
        *,
        steps: int,
        model_calls: int,
        model: str | None = None,
    ) -> None:
        """Add measured usage (and the steps it produced) to the run's counters."""
        cost = usd_to_micros(usage.cost_usd) if usage and usage.cost_usd else None
        updated = await repo.add_usage(
            conn,
            state.id,
            steps=steps,
            model_calls=model_calls,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cost_micros=cost,
            model=model,
        )
        if updated is not None:
            state.row = updated

    async def _meter_failed(self, state: _Run, exc: AIError) -> None:
        """A call that failed after the gateway metered it (it has a usage row)."""
        if exc.call_id is None:
            return  # refused before any provider was called: nothing was used
        async with self._engine.begin() as conn:
            await self._meter(conn, state, exc.usage, steps=0, model_calls=1)

    # --- Tools ---------------------------------------------------------------------------------

    async def _call_tool(self, state: _Run, decision: Decision, step_index: int) -> Outcome | None:
        row = state.row
        if row.tool_call_count >= state.limit("max_tool_calls"):
            await self._refuse_call(
                state,
                decision,
                "tool_call_limit",
                "No tool calls are left for this task; answer with complete or fail.",
            )
            return None
        definition = state.offered.get(decision.tool)
        key = _call_key(definition, decision) if definition is not None else f"?|{decision.tool}"
        repeats = state.identical.get(key, 0)
        if definition is not None and repeats >= self._settings.agents_identical_call_limit:
            if repeats > self._settings.agents_identical_call_limit:
                raise _RunFailed("loop_detected", "The agent kept repeating the same tool call.")
            state.identical[key] = repeats + 1
            await self._refuse_call(
                state,
                decision,
                "repeated_call",
                "You already made this exact call; its result is above. Do something different "
                "or finish.",
            )
            return None
        state.identical[key] = repeats + 1
        assert state.principal is not None  # noqa: S101 - set by _prepare
        turn = Turn(
            principal=state.principal,
            request_id=row.request_id or None,
            offered=state.offered,
            run_id=row.id,
            # The model may ask for an approval the policy would not require; never the reverse.
            approve_all=state.snapshot.get("approval_policy") == "all_tool_calls"
            or decision.action == "request_approval",
            approval_ttl_seconds=self._settings.agents_approval_ttl_seconds,
        )
        summary = (
            definition.summary(decision.arguments) if definition is not None else decision.tool
        )
        async with self._engine.begin() as conn:
            step = await repo.add_step(
                conn,
                organization_id=row.organization_id,
                run_id=row.id,
                kind="tool",
                status="running",
                summary=summary[:300],
                detail={"tool_id": decision.tool[:64]},
                finished_at=None,
                duration_ms=None,
            )
        call = ToolCallPart(
            id=f"run-{row.id.hex[:12]}-{step_index}",
            name=decision.tool,
            arguments=decision.arguments,
        )
        [item] = await self._tools.plan(turn, step_index, [call])
        async with self._engine.begin() as conn:
            await repo.update_step(conn, step.id, invocation_id=item.invocation_id)
            await repo.update_run(conn, row.id, tool_call_count=repo.increment("tool_call_count"))
        if item.status == "awaiting_approval":
            async with self._engine.begin() as conn:
                await repo.update_step(conn, step.id, status="waiting")
            return await self._release(
                state,
                "waiting_for_approval",
                progress_message=f"Waiting for approval: {summary}"[:300],
            )
        if not item.runnable:
            refused = Finished(
                planned=item,
                status=item.status,
                error_code=item.error_code,
                error_message=item.error_message,
            )
            await self._record_tool_outcome(state, step.id, refused)
            return None
        await self._refresh(state)
        if state.stop.is_set():
            await self._stopped_call(step.id, decision.tool)
            return None
        finished = await _unless_stopped(self._tools.execute(turn, item), state.stop)
        if finished is None:
            await self._stopped_call(step.id, decision.tool)
            return None
        await self._record_tool_outcome(state, step.id, finished)
        return None

    async def _stopped_call(self, step_id: UUID, tool_id: str) -> None:
        """A call stopped before it finished (cancellation, shutdown, deadline). Should the run
        continue (on this worker or another), the model learns what happened to it."""
        observation = json.dumps(
            {
                "status": "error",
                "tool": tool_id,
                "error": {
                    "code": "interrupted",
                    "message": "The call was stopped before it finished; make it again if it "
                    "is still needed.",
                },
            }
        )
        async with self._engine.begin() as conn:
            await repo.update_step(
                conn,
                step_id,
                status="cancelled",
                finished_at=repo.now(),
                error_code="cancelled",
                detail={"tool_id": tool_id[:64], "observation": observation},
            )

    async def _refuse_call(self, state: _Run, decision: Decision, code: str, message: str) -> None:
        row = state.row
        observation = json.dumps(
            {"status": "error", "tool": decision.tool, "error": {"code": code, "message": message}}
        )
        async with self._engine.begin() as conn:
            await repo.add_step(
                conn,
                organization_id=row.organization_id,
                run_id=row.id,
                kind="tool",
                status="rejected",
                summary=f"Refused: {message}"[:300],
                detail={"tool_id": decision.tool[:64], "observation": observation},
                error_code=code,
            )

    async def _record_tool_outcome(self, state: _Run, step_id: UUID, finished: Finished) -> None:
        observation = self._observation(state, finished)
        status = {
            "succeeded": "succeeded",
            "rejected": "rejected",
            "cancelled": "cancelled",
        }.get(finished.status, "failed")
        detail = {
            "tool_id": finished.planned.tool_id,
            "status": finished.status,
            "observation": observation,
            "truncated": finished.truncated,
        }
        if finished.error_code:
            detail["error"] = {"code": finished.error_code, "message": finished.error_message}
        async with self._engine.begin() as conn:
            await repo.update_step(
                conn,
                step_id,
                status=status,
                detail=detail,
                error_code=finished.error_code or "",
                finished_at=repo.now(),
                duration_ms=finished.duration_ms,
            )
            if finished.status != "succeeded":
                await repo.update_run(
                    conn,
                    state.id,
                    progress_message=f"Tool {finished.planned.tool_id}: {finished.error_message or finished.status}"[
                        :300
                    ],
                )
        log.info(
            "agents.run.tool",
            run_id=str(state.id),
            tool_id=finished.planned.tool_id,
            status=finished.status,
            error_code=finished.error_code or None,
            duration_ms=finished.duration_ms,
        )

    def _observation(self, state: _Run, finished: Finished) -> str:
        """The result as the model sees it. Search results get run-wide references (R1, R2,
        …) the answer may cite; what each reference points at is kept for the final result."""
        payload = json.loads(finished.result_part().content)
        result = payload.get("result")
        if finished.status == "succeeded" and isinstance(result, dict):
            for item in result.get("results") or []:
                if not isinstance(item, dict) or "document_id" not in item:
                    continue
                ref = f"R{len(state.sources) + 1}"
                state.sources[ref] = {
                    "ref": ref,
                    "document_id": item.get("document_id"),
                    "chunk_id": item.get("chunk_id"),
                    "title": str(item.get("title") or "")[:200],
                }
                item["ref"] = ref
        return json.dumps(payload, ensure_ascii=False)

    # --- Recovery and approvals ----------------------------------------------------------------

    async def _settle_pending(self, state: _Run) -> Outcome | None:
        """What an earlier worker (or a pause for approval) left open: a tool call that was
        approved or declined, one that was recorded but never ran, one that was running when
        the worker died, or a step opened and never closed."""
        row = state.row
        async with self._engine.connect() as conn:
            steps = await repo.steps_of(conn, row.id)
            invocations = {i.id: i for i in await tools_repo.invocations_of_run(conn, row.id)}
        _restore(state, steps, invocations.values())
        open_steps = [s for s in steps if s.kind == "tool" and s.status in ("running", "waiting")]
        for step in open_steps:
            invocation = invocations.get(step.invocation_id) if step.invocation_id else None
            if invocation is None:
                await self._close_step(
                    state,
                    step.id,
                    "interrupted",
                    "The worker stopped before this call was recorded.",
                )
                continue
            definition = self._tools.registry.get(invocation.tool_id)
            if invocation.status in ("validated", "approved"):
                if definition is None or (definition.redact and invocation.status == "validated"):
                    await self._close_step(
                        state,
                        step.id,
                        "interrupted",
                        "This call could not be resumed; make it again if needed.",
                    )
                    async with self._engine.begin() as conn:
                        await tools_repo.finish(
                            conn,
                            invocation.id,
                            from_states=("validated", "approved"),
                            status="cancelled",
                            error_code="interrupted",
                            error_message="Not resumed.",
                        )
                    continue
                assert state.principal is not None  # noqa: S101
                turn = Turn(
                    principal=state.principal,
                    request_id=row.request_id or None,
                    offered=state.offered,
                    run_id=row.id,
                )
                call = ToolCallPart(
                    id=invocation.call_id,
                    name=invocation.tool_id,
                    arguments=dict(invocation.arguments or {}),
                )
                item = planned_from_row(invocation, call, definition)
                resumed = await _unless_stopped(self._tools.execute(turn, item), state.stop)
                if resumed is None:
                    return None
                await self._record_tool_outcome(state, step.id, resumed)
            elif invocation.status == "executing":
                if definition is not None and definition.read_only:
                    async with self._engine.begin() as conn:
                        await tools_repo.finish(
                            conn,
                            invocation.id,
                            from_states=("executing",),
                            status="cancelled",
                            error_code="interrupted",
                            error_message="The worker stopped while the tool ran.",
                        )
                    await self._close_step(
                        state,
                        step.id,
                        "interrupted",
                        "The worker stopped while this tool ran; call it again if needed.",
                    )
                else:
                    async with self._engine.begin() as conn:
                        await tools_repo.finish(
                            conn,
                            invocation.id,
                            from_states=("executing",),
                            status="failed",
                            error_code="interrupted",
                            error_message="Outcome unknown: the worker stopped while the tool ran.",
                        )
                    await self._close_step(state, step.id, "interrupted", "Outcome unknown.")
                    raise _RunFailed(
                        "interrupted",
                        "A tool that changes data was running when the worker stopped; its outcome is unknown, so the task was not continued.",
                    )
            elif invocation.status == "awaiting_approval":
                return await self._release(state, "waiting_for_approval")
            else:
                # Decided (declined) or otherwise final: its recorded outcome is the observation.
                call = ToolCallPart(id=invocation.call_id, name=invocation.tool_id, arguments={})
                item = planned_from_row(invocation, call, definition)
                await self._record_tool_outcome(state, step.id, finished_from_row(item, invocation))
        return None

    async def _close_step(self, state: _Run, step_id: UUID, code: str, message: str) -> None:
        observation = json.dumps({"status": "error", "error": {"code": code, "message": message}})
        async with self._engine.begin() as conn:
            await repo.update_step(
                conn,
                step_id,
                status="failed",
                error_code=code,
                detail={"observation": observation, "error": {"code": code, "message": message}},
                finished_at=repo.now(),
            )

    # --- Completion ----------------------------------------------------------------------------

    async def _complete(self, state: _Run, decision: Decision) -> Outcome | None:
        row = state.row
        schema = state.snapshot.get("output_schema")
        if schema:
            try:
                check_schema(schema)  # bounds validation time, whatever saved the agent
            except AIInvalidRequest as exc:
                raise _RunFailed(
                    "invalid_configuration", "The agent's output schema cannot be used."
                ) from exc
            if decision.output is None:
                return await self._invalid(
                    state,
                    "'complete' must include 'output' matching the output schema.",
                    time.perf_counter(),
                    steps=0,
                )
            errors = sorted(
                Draft202012Validator(schema, registry=_OFFLINE).iter_errors(decision.output),
                key=lambda e: list(e.path),
            )
            if errors:
                described = "; ".join(
                    (".".join(str(p) for p in e.path) or "output") + f": fails '{e.validator}'"
                    for e in errors[:5]
                )
                return await self._invalid(
                    state,
                    f"'output' does not match the output schema ({described}).",
                    time.perf_counter(),
                    steps=0,
                )
        answer = decision.answer or json.dumps(decision.output, ensure_ascii=False)
        cited = list(dict.fromkeys(_CITATION.findall(answer)))
        valid = [ref for ref in cited if ref in state.sources]
        invalid = [ref for ref in cited if ref not in state.sources]
        sources = [state.sources[ref] for ref in valid]
        async with self._engine.begin() as conn:
            await repo.add_step(
                conn,
                organization_id=row.organization_id,
                run_id=row.id,
                kind="final",
                status="succeeded",
                summary="Completed",
                detail={
                    "answer_chars": len(answer),
                    "cited": valid,
                    "invalid_citations": invalid,
                    "has_output": decision.output is not None,
                },
            )
        return await self._finish(
            state,
            "completed",
            "",
            "",
            result=answer,
            output=decision.output,
            sources=sources,
            progress_message="Done",
        )

    # --- Ending ----------------------------------------------------------------------------------

    async def _finish(
        self, state: _Run, status: str, reason: str, message: str, **values: Any
    ) -> Outcome:
        stamp = repo.now()
        async with self._engine.begin() as conn:
            row = await repo.release(
                conn,
                state.id,
                worker=state.worker,
                status=status,
                failure_reason=reason,
                error_message=message[:500],
                completed_at=stamp,
                cancel_requested=False,
                pause_requested=False,
                **values,
            )
            if row is None:
                # The lease was lost meanwhile: whoever holds it decides. Only a cancellation
                # is final from here (the user asked for it).
                if status == "cancelled":
                    row = await repo.transition(
                        conn,
                        state.id,
                        from_states=("running", "cancel_requested", "queued"),
                        status=status,
                        failure_reason=reason,
                        error_message=message[:500],
                        completed_at=stamp,
                        claimed_by="",
                        lease_expires_at=None,
                    )
                if row is None:
                    return Outcome("lost")
            await tools_repo.cancel_open_run(conn, state.id)
            await tools_repo.close_pending_approvals_of_run(conn, state.id, status="invalidated")
            await repo.close_open_steps(
                conn, state.id, status="cancelled" if status == "cancelled" else "failed"
            )
        action = {
            "completed": "AGENT_RUN_COMPLETED",
            "cancelled": "AGENT_RUN_CANCELLED",
        }.get(status, "AGENT_RUN_FAILED")
        await self._audit.record(
            action,
            organization_id=row.organization_id,
            actor_id=row.user_id,
            resource_type="agent_run",
            resource_id=str(row.id),
            request_id=row.request_id or None,
            metadata={
                "agent_id": str(row.agent_id),
                "agent_version": row.agent_version,
                "status": status,
                "failure_reason": reason or None,
                "steps": row.step_count,
                "tool_calls": row.tool_call_count,
                "model_calls": row.model_calls,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
            },
        )
        log.info(
            "agents.run.completed",
            run_id=str(row.id),
            agent_id=str(row.agent_id),
            status=status,
            failure_reason=reason or None,
            steps=row.step_count,
            tool_calls=row.tool_call_count,
            model_calls=row.model_calls,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_micros=row.cost_micros,
            duration_ms=_ms(state.started),
            attempts=row.attempts,
        )
        return Outcome(status, reason)

    async def _release(
        self, state: _Run, status: str, *, audit: str | None = None, **values: Any
    ) -> Outcome:
        if status in ("paused", "waiting_for_approval"):
            values.update(repo.stopped_clock(state.row))
        async with self._engine.begin() as conn:
            row = await repo.release(
                conn,
                state.id,
                worker=state.worker,
                from_states=("running",),
                status=status,
                **values,
            )
            current = (
                None
                if row is not None
                else await repo.get_run(conn, state.row.organization_id, state.id)
            )
        if row is None:
            if (
                current is not None
                and current.claimed_by == state.worker
                and (current.status == "cancel_requested" or current.cancel_requested)
            ):
                # Cancelled while this step ran: the cancellation wins over the pause or wait.
                return await self._finish(state, "cancelled", "cancelled", "Cancelled by the user.")
            return Outcome("lost")
        if audit:
            await self._audit.record(
                audit,
                organization_id=row.organization_id,
                actor_id=row.user_id,
                resource_type="agent_run",
                resource_id=str(row.id),
                request_id=row.request_id or None,
                metadata={"agent_id": str(row.agent_id), "status": status, "steps": row.step_count},
            )
        log.info("agents.run.released", run_id=str(row.id), status=status, steps=row.step_count)
        return Outcome(status)


# --- Helpers ---------------------------------------------------------------------------------


def _traced(decision: Decision, definition: ToolDefinition | None) -> dict[str, Any]:
    """The decision as recorded and replayed: with a known tool's redacted arguments."""
    traced: dict[str, Any] = {"action": decision.action}
    if decision.summary:
        traced["summary"] = decision.summary
    if decision.action in ("call_tool", "request_approval"):
        traced["tool"] = decision.tool
        traced["arguments"] = (
            redacted(definition, decision.arguments)
            if definition is not None
            else decision.arguments
        )
        if decision.message:
            traced["message"] = decision.message
    elif decision.action == "report_progress":
        traced["message"] = decision.message
    elif decision.action == "complete":
        traced["answer"] = decision.answer
        if decision.output is not None:
            traced["output"] = decision.output
    else:
        traced["reason"] = decision.reason
    return traced


def _default_summary(decision: Decision) -> str:
    if decision.action == "call_tool":
        return f"Calling {decision.tool}"
    if decision.action == "request_approval":
        return f"Asking to run {decision.tool}"
    if decision.action == "report_progress":
        return decision.message[:200]
    if decision.action == "complete":
        return "Answering"
    return "Giving up"


async def _unless_stopped[T](call: Awaitable[T], stop: asyncio.Event) -> T | None:
    """Await `call` unless `stop` is set first: then cancel it and return None."""
    if stop.is_set():
        if asyncio.iscoroutine(call):
            call.close()
        return None
    task = asyncio.ensure_future(call)
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait((task, stopped), return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if task.cancelled():
        return None
    return task.result()


def _ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _call_key(definition: ToolDefinition, decision: Decision) -> str:
    """Identifies a call as the executor's arguments hash does (over validated arguments),
    so calls recorded before a resume count too."""
    call = ToolCallPart(id="key", name=definition.id, arguments=decision.arguments)
    try:
        arguments = validate_arguments(definition, call)
    except ArgumentsRejected:
        arguments = decision.arguments
    return f"{definition.id}|{arguments_hash(definition, arguments)}"


def _restore(state: _Run, steps: Iterable[Any], invocations: Iterable[Any]) -> None:
    """Rebuild the in-memory parts of a run from its records: the references the answer may
    cite (numbered in the recorded observations) and how often each call was made."""
    state.sources.clear()
    for step in steps:
        if step.kind != "tool" or not isinstance(step.detail, dict):
            continue
        try:
            payload = json.loads(step.detail.get("observation") or "null")
        except ValueError:
            continue
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            continue
        for item in result.get("results") or []:
            if isinstance(item, dict) and isinstance(item.get("ref"), str):
                state.sources[item["ref"]] = {
                    "ref": item["ref"],
                    "document_id": item.get("document_id"),
                    "chunk_id": item.get("chunk_id"),
                    "title": str(item.get("title") or "")[:200],
                }
    state.identical.clear()
    for invocation in invocations:
        if invocation.arguments_hash:
            key = f"{invocation.tool_id}|{invocation.arguments_hash}"
            state.identical[key] = state.identical.get(key, 0) + 1

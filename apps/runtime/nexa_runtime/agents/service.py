"""The runs API's operations (docs/api/agents.md#runs): submitting a task, reading runs and
their history, pausing, resuming and cancelling, deciding on a run's pending action, and the
progress stream. Every read is the run owner's; everything else is 404."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import BaseModel
from referencing import Registry as SchemaRegistry
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from nexa_ai.context import AIRequestContext
from nexa_ai.errors import AIModelUnavailable
from nexa_ai.gateway import Gateway, KeepAlive
from nexa_common.access import Principal, require
from nexa_common.errors import FieldError, NotFound, ValidationFailed
from nexa_common.logs import get_logger
from nexa_runtime.agents import repository as repo
from nexa_runtime.agents.errors import (
    AgentsDisabled,
    AgentUnavailable,
    RunLimitReached,
    RunNotActive,
)
from nexa_runtime.agents.runner import AgentRunner
from nexa_runtime.agents.schemas import (
    CreateRun,
    RunControlOut,
    RunDecision,
    RunLimits,
    RunOut,
    RunSource,
    StepOut,
)
from nexa_runtime.chat import repository as chat_repo
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.ratelimit import Limiter
from nexa_runtime.tools import repository as tools_repo
from nexa_runtime.tools.activity import activity_for_run, approval_out, readable_ids
from nexa_runtime.tools.audit import ToolAudit
from nexa_runtime.tools.errors import ApprovalClosed, ApprovalExpired, ApprovalStale
from nexa_runtime.tools.executor import DECLINED, ToolExecutor
from nexa_runtime.tools.schemas import ToolActivityOut

log = get_logger("nexa.runtime.agents.service")

_OFFLINE: SchemaRegistry[Any] = SchemaRegistry()
_EVENT_POLL_SECONDS = 1.0
_EVENT_PING_SECONDS = 15.0
_EVENT_MAX_SECONDS = 3600.0


class RunEvent(BaseModel):
    type: str
    run: RunOut | None = None
    step: StepOut | None = None


class AgentService:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        gateway: Gateway,
        executor: ToolExecutor,
        audit: ToolAudit,
        settings: RuntimeSettings,
        limiter: Limiter | None = None,
        runner: AgentRunner | None = None,
    ) -> None:
        self._engine = engine
        self._gateway = gateway
        self._executor = executor
        self._audit = audit
        self._settings = settings
        self._limiter = limiter or Limiter(None)
        self._runner = runner

    # --- Submitting ----------------------------------------------------------------------------

    async def submit(
        self, principal: Principal, body: CreateRun, *, request_id: str | None
    ) -> tuple[RunOut, bool]:
        """A new run, queued; or the earlier run with the same idempotency key (created False)."""
        if not self._settings.agents_enabled:
            raise AgentsDisabled("Agents are switched off.")
        for permission in ("ai.chat", "agents.execute", "ai.agent.execute"):
            require(principal, permission)
        organization_id = _organization(principal)
        async with self._engine.connect() as conn:
            if body.idempotency_key:
                earlier = await repo.run_by_idempotency_key(
                    conn, principal.user_id, body.idempotency_key
                )
                if earlier is not None:
                    return await self._out(conn, principal, earlier), False
            agent = await repo.get_agent(conn, organization_id, body.agent_id)
            if agent is None or not agent.enabled:
                raise AgentUnavailable
            if body.conversation_id is not None:
                conversation = await chat_repo.get_conversation(
                    conn, organization_id, body.conversation_id
                )
                if conversation is None or conversation.owner_id != principal.user_id:
                    raise NotFound
            validated_input = _validate_input(agent.input_schema, body.input)
            if agent.model:
                names = {
                    alias.name
                    for alias in await self._gateway.available_models(
                        AIRequestContext.from_principal(principal, request_id=request_id)
                    )
                }
                if agent.model not in names:
                    raise AIModelUnavailable(f"Model '{agent.model}' is not available.")
            await self._limiter.hit(
                "agent_runs",
                str(principal.user_id),
                limit=self._settings.agents_runs_per_hour,
                window_seconds=3600,
            )
            if (
                await repo.count_active(
                    conn, organization_id=organization_id, user_id=principal.user_id
                )
                >= self._settings.agents_runs_per_user
            ):
                raise RunLimitReached("You have too many runs in progress. Wait for one to finish.")
            if (
                await repo.count_active(conn, organization_id=organization_id, user_id=None)
                >= self._settings.agents_runs_per_organization
            ):
                raise RunLimitReached(
                    "Your organization has too many runs in progress. Try again later."
                )
        snapshot = self._snapshot(agent)
        try:
            async with self._engine.begin() as conn:
                row = await repo.create_run(
                    conn,
                    organization_id=organization_id,
                    agent_id=agent.id,
                    agent_version=agent.version,
                    agent_snapshot=snapshot,
                    user_id=principal.user_id,
                    conversation_id=body.conversation_id,
                    goal=body.goal,
                    input=validated_input,
                    model=agent.model,
                    idempotency_key=body.idempotency_key,
                    request_id=request_id or "",
                )
                out = await self._out(conn, principal, row)
        except IntegrityError:
            # A concurrent submission with the same key won the race: that run is the answer.
            if not body.idempotency_key:
                raise
            async with self._engine.connect() as conn:
                earlier = await repo.run_by_idempotency_key(
                    conn, principal.user_id, body.idempotency_key
                )
                if earlier is None:  # pragma: no cover - the constraint names this key
                    raise
                return await self._out(conn, principal, earlier), False
        await self._audit.record(
            "AGENT_RUN_STARTED",
            organization_id=organization_id,
            actor_id=principal.user_id,
            resource_type="agent_run",
            resource_id=str(row.id),
            request_id=request_id,
            metadata={
                "agent_id": str(agent.id),
                "agent_version": agent.version,
                "goal_chars": len(body.goal),
            },
        )
        log.info(
            "agents.run.submitted",
            run_id=str(row.id),
            agent_id=str(agent.id),
            agent_version=agent.version,
        )
        if self._runner is not None:
            self._runner.wake()
        return out, True

    def _snapshot(self, agent: Any) -> dict[str, Any]:
        """The agent as the run will use it: limits capped by the platform's settings."""
        settings = self._settings
        return {
            "name": agent.name,
            "slug": agent.slug,
            "version": agent.version,
            "instructions": agent.instructions,
            "model": agent.model,
            "allowed_tools": list(agent.allowed_tools or []),
            "input_schema": agent.input_schema,
            "output_schema": agent.output_schema,
            "approval_policy": agent.approval_policy,
            "limits": {
                "max_steps": min(int(agent.max_steps), settings.agents_max_steps),
                "max_tool_calls": min(int(agent.max_tool_calls), settings.agents_max_tool_calls),
                "max_duration_seconds": min(
                    int(agent.max_duration_seconds), settings.agents_max_duration_seconds
                ),
                "max_tokens": min(int(agent.max_tokens), settings.agents_max_tokens),
            },
        }

    # --- Reading ------------------------------------------------------------------------------

    async def list_runs(
        self,
        principal: Principal,
        *,
        status: str | None,
        agent_id: UUID | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[RunOut], str | None]:
        require(principal, "agents.view")
        async with self._engine.connect() as conn:
            rows, next_cursor = await repo.list_runs(
                conn,
                _organization(principal),
                principal.user_id,
                status=status,
                agent_id=agent_id,
                limit=limit,
                cursor=cursor,
            )
            agents = await self._agent_names(conn, principal, rows)
            outs = [await self._out(conn, principal, row, agents) for row in rows]
        return outs, next_cursor

    async def get_run(self, principal: Principal, run_id: UUID) -> RunOut:
        async with self._engine.connect() as conn:
            row = await self._load(conn, principal, run_id)
            return await self._out(conn, principal, row)

    async def steps(
        self, principal: Principal, run_id: UUID, *, after: int, limit: int
    ) -> list[StepOut]:
        async with self._engine.connect() as conn:
            row = await self._load(conn, principal, run_id)
            rows = await repo.steps_of(conn, row.id, after=after, limit=limit)
            activity = await activity_for_run(conn, principal, self._executor.registry, row.id)
        by_invocation = {a.invocation_id: a for a in activity}
        return [_step_out(step, by_invocation.get(step.invocation_id)) for step in rows]

    # --- Control ------------------------------------------------------------------------------

    async def pause(
        self, principal: Principal, run_id: UUID, *, request_id: str | None
    ) -> RunControlOut:
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, run_id, for_update=True)
            if row.status == "queued":
                updated = await repo.transition(
                    conn,
                    row.id,
                    from_states=("queued",),
                    status="paused",
                    pause_requested=False,
                    **repo.stopped_clock(row),
                )
                message = "Paused."
            elif row.status == "running":
                updated = await repo.update_run(conn, row.id, pause_requested=True)
                message = "The run will pause at its next checkpoint."
            elif row.status == "paused":
                updated, message = row, "Already paused."
            else:
                raise RunNotActive("Only a queued or running task can be paused.")
        if updated is None:  # pragma: no cover - the row was locked
            raise RunNotActive
        if row.status == "paused":
            return _control_out(updated, message)  # nothing changed: nothing to audit
        await self._audit.record(
            "AGENT_RUN_PAUSED",
            organization_id=row.organization_id,
            actor_id=principal.user_id,
            resource_type="agent_run",
            resource_id=str(row.id),
            request_id=request_id,
            metadata={"agent_id": str(row.agent_id), "from_status": row.status},
        )
        return _control_out(updated, message)

    async def resume(
        self, principal: Principal, run_id: UUID, *, request_id: str | None
    ) -> RunControlOut:
        """Back to the queue, after checking again that the user may run the agent and that
        the agent still exists and is enabled. Its clock restarts with the time it had left;
        the engine applies the step, token and time budgets at its first checkpoint."""
        for permission in ("ai.chat", "agents.execute", "ai.agent.execute"):
            require(principal, permission)
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, run_id, for_update=True)
            if row.status != "paused":
                raise RunNotActive("Only a paused task can be resumed.")
            agent = await repo.get_agent(conn, row.organization_id, row.agent_id)
            if agent is None or not agent.enabled:
                raise AgentUnavailable("The agent is no longer available.")
            updated = await repo.transition(
                conn,
                row.id,
                from_states=("paused",),
                status="queued",
                pause_requested=False,
                **repo.restarted_clock(row),
            )
        if updated is None:  # pragma: no cover
            raise RunNotActive
        await self._audit.record(
            "AGENT_RUN_RESUMED",
            organization_id=row.organization_id,
            actor_id=principal.user_id,
            resource_type="agent_run",
            resource_id=str(row.id),
            request_id=request_id,
            metadata={"agent_id": str(row.agent_id)},
        )
        if self._runner is not None:
            self._runner.wake()
        return _control_out(updated, "Resumed.")

    async def cancel(
        self, principal: Principal, run_id: UUID, *, request_id: str | None
    ) -> RunControlOut:
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, run_id, for_update=True)
            if row.status in ("queued", "paused", "waiting_for_approval"):
                updated = await repo.transition(
                    conn,
                    row.id,
                    from_states=("queued", "paused", "waiting_for_approval"),
                    status="cancelled",
                    failure_reason="cancelled",
                    error_message="Cancelled by the user.",
                    completed_at=repo.now(),
                    cancel_requested=False,
                    pause_requested=False,
                    claimed_by="",
                    lease_expires_at=None,
                )
                await tools_repo.cancel_open_run(conn, row.id)
                await tools_repo.close_pending_approvals_of_run(conn, row.id, status="invalidated")
                await repo.close_open_steps(conn, row.id, status="cancelled")
                message = "Cancelled."
            elif row.status == "running":
                updated = await repo.transition(
                    conn,
                    row.id,
                    from_states=("running",),
                    status="cancel_requested",
                    cancel_requested=True,
                )
                message = "Cancelling: the run stops at its next checkpoint."
            elif row.status == "cancel_requested":
                updated, message = row, "Cancellation is already in progress."
            else:
                raise RunNotActive("The task has already ended.")
        if updated is None:  # pragma: no cover
            raise RunNotActive
        if row.status == "cancel_requested":
            return _control_out(updated, message)  # already asked: nothing to audit
        await self._audit.record(
            "AGENT_RUN_CANCELLED",
            organization_id=row.organization_id,
            actor_id=principal.user_id,
            resource_type="agent_run",
            resource_id=str(row.id),
            request_id=request_id,
            metadata={"agent_id": str(row.agent_id), "from_status": row.status},
        )
        return _control_out(updated, message)

    async def decide(
        self,
        principal: Principal,
        run_id: UUID,
        approval_id: UUID,
        body: RunDecision,
        *,
        request_id: str | None,
    ) -> RunOut:
        """The user's decision on the action the run is waiting for. The same binding as chat
        approvals: the requester only, the exact arguments (hash), before expiry, once. The
        run goes back to the queue and the engine runs (or reports the declined) call."""
        for permission in ("ai.chat", "agents.execute", "ai.agent.execute"):
            require(principal, permission)
        organization_id = _organization(principal)
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, run_id, for_update=True)
            approval = await tools_repo.approval(
                conn, organization_id, approval_id, for_update=True
            )
            if approval is None or approval.requested_by_id != principal.user_id:
                raise NotFound
            invocation = await tools_repo.invocation(conn, organization_id, approval.invocation_id)
            if invocation is None or invocation.run_id != row.id:
                raise NotFound
            if row.status != "waiting_for_approval" or approval.status != "pending":
                raise ApprovalClosed()
            timed_out = approval.expires_at <= repo.now()
            if timed_out:
                # Ended here and committed when the block exits; the error is raised after.
                expired = await repo.fail_waiting(conn, row.id)
            else:
                expired = None
                if body.arguments_hash != approval.arguments_hash:
                    raise ApprovalStale()
                approved = body.decision == "approve"
                decided = await tools_repo.decide(
                    conn,
                    approval.id,
                    status="approved" if approved else "rejected",
                    decided_by=principal.user_id,
                )
                if decided is None:
                    raise ApprovalClosed()
                if approved:
                    await tools_repo.set_status(
                        conn, invocation.id, "approved", from_states=("awaiting_approval",)
                    )
                else:
                    await tools_repo.finish(
                        conn,
                        invocation.id,
                        from_states=("awaiting_approval",),
                        status="rejected",
                        error_code=DECLINED,
                        error_message="The user declined this action."
                        + (f" ({body.comment})" if body.comment else ""),
                    )
                updated = await repo.transition(
                    conn,
                    row.id,
                    from_states=("waiting_for_approval",),
                    status="queued",
                    progress_message="Approved; continuing" if approved else "Declined; continuing",
                    **repo.restarted_clock(row),
                )
                if updated is None:  # pragma: no cover
                    raise ApprovalClosed()
                out = await self._out(conn, principal, updated)
        if timed_out:
            if expired is None:  # pragma: no cover - the row was locked
                raise ApprovalExpired()
            await self._audit.record(
                "AGENT_RUN_FAILED",
                organization_id=row.organization_id,
                actor_id=principal.user_id,
                resource_type="agent_run",
                resource_id=str(row.id),
                request_id=request_id,
                metadata={
                    "agent_id": str(row.agent_id),
                    "status": "failed",
                    "failure_reason": "approval_expired",
                },
            )
            raise ApprovalExpired()
        await self._executor.audit_decision(
            principal,
            invocation_id=invocation.id,
            approval_id=approval.id,
            tool_id=approval.tool_id,
            decision=body.decision,
            request_id=request_id,
            run_id=row.id,
        )
        if self._runner is not None:
            self._runner.wake()
        return out

    # --- Progress stream -----------------------------------------------------------------------

    async def events(
        self, principal: Principal, run_id: UUID
    ) -> AsyncIterator[RunEvent | KeepAlive]:
        """`run` whenever the run changes, `step` for each new or changed step, and `end` once
        it is over; pings while nothing happens. Read from the database, because any worker
        (of any Runtime process) may be running it."""
        organization_id = _organization(principal)
        sent: dict[UUID, tuple[str, Any]] = {}
        last_seen: tuple[str, Any] | None = None
        loop = asyncio.get_running_loop()
        started = since_ping = loop.time()
        first = True
        while loop.time() - started < _EVENT_MAX_SECONDS:
            if not first:
                await asyncio.sleep(_EVENT_POLL_SECONDS)
            async with self._engine.connect() as conn:
                if first:
                    current = await self._load(conn, principal, run_id)
                else:
                    found = await repo.get_run(conn, organization_id, run_id)
                    if found is None or found.user_id != principal.user_id:
                        return
                    current = found
                steps = await repo.steps_of(conn, current.id)
                changed = [s for s in steps if sent.get(s.id) != (s.status, s.finished_at)]
                by_invocation: dict[UUID, ToolActivityOut] = {}
                if any(s.invocation_id for s in changed):
                    activity = await activity_for_run(
                        conn, principal, self._executor.registry, current.id
                    )
                    by_invocation = {a.invocation_id: a for a in activity}
                state = (current.status, current.updated_at)
                out = await self._out(conn, principal, current) if state != last_seen else None
            first = False
            emitted = False
            if out is not None:
                yield RunEvent(type="run", run=out)
                last_seen = state
                emitted = True
            for step in changed:
                yield RunEvent(
                    type="step", step=_step_out(step, by_invocation.get(step.invocation_id))
                )
                sent[step.id] = (step.status, step.finished_at)
                emitted = True
            if current.status in repo.FINAL:
                if out is None:
                    async with self._engine.connect() as conn:
                        out = await self._out(conn, principal, current)
                yield RunEvent(type="end", run=out)
                return
            now = loop.time()
            if emitted:
                since_ping = now
            elif now - since_ping >= _EVENT_PING_SECONDS:
                yield KeepAlive()
                since_ping = now

    # --- Internals -----------------------------------------------------------------------------

    async def _load(
        self, conn: AsyncConnection, principal: Principal, run_id: UUID, *, for_update: bool = False
    ) -> repo.RunRow:
        require(principal, "agents.view")
        row = await repo.get_run(conn, _organization(principal), run_id, for_update=for_update)
        if row is None or row.user_id != principal.user_id:
            raise NotFound
        return row

    async def _agent_names(
        self, conn: AsyncConnection, principal: Principal, rows: list[Any]
    ) -> dict[UUID, str]:
        names: dict[UUID, str] = {}
        for agent_id in {row.agent_id for row in rows}:
            agent = await repo.get_agent(conn, _organization(principal), agent_id)
            names[agent_id] = agent.name if agent is not None else ""
        return names

    async def _out(
        self,
        conn: AsyncConnection,
        principal: Principal,
        row: Any,
        names: dict[UUID, str] | None = None,
    ) -> RunOut:
        snapshot = row.agent_snapshot or {}
        limits = snapshot.get("limits") or {}
        if names is None:
            names = await self._agent_names(conn, principal, [row])
        pending = None
        if row.status == "waiting_for_approval":
            invocations = await tools_repo.invocations_of_run(conn, row.id)
            waiting = [i.id for i in invocations if i.status == "awaiting_approval"]
            approvals = await tools_repo.approvals_of(conn, waiting)
            if approvals:
                pending = approval_out(approvals[0], self._executor.registry)
        sources = list(row.sources or [])
        readable = await readable_ids(
            conn,
            principal,
            {UUID(str(s["document_id"])) for s in sources if s.get("document_id")},
        )
        return RunOut(
            id=row.id,
            agent_id=row.agent_id,
            agent_name=names.get(row.agent_id) or str(snapshot.get("name") or ""),
            agent_version=row.agent_version,
            status=row.status,
            goal=row.goal,
            input=dict(row.input or {}),
            conversation_id=row.conversation_id,
            model=row.model,
            progress_message=row.progress_message,
            step_count=row.step_count,
            tool_call_count=row.tool_call_count,
            limits=RunLimits(
                max_steps=int(limits.get("max_steps", 0)),
                max_tool_calls=int(limits.get("max_tool_calls", 0)),
                max_duration_seconds=int(limits.get("max_duration_seconds", 0)),
                max_tokens=int(limits.get("max_tokens", 0)),
            ),
            usage=RunOut.usage_of(row),
            result=row.result,
            output=row.output,
            sources=[
                RunSource(
                    ref=str(s.get("ref", "")),
                    document_id=UUID(str(s["document_id"])),
                    chunk_id=UUID(str(s["chunk_id"])) if s.get("chunk_id") else None,
                    title=str(s.get("title") or ""),
                    available=UUID(str(s["document_id"])) in readable,
                )
                for s in sources
                if s.get("document_id")
            ],
            failure_reason=row.failure_reason or None,
            error_message=row.error_message or None,
            cancel_requested=row.cancel_requested,
            pause_requested=row.pause_requested,
            pending_approval=pending,
            can_pause=row.status in ("queued", "running") and not row.pause_requested,
            can_resume=row.status == "paused",
            can_cancel=row.status in repo.ACTIVE and row.status != "cancel_requested",
            created_at=row.created_at,
            started_at=row.started_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
            deadline_at=row.deadline_at if row.time_left_seconds is None else None,
            time_left_seconds=row.time_left_seconds,
        )


def _validate_input(schema: Any, data: dict[str, Any]) -> dict[str, Any]:
    if not schema:
        return data
    errors = sorted(
        Draft202012Validator(schema, registry=_OFFLINE).iter_errors(data),
        key=lambda e: list(e.path),
    )
    if errors:
        raise ValidationFailed(
            details=[
                FieldError(
                    field="input." + ".".join(str(p) for p in e.path) if e.path else "input",
                    code=str(e.validator),
                    message=f"fails '{e.validator}'",
                )
                for e in errors[:5]
            ]
        )
    return data


def _step_out(step: Any, tool: ToolActivityOut | None) -> StepOut:
    detail = dict(step.detail or {})
    detail.pop("observation", None)  # the model's view; the tool activity is the user's
    return StepOut(
        id=step.id,
        sequence=step.sequence,
        kind=step.kind,
        status=step.status,
        summary=step.summary,
        detail=detail,
        tool=tool,
        error_code=step.error_code or None,
        retry_count=step.retry_count,
        input_tokens=step.input_tokens,
        output_tokens=step.output_tokens,
        started_at=step.started_at,
        finished_at=step.finished_at,
        duration_ms=step.duration_ms,
        created_at=step.created_at,
    )


def _control_out(row: Any, message: str) -> RunControlOut:
    return RunControlOut(
        id=row.id,
        status=row.status,
        cancel_requested=row.cancel_requested,
        pause_requested=row.pause_requested,
        message=message,
    )


def _organization(principal: Principal) -> UUID:
    if principal.organization_id is None:  # pragma: no cover - CurrentPrincipal guarantees it
        raise NotFound
    return principal.organization_id

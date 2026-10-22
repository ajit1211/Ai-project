"""The tool execution engine (docs/architecture/tools.md#execution).

The model is an untrusted planner: it may ask for any tool with any arguments. Every call
goes through one path, and stops at the first check it fails:

    requested ─► validated ─► (awaiting_approval ─► approved) ─► executing ─► succeeded
         │            │                 │                            ├──► failed
         └────────────┴─────────────────┴──► rejected                ├──► timed_out
                                                                     └──► cancelled

1. `plan` (per model step): the call id is new in this turn; the tool exists, was offered in
   this turn and is switched on; the caller (permissions read from the database now) may use
   it; the arguments are a JSON object valid against its schema; the turn's call budget and
   the user's rate limit allow it. The call is recorded under an idempotency key derived from
   the reply, the step, the position and the call id, so repeating a step records nothing new
   and never runs anything twice. A tool that needs approval stops at `awaiting_approval`.
2. `run`: each runnable call is claimed with one conditional UPDATE (only one request can win
   it), permissions and switches are checked again, an approval must match the call's exact
   arguments, then the trusted handler runs under its timeout. Read-only tools may be re-run
   after a retryable failure, within their limit; tools that change data never are. The output
   is validated against the tool's output schema and bounded before anything is stored or
   returned. Nothing reports success before the handler has.

Tool results go back to the model as JSON data, and are recorded in `tool_invocations`.
"""

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_ai.types import Capability, ToolCallPart, ToolResultPart
from nexa_common.access import Principal
from nexa_common.errors import NexaError, PermissionDenied, RateLimited
from nexa_common.logs import get_logger
from nexa_runtime.auth.dependencies import reload_principal
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.ratelimit import Limiter
from nexa_runtime.tools import repository as repo
from nexa_runtime.tools.audit import ToolAudit
from nexa_runtime.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolFailure,
    ToolOutput,
    ToolRegistry,
)
from nexa_runtime.tools.validation import (
    ArgumentsRejected,
    OutputRejected,
    arguments_hash,
    canonical,
    redacted,
    validate_arguments,
    validate_output,
)

log = get_logger("nexa.runtime.tools")

EXECUTE_PERMISSION = "ai.tool.execute"
_CALL_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:")
DECLINED = "declined_by_user"


@dataclass
class Turn:
    """One reply's tool use: who it runs for and what was offered to the model."""

    principal: Principal
    conversation_id: UUID
    message_id: UUID
    request_id: str | None
    offered: dict[str, ToolDefinition]
    calls_made: int = 0
    output_bytes: int = 0
    seen_call_ids: set[str] = field(default_factory=set)

    @property
    def organization_id(self) -> UUID:
        if self.principal.organization_id is None:  # pragma: no cover - principals have one
            raise PermissionDenied("Select an organization first.")
        return self.principal.organization_id


@dataclass(frozen=True, slots=True)
class Planned:
    """A recorded call and where it stands after the checks."""

    invocation_id: UUID
    step: int
    position: int
    call: ToolCallPart
    tool_id: str
    definition: ToolDefinition | None
    arguments: dict[str, Any]
    arguments_hash: str
    status: str
    summary: str
    error_code: str = ""
    error_message: str = ""
    approval_id: UUID | None = None

    @property
    def runnable(self) -> bool:
        return self.status in repo.RUNNABLE


@dataclass(frozen=True, slots=True)
class Finished:
    """A call that has ended, and what goes back to the model about it."""

    planned: Planned
    status: str
    data: dict[str, Any] | None = None
    truncated: bool = False
    error_code: str = ""
    error_message: str = ""
    duration_ms: int | None = None
    sources: tuple[dict[str, Any], ...] = ()

    def result_part(self) -> ToolResultPart:
        return result_part(
            self.planned.call.id,
            self.planned.tool_id,
            self.status,
            self.data,
            self.truncated,
            self.error_code,
            self.error_message,
        )


def result_part(
    call_id: str,
    tool_id: str,
    status: str,
    data: Any,
    truncated: bool,
    error_code: str,
    error_message: str,
) -> ToolResultPart:
    """The tool result as the model sees it: JSON data, with an explicit status. A failure is
    never presented as a result, and a truncated result says so."""
    if status == "succeeded":
        body: dict[str, Any] = {"status": "ok", "tool": tool_id, "result": data}
        if truncated:
            body["truncated"] = True
        return ToolResultPart(call_id=call_id, content=canonical(body))
    if error_code == DECLINED:
        body = {"status": "declined", "tool": tool_id, "message": "The user declined this action."}
    else:
        body = {
            "status": "error",
            "tool": tool_id,
            "error": {"code": error_code or status, "message": error_message or status},
        }
    return ToolResultPart(call_id=call_id, content=canonical(body), is_error=True)


class ToolExecutor:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        registry: ToolRegistry,
        settings: RuntimeSettings,
        limiter: Limiter | None = None,
        audit: ToolAudit | None = None,
        reload: Callable[[Any, Principal], Any] = reload_principal,
    ) -> None:
        self._engine = engine
        self.registry = registry
        self._settings = settings
        self._limiter = limiter or Limiter(None)
        self._audit = audit or ToolAudit(engine)
        self._reload = reload
        self._clock = time.perf_counter

    # --- Discovery -------------------------------------------------------------------------

    async def offer(
        self, principal: Principal, capabilities: frozenset[Capability]
    ) -> list[ToolDefinition]:
        """The tools a model may be offered for this user now: switched on, permitted, and
        only if the model can call tools at all. Offering is not authorization: every call is
        checked again when it runs."""
        if not self._settings.tools_enabled or Capability.TOOL_CALLING not in capabilities:
            return []
        if not principal.has(EXECUTE_PERMISSION) or principal.organization_id is None:
            return []
        switches = await self.switches(principal.organization_id)
        return [
            d
            for d in self.registry.all()
            if "conversation" in d.scopes and _enabled(d, switches) and d.allowed_for(principal)
        ]

    async def switches(self, organization_id: UUID) -> dict[str, tuple[bool, bool]]:
        async with self._engine.connect() as conn:
            return await repo.switches(conn, organization_id)

    def is_enabled(
        self, definition: ToolDefinition, switches: dict[str, tuple[bool, bool]]
    ) -> bool:
        return _enabled(definition, switches)

    # --- Planning --------------------------------------------------------------------------

    async def plan(self, turn: Turn, step: int, calls: list[ToolCallPart]) -> list[Planned]:
        """Check and record one model step's calls, in the order the model made them."""
        if not calls:
            return []
        principal = await self._fresh(turn.principal)
        switches = await self.switches(turn.organization_id)
        planned: list[Planned] = []
        for position, call in enumerate(calls):
            # One action needing approval per step: the user decides on one thing at a time.
            approval_free = not any(p.status == "awaiting_approval" for p in planned)
            planned.append(
                await self._plan_one(
                    turn, step, position, call, principal, switches, approval_free=approval_free
                )
            )
        return planned

    async def _plan_one(
        self,
        turn: Turn,
        step: int,
        position: int,
        call: ToolCallPart,
        principal: Principal | None,
        switches: dict[str, tuple[bool, bool]],
        *,
        approval_free: bool = True,
    ) -> Planned:
        definition = turn.offered.get(call.name)
        rejection: tuple[str, str, str] | None = None  # (code, message, audit action)
        arguments: dict[str, Any] = {}
        digest = ""
        if not call.id or len(call.id) > 128 or not set(call.id) <= _CALL_ID_CHARS:
            rejection = (
                "invalid_call",
                "The tool call has an invalid id.",
                "TOOL_INVOCATION_REJECTED",
            )
        elif call.id in turn.seen_call_ids:
            rejection = (
                "duplicate_call",
                "This tool call id was already used.",
                "TOOL_INVOCATION_REJECTED",
            )
        elif definition is None:
            rejection = (
                "unknown_tool",
                f"There is no tool named '{call.name[:64]}' available here.",
                "TOOL_INVOCATION_REJECTED",
            )
        elif not _enabled(definition, switches):
            rejection = ("tool_disabled", "This tool is switched off.", "TOOL_INVOCATION_REJECTED")
        elif principal is None or not _permitted(definition, principal):
            rejection = (
                "permission_denied",
                "You do not have permission to use this tool.",
                "TOOL_PERMISSION_DENIED",
            )
        elif definition.requires_approval and not approval_free:
            rejection = (
                "one_action_at_a_time",
                "Only one action that needs approval can be proposed at a time.",
                "TOOL_INVOCATION_REJECTED",
            )
        elif turn.calls_made >= self._settings.tools_max_calls_per_turn:
            rejection = (
                "too_many_calls",
                "The limit of tool calls for this answer has been reached.",
                "TOOL_INVOCATION_REJECTED",
            )
        else:
            try:
                arguments = validate_arguments(definition, call)
                digest = arguments_hash(definition, arguments)
            except ArgumentsRejected as exc:
                rejection = (exc.code, exc.message, "TOOL_INVOCATION_REJECTED")
        if rejection is None and definition is not None:
            try:
                await self._limiter.hit(
                    "tool_calls",
                    str(turn.principal.user_id),
                    limit=self._settings.tools_calls_per_minute,
                    window_seconds=60,
                )
            except RateLimited:
                rejection = (
                    "rate_limited",
                    "Too many tool calls in a short time; try again in a minute.",
                    "TOOL_RATE_LIMITED",
                )
        turn.seen_call_ids.add(call.id)
        turn.calls_made += 1

        tool_id = definition.id if definition is not None else call.name[:64]
        needs_approval = (
            rejection is None and definition is not None and definition.requires_approval
        )
        status = (
            "rejected" if rejection else ("awaiting_approval" if needs_approval else "validated")
        )
        summary = definition.summary(arguments) if definition is not None else tool_id
        values: dict[str, Any] = {
            "organization_id": turn.organization_id,
            "conversation_id": turn.conversation_id,
            "message_id": turn.message_id,
            "user_id": turn.principal.user_id,
            "step": step,
            "position": position,
            "call_id": call.id[:128],
            "tool_id": tool_id,
            "tool_version": definition.version if definition else "",
            "status": status,
            "risk": definition.risk if definition else "",
            "arguments": redacted(definition, arguments) if definition else {},
            "arguments_hash": digest,
            "result": None,
            "result_truncated": False,
            "error_code": rejection[0] if rejection else "",
            "error_message": rejection[1] if rejection else "",
            "sources": [],
            "idempotency_key": _idempotency_key(turn.message_id, step, position, call),
        }
        if rejection:
            values["completed_at"] = repo.now()
        approval_id: UUID | None = None
        async with self._engine.begin() as conn:
            row, created = await repo.record_invocation(conn, **values)
            if created and needs_approval and definition is not None:
                details = (
                    definition.approval_details(arguments) if definition.approval_details else {}
                )
                approval = await repo.create_approval(
                    conn,
                    organization_id=turn.organization_id,
                    invocation_id=row.id,
                    requested_by_id=turn.principal.user_id,
                    tool_id=definition.id,
                    tool_version=definition.version,
                    summary=summary,
                    details=details,
                    arguments_hash=digest,
                    risk=definition.risk,
                    expires_at=repo.now()
                    + timedelta(seconds=self._settings.tools_approval_ttl_seconds),
                )
                approval_id = approval.id
        if not created:
            # This step was recorded before (a repeated step): report it as it stands.
            return _planned_from_row(row, call, definition)
        audit_meta = {"tool_id": tool_id, "tool_version": values["tool_version"], "step": step}
        if rejection:
            log.info("tools.invocation.rejected", tool_id=tool_id, error_code=rejection[0])
            await self._audit_event(
                rejection[2], turn, row.id, {**audit_meta, "error_code": rejection[0]}
            )
        elif needs_approval:
            await self._audit_event(
                "TOOL_APPROVAL_REQUESTED",
                turn,
                row.id,
                {**audit_meta, "approval_id": str(approval_id)},
            )
        return Planned(
            invocation_id=row.id,
            step=step,
            position=position,
            call=call,
            tool_id=tool_id,
            definition=definition,
            arguments=arguments,
            arguments_hash=digest,
            status=status,
            summary=summary,
            error_code=values["error_code"],
            error_message=values["error_message"],
            approval_id=approval_id,
        )

    # --- Running ---------------------------------------------------------------------------

    async def run(self, turn: Turn, planned: list[Planned]) -> AsyncIterator[Finished]:
        """Run the runnable calls of one step and yield each as it ends; calls that were
        rejected are yielded as they are. Calls run together only when every one of them is
        read-only and safe to run concurrently; otherwise one at a time, in order."""
        for item in planned:
            if item.runnable:
                continue
            if item.status in repo.FINAL:
                yield Finished(
                    planned=item,
                    status=item.status,
                    error_code=item.error_code,
                    error_message=item.error_message,
                )
            else:
                yield Finished(
                    planned=item,
                    status="cancelled",
                    error_code="not_run",
                    error_message="This call was not run here.",
                )
        runnable = [item for item in planned if item.runnable]
        together = len(runnable) > 1 and all(
            item.definition is not None
            and item.definition.read_only
            and item.definition.concurrency_safe
            for item in runnable
        )
        if not together:
            for item in runnable:
                yield await self.execute(turn, item)
            return
        gate = asyncio.Semaphore(self._settings.tools_max_concurrency)

        async def limited(item: Planned) -> Finished:
            async with gate:
                return await self.execute(turn, item)

        tasks = [asyncio.ensure_future(limited(item)) for item in runnable]
        try:
            for done in asyncio.as_completed(tasks):
                yield await done
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, turn: Turn, item: Planned) -> Finished:
        definition = item.definition
        if definition is None:  # pragma: no cover - only validated calls are runnable
            return Finished(planned=item, status="rejected", error_code="unknown_tool")
        async with self._engine.begin() as conn:
            claimed = await repo.claim(conn, item.invocation_id)
            if claimed is None:
                current = await repo.invocation(conn, turn.organization_id, item.invocation_id)
                return _finished_from_row(item, current)
            approval = None
            if definition.requires_approval:
                approvals = await repo.approvals_of(conn, [item.invocation_id])
                approval = approvals[0] if approvals else None
        # Checked again now, with permissions read from the database at this moment.
        principal = await self._fresh(turn.principal)
        switches = await self.switches(turn.organization_id)
        refusal: tuple[str, str, str] | None = None
        if not _enabled(definition, switches):
            refusal = ("tool_disabled", "This tool is switched off.", "TOOL_INVOCATION_REJECTED")
        elif principal is None or not _permitted(definition, principal):
            refusal = (
                "permission_denied",
                "You do not have permission to use this tool.",
                "TOOL_PERMISSION_DENIED",
            )
        elif definition.requires_approval and not _approved(
            approval, arguments_hash(definition, item.arguments), turn.principal.user_id
        ):
            refusal = ("not_approved", "This action was not approved.", "TOOL_INVOCATION_REJECTED")
        if refusal is not None or principal is None:
            code, message, action = refusal or ("permission_denied", "", "TOOL_PERMISSION_DENIED")
            await self._store(
                item.invocation_id, "rejected", error_code=code, error_message=message
            )
            await self._audit_event(
                action, turn, item.invocation_id, {"tool_id": definition.id, "error_code": code}
            )
            return Finished(planned=item, status="rejected", error_code=code, error_message=message)

        context = ToolContext(
            principal=principal,
            conversation_id=turn.conversation_id,
            message_id=turn.message_id,
            invocation_id=item.invocation_id,
            request_id=turn.request_id,
        )
        started = self._clock()
        attempts = 0
        status, code, message = "failed", "tool_error", "The tool failed."
        output: ToolOutput | None = None
        try:
            while True:
                attempts += 1
                try:
                    async with asyncio.timeout(definition.timeout_seconds):
                        output = await definition.handler(context, dict(item.arguments))
                    break
                except ToolFailure as failure:
                    status, code, message = "failed", failure.code, failure.message
                    if not (
                        failure.retryable
                        and definition.read_only
                        and attempts <= definition.max_retries
                    ):
                        break
                except TimeoutError:
                    status, code, message = "timed_out", "timeout", "The tool took too long."
                    if not (definition.read_only and attempts <= definition.max_retries):
                        break
        except asyncio.CancelledError:
            await asyncio.shield(
                self._store(
                    item.invocation_id,
                    "cancelled",
                    error_code="cancelled",
                    error_message="Stopped before the tool finished.",
                    duration_ms=_ms(started),
                )
            )
            raise
        except NexaError as exc:
            # A service the handler used refused: report its safe message, nothing more.
            status, code, message = "failed", exc.error_code.value.lower(), exc.message[:300]
        except Exception as exc:
            log.error("tools.handler.bug", tool_id=definition.id, error=type(exc).__name__)
            status, code, message = "failed", "tool_error", "The tool failed."

        data: dict[str, Any] | None = None
        truncated = False
        sources: tuple[dict[str, Any], ...] = ()
        if output is not None:
            try:
                data, truncated = validate_output(definition, output.data)
                size = len(canonical(data).encode())
                if turn.output_bytes + size > self._settings.tools_max_total_output_bytes:
                    raise OutputRejected(
                        "output_budget", "The tools have returned as much as one answer can use."
                    )
                turn.output_bytes += size
                status, code, message = "succeeded", "", ""
                sources = tuple(ref.record() for ref in output.sources)
            except OutputRejected as exc:
                data = None
                status, code, message = "failed", exc.code, exc.message
        duration = _ms(started)
        await self._store(
            item.invocation_id,
            status,
            result=data,
            result_truncated=truncated,
            error_code=code,
            error_message=message,
            sources=list(sources),
            duration_ms=duration,
            attempts=attempts,
        )
        log.info(
            "tools.invocation.completed",
            tool_id=definition.id,
            tool_version=definition.version,
            status=status,
            error_code=code or None,
            duration_ms=duration,
            attempts=attempts,
            truncated=truncated,
        )
        if status != "succeeded" or definition.audit == "all":
            await self._audit_event(
                "TOOL_EXECUTED" if status == "succeeded" else "TOOL_EXECUTION_FAILED",
                turn,
                item.invocation_id,
                {
                    "tool_id": definition.id,
                    "tool_version": definition.version,
                    "status": status,
                    "error_code": code or None,
                    "duration_ms": duration,
                },
            )
        return Finished(
            planned=item,
            status=status,
            data=data,
            truncated=truncated,
            error_code=code,
            error_message=message,
            duration_ms=duration,
            sources=sources,
        )

    async def decline(self, turn: Turn, item: Planned) -> Finished:
        """The user rejected the action: it never runs, and the model is told so."""
        await self._store(
            item.invocation_id,
            "rejected",
            from_states=("approved", "awaiting_approval", "validated"),
            error_code=DECLINED,
            error_message="The user declined this action.",
        )
        return Finished(
            planned=item,
            status="rejected",
            error_code=DECLINED,
            error_message="The user declined this action.",
        )

    async def audit_decision(
        self,
        principal: Principal,
        *,
        invocation_id: UUID,
        approval_id: UUID,
        tool_id: str,
        decision: str,
        conversation_id: UUID,
        message_id: UUID,
        request_id: str | None,
    ) -> None:
        log.info("tools.approval.decided", tool_id=tool_id, decision=decision)
        await self._audit.record(
            "TOOL_APPROVAL_DECIDED",
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            resource_type="tool_invocation",
            resource_id=str(invocation_id),
            request_id=request_id,
            metadata={
                "tool_id": tool_id,
                "approval_id": str(approval_id),
                "decision": decision,
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
            },
        )

    # --- Internals -------------------------------------------------------------------------

    async def _fresh(self, principal: Principal) -> Principal | None:
        """The principal re-read from the database, or None when the membership is gone."""
        try:
            async with self._engine.connect() as conn:
                fresh: Principal = await self._reload(conn, principal)
                return fresh
        except PermissionDenied:
            return None

    async def _store(
        self,
        invocation_id: UUID,
        status: str,
        *,
        from_states: tuple[str, ...] = ("executing",),
        **values: Any,
    ) -> None:
        async with self._engine.begin() as conn:
            await repo.finish(conn, invocation_id, from_states=from_states, status=status, **values)

    async def _audit_event(
        self, action: str, turn: Turn, invocation_id: UUID, metadata: dict[str, Any]
    ) -> None:
        await self._audit.record(
            action,
            organization_id=turn.organization_id,
            actor_id=turn.principal.user_id,
            resource_type="tool_invocation",
            resource_id=str(invocation_id),
            request_id=turn.request_id,
            metadata={
                **metadata,
                "conversation_id": str(turn.conversation_id),
                "message_id": str(turn.message_id),
            },
        )


def _enabled(definition: ToolDefinition, switches: dict[str, tuple[bool, bool]]) -> bool:
    platform, organization = switches.get(definition.id, (True, True))
    return definition.enabled and platform and organization


def _permitted(definition: ToolDefinition, principal: Principal) -> bool:
    return principal.has(EXECUTE_PERMISSION) and definition.allowed_for(principal)


def _approved(approval: Any, digest: str, user_id: UUID) -> bool:
    """Approved by the requesting user, for exactly these arguments of this version of the
    tool (`digest` is recomputed from what is about to run), before it expired."""
    return (
        approval is not None
        and approval.status == "approved"
        and approval.arguments_hash == digest
        and approval.decided_by_id == user_id
        and approval.decided_at is not None
        and approval.decided_at <= approval.expires_at
    )


def _idempotency_key(message_id: UUID, step: int, position: int, call: ToolCallPart) -> str:
    raw = f"{message_id}|{step}|{position}|{call.id}|{call.name}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _planned_from_row(row: Any, call: ToolCallPart, definition: ToolDefinition | None) -> Planned:
    return Planned(
        invocation_id=row.id,
        step=row.step,
        position=row.position,
        call=call,
        tool_id=row.tool_id,
        definition=definition,
        arguments=dict(row.arguments or {}),
        arguments_hash=row.arguments_hash,
        status=row.status,
        summary=definition.summary(row.arguments or {}) if definition else row.tool_id,
        error_code=row.error_code,
        error_message=row.error_message,
    )


def _finished_from_row(item: Planned, row: Any) -> Finished:
    if row is None or row.status not in repo.FINAL:
        # Claimed elsewhere and still running there: report it as not run here.
        return Finished(
            planned=item,
            status="cancelled",
            error_code="not_run",
            error_message="This call was handled by another request.",
        )
    return Finished(
        planned=item,
        status=row.status,
        data=row.result,
        truncated=row.result_truncated,
        error_code=row.error_code,
        error_message=row.error_message,
        duration_ms=row.duration_ms,
        sources=tuple(row.sources or ()),
    )


finished_from_row = _finished_from_row
planned_from_row = _planned_from_row


def _ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))

"""The chat engine: conversation management and the reply-generation lifecycle.

    user submits ──► authorize ──► validate ──► store user message + pending assistant message
                                                  (one transaction; the conversation is locked
                                                   for exactly one generation at a time)
    ──► build context ──► AI Gateway stream ──► relay deltas, flush partial text
    ──► completed | failed | cancelled, usage copied from the gateway, lock released

A document-grounded message (Phase 08, docs/architecture/rag.md) has its scope authorized
before anything is stored; its reply first retrieves passages (`sources` event), answers
without calling the model when the documents have nothing, and stores which passages it
cited, checked against the ones it was given.

A reply may use tools (Phase 10, docs/architecture/tools.md): the model is offered the tools
the user may use, and the reply becomes a bounded loop of model calls and tool calls, each
call checked and run by the tool executor. A call that needs the user's approval pauses the
reply (`awaiting_approval`); the decision continues the same reply.

Everything here is scoped by the caller's principal (organization from the token, ownership
checked with the shared policy). The gateway does authorization, quotas and metering for the
model call itself; this service only persists what happened to it.

A transaction that writes both a conversation and its messages locks the conversation row
first (sending, regenerating, deleting and finishing a reply all do), so two of them cannot
deadlock.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_ai.context import AIRequestContext
from nexa_ai.cost import micros_to_usd, usd_to_micros
from nexa_ai.errors import (
    AIContentRejected,
    AIError,
    AIInternalError,
    AIModelUnavailable,
    AIProviderError,
)
from nexa_ai.gateway import Gateway, GatewayStream, KeepAlive
from nexa_ai.types import (
    ChatRequest,
    Feature,
    Message,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
    UsageEvent,
)
from nexa_ai.types import MessageDelta as GatewayDelta
from nexa_ai.types import MessageEnd as GatewayEnd
from nexa_ai.types import MessageStart as GatewayStart
from nexa_ai.types import StreamError as GatewayError
from nexa_common.access import Principal, ResourceRef, authorize, require
from nexa_common.errors import Conflict, ErrorCode, NexaError, NotFound
from nexa_common.ids import new_id
from nexa_common.logs import get_logger
from nexa_runtime.chat import repository as repo
from nexa_runtime.chat.cancellation import CancelRegistry
from nexa_runtime.chat.context import ContextPolicy, HistoryItem, MemoryItem, build_context
from nexa_runtime.chat.events import (
    ApprovalRequired,
    ChatEvent,
    GenerationError,
    MessageCancelled,
    MessageComplete,
    MessageStart,
    SourcesFound,
    TextDelta,
    ToolCallEvent,
    ToolResultEvent,
    UsageReport,
)
from nexa_runtime.chat.prompts import DEFAULT_PROMPT_VERSION, tools_message
from nexa_runtime.chat.schemas import (
    CancelResult,
    ConversationOut,
    CreateConversation,
    Exchange,
    MessageOut,
    Regenerate,
    SendMessage,
    UpdateConversation,
)
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.memory.service import MemoryEngine
from nexa_runtime.rag.errors import GroundingDisabled
from nexa_runtime.rag.evidence import (
    NO_EVIDENCE_ANSWER,
    NO_TERMS_ANSWER,
    CitationCheck,
    check_citations,
    strip_citations,
)
from nexa_runtime.rag.schemas import GroundingRequest, SourceOut
from nexa_runtime.rag.service import Grounded, RagEngine, Scope, failed_record
from nexa_runtime.ratelimit import Limiter
from nexa_runtime.tools import repository as tools_repo
from nexa_runtime.tools.activity import activity_for
from nexa_runtime.tools.errors import (
    ApprovalClosed,
    ApprovalExpired,
    ApprovalPending,
    ApprovalStale,
)
from nexa_runtime.tools.executor import (
    DECLINED,
    Finished,
    Planned,
    ToolExecutor,
    Turn,
    finished_from_row,
    planned_from_row,
    result_part,
)
from nexa_runtime.tools.schemas import ApprovalDecision
from nexa_runtime.tools.validation import redacted

log = get_logger("nexa.runtime.chat")

_FLUSH_SECONDS = 2.0
# How often the generation loop looks at the stop flag while waiting for the next chunk.
_STOP_CHECK_SECONDS = 0.25
# Used for the context budget when the catalog does not know the model's window.
_FALLBACK_CONTEXT_WINDOW = 200_000


class GenerationInProgress(NexaError):
    status = 409
    code = ErrorCode.GENERATION_IN_PROGRESS


class EmptyResponse(AIError):
    """The model finished without producing any text."""

    status = 502
    code = ErrorCode.AI_EMPTY_RESPONSE
    retryable = True


@dataclass(frozen=True, slots=True)
class _End:
    pass


@dataclass(frozen=True, slots=True)
class _Failure:
    error: BaseException


_END = _End()


@dataclass(frozen=True, slots=True)
class Generation:
    """Everything a reply needs once the messages are stored."""

    conversation: repo.ConversationRow
    user_message: repo.MessageRow
    assistant_message: repo.MessageRow
    principal: Principal
    request_id: str | None
    # Set for a document-grounded reply: the scope, authorized when the message was sent.
    scope: Scope | None = None
    # "auto": the model may use the tools this user may use; "none": no tools (Phase 10).
    tools: str = "auto"
    # Set when a paused reply continues after the user decided on an action.
    resume: "Resume | None" = None


@dataclass(frozen=True, slots=True)
class Resume:
    approval_id: UUID
    decision: str


@dataclass
class _Relay:
    """The gateway stream being read and the task reading it, for cleanup."""

    events: GatewayStream | None = None
    pump: asyncio.Task[None] | None = None


@dataclass
class _StepOutcome:
    finished: bool = False
    text: str = ""
    tool_calls: list[ToolCallPart] = field(default_factory=list)


@dataclass
class _Progress:
    started: float
    grounded: Grounded | None = None
    parts: list[str] = field(default_factory=list)
    alias: str = ""
    provider: str = ""
    provider_model: str = ""
    # The whole reply's usage (every model call so far); `step_usage` is the current call's.
    usage: Usage | None = None
    prior_usage: Usage | None = None
    step_usage: Usage | None = None
    usage_event_id: UUID | None = None
    finish_reason: str = ""
    ttft_ms: int | None = None
    ended: bool = False
    # Tool use (Phase 10): each model call's text and tool calls; stored as `tool_trace`.
    steps: list[dict[str, Any]] = field(default_factory=list)
    tool_limit_reached: bool = False
    # Time already spent on a reply that paused for approval and now continues.
    latency_base: int = 0

    @property
    def text(self) -> str:
        return "".join(self.parts)

    @property
    def call_finished(self) -> bool:
        """The gateway has metered the current model call: only its closing event is still
        to come."""
        return self.ended or self.step_usage is not None

    def begin_step(self) -> None:
        self.prior_usage = self.usage
        self.step_usage = None
        self.ended = False
        self.finish_reason = ""

    def meter(self, usage: Usage) -> None:
        self.step_usage = usage
        self.usage = _add_usage(self.prior_usage, usage)

    def elapsed_ms(self) -> int:
        return self.latency_base + _elapsed_ms(self.started)

    def resume_from(self, row: Any) -> None:
        """Continue a paused reply: its text, usage, timing and tool steps so far."""
        if row.content:
            self.parts = [row.content]
        if row.input_tokens or row.output_tokens:
            self.usage = Usage(
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                total_tokens=row.input_tokens + row.output_tokens,
                cost_usd=micros_to_usd(row.cost_micros) if row.cost_micros is not None else None,
            )
        trace = getattr(row, "tool_trace", None) or {}
        self.steps = list(trace.get("steps", []))
        self.ttft_ms = row.ttft_ms
        self.latency_base = row.latency_ms or 0
        self.alias, self.provider, self.provider_model = row.model, row.provider, row.provider_model

    def add_text(self, text: str) -> str:
        """Append model output; returns what was kept. PostgreSQL cannot store NUL, so it is
        dropped here, before the client sees it: what was streamed is what is saved."""
        text = text.replace("\x00", "")
        if text:
            self.parts.append(text)
        return text


class ChatService:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        gateway: Gateway,
        cancel: CancelRegistry,
        settings: RuntimeSettings,
        limiter: Limiter | None = None,
        memory: MemoryEngine | None = None,
        rag: RagEngine | None = None,
        tools: ToolExecutor | None = None,
    ) -> None:
        self._engine = engine
        self._memory = memory
        self._rag = rag
        self._tools = tools
        self._gateway = gateway
        self._cancel = cancel
        self._limiter = limiter or Limiter(None)
        self._settings = settings
        self._policy = ContextPolicy(
            max_messages=settings.chat_context_max_messages,
            max_tokens=settings.chat_context_max_tokens,
            memory_budget=settings.memory_context_max_tokens,
            summary_budget=settings.memory_summary_max_tokens,
        )
        self._background: set[asyncio.Task[Any]] = set()

    # --- Conversations ---------------------------------------------------------------------

    async def create(self, principal: Principal, body: CreateConversation) -> ConversationOut:
        require(principal, "conversation.create")
        await self._limiter.hit(
            "conversations_create",
            str(principal.user_id),
            limit=self._settings.chat_conversations_per_minute,
            window_seconds=60,
        )
        await self._check_model(principal, body.model)
        async with self._engine.begin() as conn:
            row = await repo.create_conversation(
                conn,
                organization_id=_organization(principal),
                owner_id=principal.user_id,
                title=body.title.strip(),
                model=body.model,
                prompt_version=DEFAULT_PROMPT_VERSION,
            )
        return self._out(row)

    async def list_conversations(
        self, principal: Principal, *, status: str, query: str, limit: int, cursor: str | None
    ) -> tuple[list[ConversationOut], str | None]:
        require(principal, "conversation.view")
        async with self._engine.connect() as conn:
            rows, next_cursor = await repo.list_conversations(
                conn,
                _organization(principal),
                principal.user_id,
                status=status,
                query=query.strip(),
                limit=limit,
                cursor=cursor,
            )
        return [self._out(row) for row in rows], next_cursor

    async def get(self, principal: Principal, conversation_id: UUID) -> ConversationOut:
        async with self._engine.connect() as conn:
            row = await self._load(conn, principal, conversation_id, "view")
        return self._out(row)

    async def update(
        self, principal: Principal, conversation_id: UUID, body: UpdateConversation
    ) -> ConversationOut:
        if body.model:
            await self._check_model(principal, body.model)
        values: dict[str, Any] = {}
        if body.title is not None:
            values["title"] = body.title.strip()
        if body.model is not None:
            values["model"] = body.model
        if body.status is not None:
            values["status"] = body.status
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, conversation_id, "update", for_update=True)
            if values.get("title") == "":
                # An empty title means "name it automatically again": from the first message.
                first = await repo.message_at(conn, row.id, 1)
                values["title"] = self._title_from(first.content) if first else ""
            if values:
                row = await repo.update_conversation(conn, row.id, **values)
        return self._out(row)

    async def delete(self, principal: Principal, conversation_id: UUID) -> None:
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, conversation_id, "delete", for_update=True)
            if self._generating(row):
                raise GenerationInProgress("Stop the reply being generated before deleting.")
            if self._memory is not None:
                await self._memory.forget_conversation(conn, row.id)
            await repo.delete_conversation(conn, row.id)

    async def list_messages(
        self, principal: Principal, conversation_id: UUID, *, limit: int, before: int | None
    ) -> tuple[list[MessageOut], int | None]:
        async with self._engine.connect() as conn:
            row = await self._load(conn, principal, conversation_id, "view")
            rows, next_before = await repo.list_messages(conn, row.id, limit=limit, before=before)
            messages = await self._with_sources(
                conn, principal, [MessageOut.from_row(m) for m in rows]
            )
            messages = await self._with_tools(conn, principal, messages)
        return messages, next_before

    async def message_sources(
        self, principal: Principal, conversation_id: UUID, message_id: UUID
    ) -> list[SourceOut]:
        """The sources of one grounded reply, with whole passages rather than excerpts."""
        async with self._engine.connect() as conn:
            row = await self._load(conn, principal, conversation_id, "view")
            message = await repo.message_by_id(conn, row.id, message_id)
            if message is None:
                raise NotFound
            if message.grounding is None or self._rag is None:
                return []
            found = await self._rag.sources_for(conn, principal, [message.id], detail=True)
        return found.get(message.id, [])

    # --- Sending and regenerating ------------------------------------------------------------

    async def send(
        self,
        principal: Principal,
        conversation_id: UUID,
        body: SendMessage,
        *,
        request_id: str | None,
    ) -> Generation | Exchange:
        """Store the user message and a pending reply, or return the earlier exchange when
        the client sends the same `client_message_id` again."""
        require(principal, "ai.chat")
        await self._check_model(principal, body.model)
        scope: Scope | None = None
        async with self._engine.begin() as conn:
            conversation = await self._load(
                conn, principal, conversation_id, "update", for_update=True
            )
            if conversation.status != "active":
                raise Conflict("This conversation is archived.")
            if body.client_message_id:
                earlier = await repo.message_by_client_id(
                    conn, conversation.id, body.client_message_id
                )
                if earlier is not None:
                    return await self._replay(conn, principal, conversation, earlier)
            if body.grounding is not None:
                # Authorized before anything is stored (a bad selection stores no message),
                # on this transaction's connection rather than a second pooled one.
                scope = await self._resolve_grounding(
                    principal, body.grounding, body.content, conn=conn
                )
            await self._close_paused(conn, conversation.id)
            assistant_id = new_id()
            await self._claim(conn, conversation, assistant_id)
            sequence = await repo.next_sequence(conn, conversation.id)
            model = body.model or conversation.model
            user = await repo.insert_message(
                conn,
                organization_id=conversation.organization_id,
                conversation_id=conversation.id,
                sequence=sequence,
                role="user",
                content=body.content,
                status="completed",
                client_message_id=body.client_message_id,
            )
            assistant = await repo.insert_message(
                conn,
                organization_id=conversation.organization_id,
                conversation_id=conversation.id,
                sequence=sequence + 1,
                role="assistant",
                content="",
                status="pending",
                model=model,
                message_id=assistant_id,
                grounding=scope.record() if scope is not None else None,
            )
            changes: dict[str, Any] = {
                "message_count": conversation.message_count + 2,
                "last_message_at": user.created_at,
            }
            if body.model and body.model != conversation.model:
                changes["model"] = body.model
            if not conversation.title:
                changes["title"] = self._title_from(body.content)
            conversation = await repo.update_conversation(conn, conversation.id, **changes)
        return Generation(
            conversation, user, assistant, principal, request_id, scope, tools=body.tools
        )

    async def _close_paused(self, conn: Any, conversation_id: UUID) -> None:
        """A reply paused for approval blocks a new message until the user decides; once the
        approval has expired, the paused reply is closed (cancelled) and the conversation
        goes on."""
        last = await repo.last_message(conn, conversation_id)
        if last is None or last.status != "awaiting_approval":
            return
        rows = await tools_repo.invocations_of(conn, [last.id])
        approvals = await tools_repo.approvals_of(conn, [row.id for row in rows])
        if any(a.status == "pending" and a.expires_at > repo.now() for a in approvals):
            raise ApprovalPending()
        await tools_repo.close_pending_approvals(conn, last.id, status="expired")
        await repo.update_message(
            conn,
            last.id,
            status="cancelled",
            error_code=ErrorCode.TOOL_APPROVAL_EXPIRED.value,
            completed_at=repo.now(),
        )

    async def regenerate(
        self,
        principal: Principal,
        conversation_id: UUID,
        body: Regenerate,
        *,
        request_id: str | None,
    ) -> Generation:
        """Answer the last user message again. The previous reply, whatever its state, is
        replaced; its usage stays in the ledger."""
        require(principal, "ai.chat")
        await self._check_model(principal, body.model)
        async with self._engine.begin() as conn:
            conversation = await self._load(
                conn, principal, conversation_id, "update", for_update=True
            )
            if conversation.status != "active":
                raise Conflict("This conversation is archived.")
            last = await repo.last_message(conn, conversation.id)
            if last is None:
                raise Conflict("There is nothing to regenerate yet.")
            count = conversation.message_count
            if last.role == "assistant":
                if last.status in ("pending", "streaming") and self._generating(conversation):
                    raise GenerationInProgress()
                user = await repo.message_at(conn, conversation.id, last.sequence - 1)
                if user is None or user.role != "user":
                    raise Conflict("There is nothing to regenerate.")
            else:
                user = last
                count += 1  # the reply that was never stored
            # A grounded reply is regenerated from the same scope, authorized again now: a
            # document unshared since then is not searched.
            scope: Scope | None = None
            request = Scope.request_from_record(
                last.grounding if last.role == "assistant" else None
            )
            if request is not None:
                scope = await self._resolve_grounding(principal, request, user.content, conn=conn)
            assistant_id = new_id()
            await self._claim(conn, conversation, assistant_id)
            if last.role == "assistant":
                await repo.delete_message(conn, last.id)
            model = body.model or conversation.model
            assistant = await repo.insert_message(
                conn,
                organization_id=conversation.organization_id,
                conversation_id=conversation.id,
                sequence=await repo.next_sequence(conn, conversation.id),
                role="assistant",
                content="",
                status="pending",
                model=model,
                message_id=assistant_id,
                grounding=scope.record() if scope is not None else None,
            )
            changes: dict[str, Any] = {"message_count": count}
            if body.model and body.model != conversation.model:
                changes["model"] = body.model
            conversation = await repo.update_conversation(conn, conversation.id, **changes)
        return Generation(conversation, user, assistant, principal, request_id, scope)

    async def cancel(self, principal: Principal, conversation_id: UUID) -> CancelResult:
        async with self._engine.connect() as conn:
            row = await self._load(conn, principal, conversation_id, "update")
        if not self._generating(row):
            return CancelResult(cancelled=False, message_id=None)
        await self._cancel.request(row.active_generation_id)
        log.info(
            "chat.cancel.requested",
            conversation_id=str(row.id),
            message_id=str(row.active_generation_id),
        )
        return CancelResult(cancelled=True, message_id=row.active_generation_id)

    # --- Generation --------------------------------------------------------------------------

    def stream(self, gen: Generation) -> "GenerationStream":
        """The `nexa.chat.v1` event stream for one generation. Exactly one terminal event.
        The caller must close it (see `GenerationStream`)."""
        return GenerationStream(self, gen)

    async def _events(self, gen: Generation) -> AsyncGenerator[ChatEvent | KeepAlive]:
        """The generation itself: one model call, or, when tools are used, a bounded loop of
        model calls and tool calls (docs/architecture/tools.md#orchestration). Each gateway
        stream is read in a separate task, so a stop request is noticed within a quarter of a
        second even while the model is thinking or a tool is running."""
        assistant_id: UUID = gen.assistant_message.id
        progress = _Progress(started=time.perf_counter())
        if gen.resume is not None:
            progress.resume_from(gen.assistant_message)
        stop = self._cancel.start(assistant_id)
        relay = _Relay()
        done = False
        try:
            assistant_out = MessageOut.from_row(gen.assistant_message)
            if gen.resume is not None:
                async with self._engine.connect() as conn:
                    [assistant_out] = await self._with_tools(conn, gen.principal, [assistant_out])
            yield MessageStart(
                conversation_id=gen.conversation.id,
                request_id=gen.request_id,
                user_message=MessageOut.from_row(gen.user_message),
                assistant_message=assistant_out,
            )
            if gen.scope is not None:
                progress.grounded = await self._ground(gen)
                yield SourcesFound(
                    message_id=assistant_id,
                    searched_documents=progress.grounded.searched_documents,
                    sources=self._sources_of(progress.grounded, None),
                )
                if not progress.grounded.evidence.passages:
                    # Nothing to answer from: say so instead of letting the model guess.
                    done = True
                    out, ok = await self._answer_without_evidence(gen, progress)
                    if ok:
                        yield TextDelta(message_id=assistant_id, text=out.content)
                        yield MessageComplete(message=out)
                    else:
                        yield _error_event(AIInternalError(), out, gen.request_id)
                    return
            request, ctx = await self._prepare(gen, progress.grounded)
            turn = await self._tool_turn(gen, request, ctx)
            if turn is not None:
                request = _offering(request, turn)
            # Marked before the gateway call: between opening the gateway stream (which holds
            # a quota reservation) and handing it to the pump nothing is awaited, so it cannot
            # be abandoned unclosed.
            await self._mark_streaming(assistant_id)
            messages = list(request.messages)
            if gen.resume is not None and turn is not None:
                async for resumed in self._resume(gen, progress, turn, messages):
                    yield resumed
            settings = self._settings
            deadline = time.monotonic() + settings.tools_turn_timeout_seconds
            while True:
                step = len(progress.steps)
                limited = turn is not None and (
                    step >= settings.tools_max_iterations or time.monotonic() > deadline
                )
                if limited:
                    progress.tool_limit_reached = True
                outcome = _StepOutcome()
                step_request = request.model_copy(
                    update={
                        "messages": messages,
                        "tool_choice": "none" if limited else request.tool_choice,
                    }
                )
                async for event in self._model_step(
                    gen, progress, step_request, ctx, stop, relay, outcome
                ):
                    yield event
                if outcome.finished:
                    done = True
                    return
                calls = outcome.tool_calls if turn is not None and not limited else []
                if not calls:
                    break
                progress.steps.append(
                    {"text": outcome.text, "calls": [_traced(c, turn) for c in calls]}
                )
                tools = self._tools
                if tools is None or turn is None:  # pragma: no cover - calls need a turn
                    raise AIInternalError()
                planned = await tools.plan(turn, step, calls)
                for item in planned:
                    yield _tool_call_event(assistant_id, item)
                waiting = [item for item in planned if item.status == "awaiting_approval"]
                results: dict[int, ToolResultPart] = {}
                stopped = False
                async for finished in self._run_tools(
                    turn, [item for item in planned if item.status != "awaiting_approval"], stop
                ):
                    if finished is None:
                        stopped = True
                        break
                    results[finished.planned.position] = finished.result_part()
                    yield _tool_result_event(assistant_id, finished)
                if stopped:
                    done = True
                    out, ok = await self._settle(gen, progress, status="cancelled")
                    yield (
                        MessageCancelled(message=out)
                        if ok
                        else _error_event(AIInternalError(), out, gen.request_id)
                    )
                    return
                if waiting:
                    # Paused: the user decides; the decision continues this same reply.
                    done = True
                    out, ok = await self._settle(gen, progress, status="awaiting_approval")
                    approval = next(
                        (
                            activity.approval
                            for activity in out.tools
                            if activity.approval is not None
                            and activity.invocation_id == waiting[0].invocation_id
                        ),
                        None,
                    )
                    if ok and approval is not None:
                        yield ApprovalRequired(message=out, approval=approval)
                    else:
                        yield _error_event(AIInternalError(), out, gen.request_id)
                    return
                messages.extend(
                    _step_messages(outcome.text, calls, [results[p.position] for p in planned])
                )
            if not progress.ended:
                raise AIProviderError("The provider ended the stream without finishing.")
            empty = _empty_answer_error(progress)
            done = True
            if empty is not None:
                out, _ = await self._settle(gen, progress, status="failed", error=empty)
                yield _error_event(empty, out, gen.request_id)
                return
            out, ok = await self._settle(gen, progress, status="completed")
            if ok:
                yield MessageComplete(message=out)
            else:
                yield _error_event(AIInternalError(), out, gen.request_id)
        except NexaError as exc:
            if not done:
                done = True
                _take_usage(progress, relay.events)
                out, _ = await self._settle(gen, progress, status="failed", error=exc)
                yield _error_event(exc, out, gen.request_id)
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as exc:
            log.error("chat.generation.bug", error=type(exc).__name__, message_id=str(assistant_id))
            if not done:
                done = True
                _take_usage(progress, relay.events)
                internal = AIInternalError()
                out, _ = await self._settle(gen, progress, status="failed", error=internal)
                yield _error_event(internal, out, gen.request_id)
        finally:
            self._cancel.finish(assistant_id)
            if not done:
                # The client went away, or the server is shutting down.
                self._abandon(gen, progress, relay.pump, relay.events)
            elif relay.pump is not None and not relay.pump.done():
                # Closes the gateway stream, which closes the provider's and records usage.
                relay.pump.cancel()
                self._track(relay.pump)

    async def _model_step(
        self,
        gen: Generation,
        progress: "_Progress",
        request: ChatRequest,
        ctx: AIRequestContext,
        stop: asyncio.Event,
        relay: "_Relay",
        outcome: "_StepOutcome",
    ) -> AsyncGenerator[ChatEvent | KeepAlive]:
        """One model call, relayed. Ends normally when the model has finished (its text and
        tool calls are in `outcome`), or with `outcome.finished` set once a terminal event
        (stopped, failed) has been yielded and the reply stored."""
        assistant_id: UUID = gen.assistant_message.id
        if progress.steps:
            async with self._engine.begin() as conn:
                await repo.renew_generation(conn, gen.conversation.id, assistant_id)
        progress.begin_step()
        if relay.pump is not None and not relay.pump.done():
            await _stop(relay.pump)
        events = await self._gateway.open_stream(request, ctx)
        relay.events = events
        progress.usage_event_id = events.call_id
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
        # Started eagerly, so the pump owns the gateway stream (and closes it on the way
        # out) even when a stop is already waiting. (Python 3.13 needs the loop passed.)
        relay.pump = asyncio.Task(
            _pump(events, queue), loop=asyncio.get_running_loop(), eager_start=True
        )
        last_flush = time.monotonic()
        step_text: list[str] = []
        while True:
            # A stop that arrives once the model has finished changes nothing.
            if stop.is_set() and not progress.call_finished:
                await _stop(relay.pump)
                relay.pump = None
                await _close(events)
                _take_usage(progress, events)
                outcome.finished = True
                out, ok = await self._settle(gen, progress, status="cancelled")
                if ok:
                    yield MessageCancelled(message=out)
                else:
                    yield _error_event(AIInternalError(), out, gen.request_id)
                return
            try:
                async with asyncio.timeout(_STOP_CHECK_SECONDS):
                    item = await queue.get()
            except TimeoutError:
                continue
            if isinstance(item, _End):
                break
            if isinstance(item, _Failure):
                raise item.error
            if isinstance(item, KeepAlive):
                yield item
            elif isinstance(item, GatewayStart):
                progress.alias = item.model
                progress.provider = item.provider
                progress.provider_model = item.provider_model
            elif isinstance(item, GatewayDelta):
                if not step_text and progress.text and not progress.text[-1].isspace():
                    # A later step's text starts a new paragraph after the earlier text.
                    yield TextDelta(message_id=assistant_id, text=progress.add_text("\n\n"))
                text = progress.add_text(item.text)
                if not text:
                    continue
                step_text.append(text)
                if progress.ttft_ms is None:
                    progress.ttft_ms = progress.elapsed_ms()
                yield TextDelta(message_id=assistant_id, text=text)
                if time.monotonic() - last_flush > _FLUSH_SECONDS:
                    await self._flush(assistant_id, progress.text)
                    last_flush = time.monotonic()
            elif isinstance(item, UsageEvent):
                # Sent before the end of the stream, whether it succeeded or failed.
                progress.meter(item.usage)
                total = progress.usage or item.usage
                yield UsageReport(message_id=assistant_id, **total.model_dump())
            elif isinstance(item, GatewayEnd):
                progress.finish_reason = item.finish_reason.value
                progress.ended = True
                outcome.tool_calls = list(item.tool_calls)
            elif isinstance(item, GatewayError):
                outcome.finished = True
                out, _ = await self._settle(gen, progress, status="failed", error_code=item.code)
                yield GenerationError(
                    message_id=assistant_id,
                    code=item.code,
                    message=item.message,
                    retryable=item.retryable,
                    request_id=gen.request_id,
                    assistant_message=out,
                )
                return
        if not progress.ended:
            raise AIProviderError("The provider ended the stream without finishing.")
        outcome.text = "".join(step_text)

    async def _tool_turn(
        self, gen: Generation, request: ChatRequest, ctx: AIRequestContext
    ) -> "Turn | None":
        """The tools this reply may use, or None: grounded replies, replies sent with
        `tools: "none"`, models that cannot call tools and users who may not let AI use tools
        get none."""
        if self._tools is None or gen.scope is not None or gen.tools == "none":
            return None
        capabilities = await self._gateway.model_capabilities(ctx, request.model)
        offered = await self._tools.offer(gen.principal, capabilities)
        if not offered:
            return None
        turn = Turn(
            principal=gen.principal,
            conversation_id=gen.conversation.id,
            message_id=gen.assistant_message.id,
            request_id=gen.request_id,
            offered={d.id: d for d in offered},
        )
        if gen.resume is not None:
            async with self._engine.connect() as conn:
                turn.calls_made = await tools_repo.count_for_message(conn, turn.message_id)
        log.info(
            "chat.tools.offered",
            message_id=str(gen.assistant_message.id),
            tools=sorted(turn.offered),
        )
        return turn

    async def _run_tools(
        self, turn: "Turn", planned: list["Planned"], stop: asyncio.Event
    ) -> AsyncIterator["Finished | None"]:
        """The executor's results as calls end; None (then nothing more) when a stop request
        arrived: running calls are cancelled and recorded as such."""
        tools = self._tools
        if tools is None or not planned:
            return
        queue: asyncio.Queue[Any] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for finished in tools.run(turn, planned):
                    await queue.put(finished)
                await queue.put(_END)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await queue.put(_Failure(exc))

        task = asyncio.ensure_future(pump())
        try:
            while True:
                if stop.is_set():
                    await _stop(task)
                    yield None
                    return
                try:
                    async with asyncio.timeout(_STOP_CHECK_SECONDS):
                        item = await queue.get()
                except TimeoutError:
                    continue
                if isinstance(item, _End):
                    return
                if isinstance(item, _Failure):
                    raise item.error
                yield item
        finally:
            if not task.done():
                await _stop(task)

    async def _resume(
        self, gen: Generation, progress: "_Progress", turn: "Turn", messages: list[Message]
    ) -> AsyncGenerator[ChatEvent]:
        """Rebuild the paused reply's tool steps for the model, running the call the user has
        just approved (or recording that they declined it). Earlier calls are never re-run:
        their recorded results are replayed."""
        tools = self._tools
        if tools is None:  # pragma: no cover - a paused reply always had tools
            return
        async with self._engine.connect() as conn:
            rows = await tools_repo.invocations_of(conn, [gen.assistant_message.id])
        assistant_id: UUID = gen.assistant_message.id
        last = len(progress.steps) - 1
        for index, recorded in enumerate(progress.steps):
            calls = [ToolCallPart.model_validate(c) for c in recorded.get("calls", [])]
            for call in calls:
                turn.seen_call_ids.add(call.id)
            by_position = {row.position: row for row in rows if row.step == index}
            results: list[ToolResultPart] = []
            for position, call in enumerate(calls):
                row = by_position.get(position)
                definition = tools.registry.get(row.tool_id) if row is not None else None
                if row is None:
                    results.append(
                        result_part(call.id, call.name, "failed", None, False, "not_run", "")
                    )
                    continue
                item = planned_from_row(row, call, definition)
                if row.status == "approved" and definition is not None:
                    yield _tool_call_event(assistant_id, replace(item, status="executing"))
                    finished = await tools.execute(turn, item)
                    yield _tool_result_event(assistant_id, finished)
                else:
                    finished = finished_from_row(item, row)
                    if row.status not in tools_repo.FINAL:
                        finished = replace(finished, status="rejected", error_code="not_approved")
                    elif index == last and row.error_code == DECLINED:
                        yield _tool_result_event(assistant_id, finished)
                results.append(finished.result_part())
            messages.extend(_step_messages(recorded.get("text", ""), calls, results))

    async def decide(
        self,
        principal: Principal,
        conversation_id: UUID,
        approval_id: UUID,
        body: ApprovalDecision,
        *,
        request_id: str | None,
    ) -> Generation:
        """Record the user's decision on a paused action and claim the conversation to
        continue the reply. Only the requesting user may decide; the decision must name the
        exact action they were shown (`arguments_hash`), before it expires."""
        require(principal, "ai.chat")
        if self._tools is None:
            raise NotFound
        organization_id = _organization(principal)
        expired = False
        user: repo.MessageRow | None = None
        async with self._engine.begin() as conn:
            conversation = await self._load(
                conn, principal, conversation_id, "update", for_update=True
            )
            if conversation.status != "active":
                raise Conflict("This conversation is archived.")
            approval = await tools_repo.approval(
                conn, organization_id, approval_id, for_update=True
            )
            if approval is None or approval.requested_by_id != principal.user_id:
                raise NotFound
            invocation = await tools_repo.invocation(conn, organization_id, approval.invocation_id)
            if invocation is None or invocation.conversation_id != conversation.id:
                raise NotFound
            message = await repo.message_by_id(conn, conversation.id, invocation.message_id)
            if message is None or message.status != "awaiting_approval":
                raise ApprovalClosed()
            if approval.status != "pending":
                raise ApprovalClosed()
            if approval.expires_at <= repo.now():
                expired = True
                await tools_repo.close_pending_approvals(conn, message.id, status="expired")
                await repo.update_message(
                    conn,
                    message.id,
                    status="cancelled",
                    error_code=ErrorCode.TOOL_APPROVAL_EXPIRED.value,
                    completed_at=repo.now(),
                )
            elif body.arguments_hash != approval.arguments_hash:
                raise ApprovalStale()
            else:
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
                        error_message="The user declined this action.",
                    )
                await self._claim(conn, conversation, message.id)
                message = await repo.update_message(conn, message.id, status="streaming")
                user = await repo.message_at(conn, conversation.id, message.sequence - 1)
        if expired:
            raise ApprovalExpired()
        await self._tools.audit_decision(
            principal,
            invocation_id=invocation.id,
            approval_id=approval.id,
            tool_id=approval.tool_id,
            decision=body.decision,
            conversation_id=conversation.id,
            message_id=message.id,
            request_id=request_id,
        )
        if user is None:  # pragma: no cover - a reply always follows its question
            raise NotFound
        return Generation(
            conversation,
            user,
            message,
            principal,
            request_id,
            resume=Resume(approval_id=approval.id, decision=body.decision),
        )

    async def complete(self, gen: Generation) -> Exchange:
        """The non-streaming path: wait for the whole reply. A stop request ends it too; the
        reply is then returned as cancelled."""
        assistant_id: UUID = gen.assistant_message.id
        progress = _Progress(started=time.perf_counter())
        stop = self._cancel.start(assistant_id)
        no_evidence = False
        try:
            if gen.scope is not None:
                progress.grounded = await self._ground(gen)
                no_evidence = not progress.grounded.evidence.passages
            response = None
            if not no_evidence:
                request, ctx = await self._prepare(gen, progress.grounded)
                await self._mark_streaming(assistant_id)
                response = await _unless_stopped(self._gateway.chat(request, ctx), stop)
        except NexaError as exc:
            if isinstance(exc, AIError) and exc.call_id is not None:
                progress.usage_event_id = exc.call_id
                progress.usage = exc.usage
            await self._settle(gen, progress, status="failed", error=exc)
            raise
        except asyncio.CancelledError:
            self._abandon(gen, progress)
            raise
        except Exception as exc:
            log.error("chat.generation.bug", error=type(exc).__name__, message_id=str(assistant_id))
            internal = AIInternalError()
            await self._settle(gen, progress, status="failed", error=internal)
            raise internal from exc
        finally:
            self._cancel.finish(assistant_id)
        if no_evidence:
            out, ok = await self._answer_without_evidence(gen, progress)
        elif response is None:
            out, ok = await self._settle(gen, progress, status="cancelled")
        else:
            progress.add_text(response.content)
            progress.alias = response.model
            progress.provider = response.provider
            progress.provider_model = response.provider_model
            progress.usage = response.usage
            progress.usage_event_id = UUID(response.id)
            progress.finish_reason = response.finish_reason.value
            progress.ended = True
            empty = _empty_answer_error(progress)
            if empty is not None:
                await self._settle(gen, progress, status="failed", error=empty)
                raise empty
            out, ok = await self._settle(gen, progress, status="completed")
        if not ok:
            raise AIInternalError()
        return Exchange(user_message=MessageOut.from_row(gen.user_message), assistant_message=out)

    async def finish(self, gen: Generation) -> Exchange:
        """Run a generation's stream to its end and return the exchange it ended with: the
        JSON path of a continued reply (completed, failed, cancelled, or paused again)."""
        events = self.stream(gen)
        message: MessageOut | None = None
        try:
            async for event in events:
                if isinstance(event, MessageComplete | MessageCancelled | ApprovalRequired):
                    message = event.message
                elif isinstance(event, GenerationError):
                    message = event.assistant_message
        finally:
            await events.aclose()
        if message is None:  # pragma: no cover - every stream ends with a terminal event
            raise AIInternalError()
        return Exchange(
            user_message=MessageOut.from_row(gen.user_message), assistant_message=message
        )

    async def drain(self) -> None:
        while pending := [task for task in self._background if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    # --- Internals ---------------------------------------------------------------------------

    async def _load(
        self,
        conn: Any,
        principal: Principal,
        conversation_id: UUID,
        action: str,
        *,
        for_update: bool = False,
    ) -> repo.ConversationRow:
        row = await repo.get_conversation(
            conn, _organization(principal), conversation_id, for_update=for_update
        )
        if row is None:
            raise NotFound
        # Private to its owner: the shared policy answers 404 for everyone else.
        authorize(
            principal,
            action,
            ResourceRef(
                type="conversation",
                id=row.id,
                organization_id=row.organization_id,
                owner_id=row.owner_id,
                visibility="private",
            ),
        )
        return row

    async def _check_model(self, principal: Principal, model: str) -> None:
        if not model:
            return
        ctx = AIRequestContext.from_principal(principal, request_id=None)
        names = {alias.name for alias in await self._gateway.available_models(ctx)}
        if model not in names:
            raise AIModelUnavailable(f"Model '{model}' is not available.")

    async def _claim(self, conn: Any, conversation: repo.ConversationRow, message_id: UUID) -> None:
        """Claim the conversation (already locked) for a new reply."""
        if not await repo.acquire_generation(
            conn, conversation.id, message_id, stale_before=self._stale_before()
        ):
            raise GenerationInProgress()
        if conversation.active_generation_id is not None:
            # Taken over from a process that died mid-reply: that reply will never finish.
            await self._interrupted(conn, conversation.active_generation_id)

    async def _interrupted(self, conn: Any, message_id: UUID) -> repo.MessageRow | None:
        row = await repo.fail_message(
            conn, message_id, error_code=ErrorCode.GENERATION_INTERRUPTED.value
        )
        if row is not None:
            await tools_repo.cancel_open(conn, message_id)
            log.warning("chat.generation.interrupted", message_id=str(message_id))
        return row

    async def _replay(
        self,
        conn: Any,
        principal: Principal,
        conversation: repo.ConversationRow,
        user: repo.MessageRow,
    ) -> Exchange:
        reply = await repo.message_at(conn, conversation.id, user.sequence + 1)
        if reply is None or reply.role != "assistant":
            raise Conflict("The earlier message has no reply to return.")
        if reply.status in ("pending", "streaming"):
            if self._generating(conversation) and conversation.active_generation_id == reply.id:
                raise GenerationInProgress("The reply to that message is still being generated.")
            # Its generation died with its process: report that, not a reply pending forever.
            reply = await self._interrupted(conn, reply.id) or reply
        [answer] = await self._with_sources(conn, principal, [MessageOut.from_row(reply)])
        [answer] = await self._with_tools(conn, principal, [answer])
        return Exchange(user_message=MessageOut.from_row(user), assistant_message=answer)

    # --- Document grounding (Phase 08) -------------------------------------------------------

    async def _resolve_grounding(
        self, principal: Principal, request: GroundingRequest, question: str, *, conn: Any
    ) -> Scope:
        if self._rag is None:
            raise GroundingDisabled("Answers from documents are not available.")
        return await self._rag.resolve(principal, request, question=question, conn=conn)

    async def _ground(self, gen: Generation) -> Grounded:
        if self._rag is None or gen.scope is None:  # pragma: no cover - send() checked both
            raise GroundingDisabled("Answers from documents are not available.")
        return await self._rag.retrieve(
            gen.principal,
            gen.scope,
            question=gen.user_message.content,
            request_id=gen.request_id,
        )

    def _sources_of(self, grounded: Grounded, check: CitationCheck | None) -> list[SourceOut]:
        return self._rag.sources_from(grounded, check) if self._rag is not None else []

    async def _answer_without_evidence(
        self, gen: Generation, progress: _Progress
    ) -> tuple[MessageOut, bool]:
        """The reply when retrieval found nothing: a fixed statement, no model call."""
        grounded = progress.grounded
        no_terms = grounded is not None and grounded.terms == 0
        progress.add_text(NO_TERMS_ANSWER if no_terms else NO_EVIDENCE_ANSWER)
        progress.ttft_ms = progress.elapsed_ms()
        progress.finish_reason = "stop"
        progress.ended = True
        return await self._settle(gen, progress, status="completed")

    async def _with_sources(
        self, conn: Any, principal: Principal, messages: list[MessageOut]
    ) -> list[MessageOut]:
        """Attach the sources of grounded replies, re-authorized for this reader now."""
        grounded = [m.id for m in messages if m.grounding is not None]
        if not grounded or self._rag is None:
            return messages
        found = await self._rag.sources_for(conn, principal, grounded)
        return [
            m.model_copy(update={"sources": found.get(m.id, [])}) if m.grounding else m
            for m in messages
        ]

    async def _with_tools(
        self, conn: Any, principal: Principal, messages: list[MessageOut]
    ) -> list[MessageOut]:
        """Attach each reply's tool activity, with document results re-authorized now."""
        if self._tools is None:
            return messages
        replies = [m.id for m in messages if m.role == "assistant"]
        found = await activity_for(conn, principal, self._tools.registry, replies)
        return [
            m.model_copy(update={"tools": found[m.id]}) if m.id in found else m for m in messages
        ]

    async def _prepare(
        self, gen: Generation, grounded: Grounded | None = None
    ) -> tuple[ChatRequest, AIRequestContext]:
        """The gateway request: system instructions, the evidence of a grounded reply,
        relevant memories, the rolling summary and the bounded recent history
        (docs/architecture/memory-engine.md#context, rag.md#evidence)."""
        model = gen.assistant_message.model
        registry = await self._gateway.catalog.registry()
        alias = registry.get(model) if model else registry.default_alias
        context_window = (
            alias.context_window if alias and alias.context_window else _FALLBACK_CONTEXT_WINDOW
        )
        async with self._engine.connect() as conn:
            rows, _ = await repo.list_messages(
                conn,
                gen.conversation.id,
                limit=self._policy.max_messages + 2,
                before=gen.assistant_message.sequence,
            )
        memories, summary = await self._recall(gen)
        history = [
            HistoryItem(
                role=row.role,
                # Earlier answers' [S1] markers meant other passages: they would mislead.
                content=strip_citations(row.content)
                if grounded is not None and row.role == "assistant"
                else row.content,
                status=row.status,
                sequence=row.sequence,
            )
            for row in rows
            if row.role in ("user", "assistant")
        ]
        built = build_context(
            history,
            prompt_version=gen.conversation.prompt_version,
            context_window=context_window,
            max_output_tokens=self._settings.chat_max_output_tokens,
            policy=self._policy,
            memories=memories,
            summary=summary,
            evidence=grounded.evidence.message if grounded is not None else None,
        )
        log.info(
            "chat.context.built",
            conversation_id=str(gen.conversation.id),
            message_id=str(gen.assistant_message.id),
            included=built.included,
            dropped=built.dropped,
            memories=built.memories_included,
            memories_dropped=built.memories_dropped,
            summary=built.summary_included,
            evidence_passages=len(grounded.evidence.passages) if grounded else 0,
            evidence_tokens=grounded.evidence.estimated_tokens if grounded else 0,
            estimated_tokens=built.estimated_tokens,
        )
        request = ChatRequest(
            model=model or None,
            messages=built.messages,
            max_output_tokens=self._settings.chat_max_output_tokens,
            feature=Feature.RAG if grounded is not None else Feature.CHAT,
        )
        ctx = replace(
            AIRequestContext.from_principal(gen.principal, request_id=gen.request_id),
            source_type="message",
            source_id=gen.assistant_message.id,
        )
        return request, ctx

    async def _recall(self, gen: Generation) -> tuple[Sequence[MemoryItem], Any]:
        """What the memory engine has for this turn. A failure there costs the turn its
        memories, never the reply."""
        if self._memory is None:
            return (), None
        try:
            return await self._memory.recall(
                gen.principal, conversation_id=gen.conversation.id, query=gen.user_message.content
            )
        except Exception as exc:
            log.error(
                "memory.recall.failed",
                conversation_id=str(gen.conversation.id),
                error=type(exc).__name__,
            )
            return (), None

    async def _mark_streaming(self, message_id: UUID) -> None:
        async with self._engine.begin() as conn:
            await repo.update_message(conn, message_id, status="streaming")

    async def _flush(self, message_id: UUID, text: str) -> None:
        """Keep the partial reply on disk while it streams, so a crash loses little."""
        try:
            async with self._engine.begin() as conn:
                await repo.update_message(conn, message_id, content=text)
        except Exception as exc:
            log.warning("chat.flush_failed", message_id=str(message_id), error=type(exc).__name__)

    async def _settle(
        self,
        gen: Generation,
        progress: _Progress,
        *,
        status: str,
        error: NexaError | None = None,
        error_code: str | None = None,
    ) -> tuple[MessageOut, bool]:
        """Store how the generation ended and release the conversation (`_store_outcome`).

        The write runs in a task of its own: if the request is cancelled now (the client left
        during the final write), it still completes instead of stopping half-way with the
        conversation locked. `drain` waits for it at shutdown."""
        task = asyncio.get_running_loop().create_task(
            self._store_outcome(gen, progress, status=status, error=error, error_code=error_code)
        )
        self._track(task)
        return await asyncio.shield(task)

    async def _store_outcome(
        self,
        gen: Generation,
        progress: _Progress,
        *,
        status: str,
        error: NexaError | None = None,
        error_code: str | None = None,
    ) -> tuple[MessageOut, bool]:
        """`ok` is False when the outcome could not be stored. A smaller write then marks the
        reply failed and releases the conversation, so nothing stays locked, and the caller
        reports an internal error instead of the outcome it meant to."""
        code = error_code or (error.error_code.value if error else "")
        # Citations are checked against the passages this reply was given, and nothing else.
        check = (
            check_citations(progress.text, progress.grounded.evidence.labels)
            if progress.grounded is not None
            else None
        )
        try:
            row = await self._finalize(gen, progress, status=status, code=code, check=check)
            if status == "completed":
                self._after_turn(gen, row)
            out = MessageOut.from_row(row)
            if progress.grounded is not None:
                out = out.model_copy(update={"sources": self._sources_of(progress.grounded, check)})
            if progress.steps:
                async with self._engine.connect() as conn:
                    [out] = await self._with_tools(conn, gen.principal, [out])
            return out, True
        except Exception as exc:
            log.error(
                "chat.finalize_failed",
                message_id=str(gen.assistant_message.id),
                error=type(exc).__name__,
            )
        if status != "failed":
            code = ErrorCode.AI_INTERNAL_ERROR.value
        try:
            async with self._engine.begin() as conn:
                await repo.release_generation(conn, gen.conversation.id, gen.assistant_message.id)
                failed = await repo.fail_message(conn, gen.assistant_message.id, error_code=code)
            if failed is not None:
                return MessageOut.from_row(failed), False
        except Exception as exc:
            log.error(
                "chat.finalize_fallback_failed",
                message_id=str(gen.assistant_message.id),
                error=type(exc).__name__,
            )
        # Nothing could be written (the database is unreachable): the claim on the conversation
        # expires on its own, and the reply is taken over as interrupted.
        unsaved = MessageOut.from_row(gen.assistant_message).model_copy(
            update={"status": "failed", "error_code": code, "content": progress.text}
        )
        return unsaved, False

    async def _finalize(
        self,
        gen: Generation,
        progress: _Progress,
        *,
        status: str,
        code: str,
        check: CitationCheck | None = None,
    ) -> repo.MessageRow:
        usage = progress.usage
        values: dict[str, Any] = {
            "content": progress.text,
            "status": status,
            "model": progress.alias or gen.assistant_message.model,
            "provider": progress.provider,
            "provider_model": progress.provider_model,
            "input_tokens": usage.input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "cost_micros": usd_to_micros(usage.cost_usd) if usage and usage.cost_usd else None,
            "latency_ms": progress.elapsed_ms(),
            "ttft_ms": progress.ttft_ms,
            "usage_event_id": progress.usage_event_id,
            "request_id": gen.request_id or "",
            "finish_reason": progress.finish_reason,
            "error_code": code,
            "completed_at": None if status == "awaiting_approval" else repo.now(),
        }
        if progress.steps:
            values["tool_trace"] = {
                "steps": progress.steps,
                "limit_reached": progress.tool_limit_reached,
            }
        grounded = progress.grounded
        if gen.scope is not None:
            values["grounding"] = (
                grounded.record(status, check)
                if grounded is not None
                else failed_record(gen.scope, "cancelled" if status == "cancelled" else "failed")
            )
        async with self._engine.begin() as conn:
            # The conversation row first (see the module docstring).
            await repo.update_conversation(conn, gen.conversation.id, last_message_at=repo.now())
            await repo.release_generation(conn, gen.conversation.id, gen.assistant_message.id)
            row = await repo.update_message(conn, gen.assistant_message.id, **values)
            if progress.steps and status != "awaiting_approval":
                # Calls that never got to run end with the reply.
                await tools_repo.cancel_open(conn, gen.assistant_message.id)
            if grounded is not None and self._rag is not None:
                await self._rag.store_sources(
                    conn,
                    organization_id=gen.conversation.organization_id,
                    message_id=gen.assistant_message.id,
                    grounded=grounded,
                    check=check,
                )
        if gen.scope is not None:
            record = values["grounding"]
            log.info(
                "rag.answer.completed",
                message_id=str(gen.assistant_message.id),
                outcome=record["outcome"],
                supplied=record.get("supplied", 0),
                cited=record.get("cited", 0),
                invalid_citations=len(record.get("invalid_citations", [])),
            )
        log.info(
            "chat.generation.completed",
            conversation_id=str(gen.conversation.id),
            message_id=str(gen.assistant_message.id),
            usage_event_id=str(progress.usage_event_id) if progress.usage_event_id else None,
            status=status,
            error_code=code or None,
            provider=progress.provider or None,
            provider_model=progress.provider_model or None,
            input_tokens=values["input_tokens"],
            output_tokens=values["output_tokens"],
            latency_ms=values["latency_ms"],
            ttft_ms=progress.ttft_ms,
            characters=len(progress.text),
        )
        return row

    def _abandon(
        self,
        gen: Generation,
        progress: _Progress,
        pump: asyncio.Task[None] | None = None,
        events: GatewayStream | None = None,
    ) -> None:
        """The client went away (or the server is shutting down): stop the model call and keep
        the text that arrived, in a task of its own so the cancellation cannot interrupt it."""

        async def wind_down() -> None:
            if pump is not None:
                await _stop(pump)
            if events is not None:
                await _close(events)
                _take_usage(progress, events)
            await self._store_outcome(gen, progress, status="cancelled")

        self._track(asyncio.get_running_loop().create_task(wind_down()))

    def _after_turn(self, gen: Generation, reply: repo.MessageRow) -> None:
        """Memory extraction and summarization run after a completed reply, in a task of
        their own: they never delay the answer, and their failure never fails it."""
        if self._memory is None:
            return
        self._track(
            asyncio.get_running_loop().create_task(
                self._memory.after_turn(
                    gen.principal,
                    conversation=gen.conversation,
                    user_message=gen.user_message,
                    assistant_message=reply,
                    request_id=gen.request_id,
                    # A grounded reply carries document text: memories are never made from
                    # documents (Phase 07 rule), so only the conversation summary runs.
                    extract=gen.scope is None,
                )
            )
        )

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _generating(self, row: repo.ConversationRow) -> bool:
        return (
            row.active_generation_id is not None
            and row.generation_started_at is not None
            and row.generation_started_at >= self._stale_before()
        )

    def _stale_before(self) -> datetime:
        return repo.now() - timedelta(seconds=self._settings.ai_timeout_total + 60)

    def _out(self, row: repo.ConversationRow) -> ConversationOut:
        out = ConversationOut.from_row(row)
        return out.model_copy(update={"generating": self._generating(row)})

    def _title_from(self, content: str) -> str:
        line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
        line = " ".join(line.split())
        limit = self._settings.chat_title_max_chars
        return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


class GenerationStream:
    """The event stream of one generation, returned by `ChatService.stream`.

    Closing it settles the generation however far it got, including a stream closed before
    its first event was read (the client left before the response started). An async
    generator that never started runs none of its cleanup, so this wrapper then stores the
    reply as cancelled and releases the conversation itself.
    """

    def __init__(self, service: ChatService, gen: Generation) -> None:
        self._service = service
        self._gen = gen
        self._events = service._events(gen)
        self._started = False
        self._closed = False

    def __aiter__(self) -> "GenerationStream":
        return self

    async def __anext__(self) -> ChatEvent | KeepAlive:
        self._started = True
        return await self._events.__anext__()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._started:
            self._service._abandon(self._gen, _Progress(started=time.perf_counter()))
        await self._events.aclose()


def _organization(principal: Principal) -> UUID:
    if principal.organization_id is None:  # pragma: no cover - CurrentPrincipal guarantees it
        raise NotFound
    return principal.organization_id


def _error_event(error: NexaError, message: MessageOut, request_id: str | None) -> GenerationError:
    retry_after = None
    if isinstance(error, AIError) and error.retry_after_seconds is not None:
        retry_after = max(1, int(error.retry_after_seconds + 0.999))
    retryable = isinstance(error, AIError) and (error.retryable or error.fallback_eligible)
    return GenerationError(
        message_id=message.id,
        code=error.error_code.value,
        message=error.message,
        retryable=retryable,
        retry_after=retry_after,
        request_id=request_id,
        assistant_message=message,
    )


def _empty_answer_error(progress: _Progress) -> AIError | None:
    """A reply with no text is a failure: a refusal says so, anything else is retryable."""
    if progress.text.strip():
        return None
    if progress.finish_reason == "content_filter":
        return AIContentRejected()
    return EmptyResponse()


def _take_usage(progress: _Progress, events: GatewayStream | None) -> None:
    """Keep what the gateway metered when the stream ended without a `usage` event."""
    if progress.step_usage is None and events is not None and events.usage is not None:
        progress.meter(events.usage)


def _add_usage(first: Usage | None, second: Usage) -> Usage:
    """Two model calls' usage together; the cost is unknown only when neither had a price."""
    if first is None:
        return second
    costs = [usd_to_micros(c) for c in (first.cost_usd, second.cost_usd) if c is not None]
    return Usage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
        cached_input_tokens=first.cached_input_tokens + second.cached_input_tokens,
        cache_write_input_tokens=first.cache_write_input_tokens + second.cache_write_input_tokens,
        cost_usd=micros_to_usd(sum(costs)) if costs else None,
    )


def _offering(request: ChatRequest, turn: Turn) -> ChatRequest:
    """The request with the turn's tools, and the rules for using them after the other
    system blocks (instructions, memories, summary), just before the conversation."""
    messages = list(request.messages)
    at = next((i for i, m in enumerate(messages) if m.role is not Role.SYSTEM), len(messages))
    messages.insert(at, tools_message())
    return request.model_copy(
        update={"messages": messages, "tools": [d.spec() for d in turn.offered.values()]}
    )


def _traced(call: ToolCallPart, turn: Turn | None) -> dict[str, Any]:
    """A tool call as `tool_trace` keeps it: with the tool's redacted arguments replaced, as
    in its invocation record (replaying "[redacted]" to the model is harmless; tools that
    need approval cannot redact, so an approved call always runs with its real arguments)."""
    traced = call.model_dump(mode="json")
    definition = turn.offered.get(call.name) if turn is not None else None
    if definition is not None and definition.redact:
        traced["arguments"] = redacted(definition, call.arguments)
    return traced


def _step_messages(
    text: str, calls: list[ToolCallPart], results: list[ToolResultPart]
) -> list[Message]:
    """One tool step in the provider-neutral history: the model's turn (its text and calls)
    and one tool message answering every call, in the same order."""
    content: list[Any] = [TextPart(text=text)] if text else []
    return [
        Message(role=Role.ASSISTANT, content=[*content, *calls]),
        Message(role=Role.TOOL, content=list(results)),
    ]


def _tool_call_event(message_id: UUID, item: Planned) -> ToolCallEvent:
    definition = item.definition
    return ToolCallEvent(
        message_id=message_id,
        invocation_id=item.invocation_id,
        call_id=item.call.id,
        tool_id=item.tool_id,
        tool_name=definition.name if definition else item.tool_id,
        step=item.step,
        status="executing" if item.runnable else item.status,
        summary=item.summary,
        risk=definition.risk if definition else "",
        error_code=item.error_code or None,
        error_message=item.error_message or None,
    )


def _tool_result_event(message_id: UUID, finished: Finished) -> ToolResultEvent:
    return ToolResultEvent(
        message_id=message_id,
        invocation_id=finished.planned.invocation_id,
        call_id=finished.planned.call.id,
        tool_id=finished.planned.tool_id,
        status=finished.status,
        duration_ms=finished.duration_ms,
        result=finished.data,
        result_truncated=finished.truncated,
        error_code=finished.error_code or None,
        error_message=finished.error_message or None,
        sources=list(finished.sources),
    )


async def _pump(events: AsyncIterator[Any], queue: "asyncio.Queue[Any]") -> None:
    """Read the gateway stream into `queue`; always close it on the way out."""
    try:
        async for event in events:
            await queue.put(event)
        await queue.put(_END)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        await queue.put(_Failure(exc))
    finally:
        await _close(events)


async def _stop(pump: asyncio.Task[None]) -> None:
    """Stop reading the gateway stream and wait until it has been closed."""
    pump.cancel()
    await asyncio.gather(pump, return_exceptions=True)


async def _unless_stopped[T](call: Coroutine[Any, Any, T], stop: asyncio.Event) -> T | None:
    """Await `call`, unless `stop` is set first: then cancel it and return None. A call that
    has already finished wins over a stop."""
    if stop.is_set():
        call.close()  # stopped before the call began: do not start it
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


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


async def _close(iterator: AsyncIterator[Any]) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception as exc:
            log.debug("chat.stream.close_failed", error=type(exc).__name__)

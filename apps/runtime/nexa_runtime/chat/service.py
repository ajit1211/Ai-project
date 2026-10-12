"""The chat engine: conversation management and the reply-generation lifecycle.

    user submits ──► authorize ──► validate ──► store user message + pending assistant message
                                                  (one transaction; the conversation is locked
                                                   for exactly one generation at a time)
    ──► build context ──► AI Gateway stream ──► relay deltas, flush partial text
    ──► completed | failed | cancelled, usage copied from the gateway, lock released

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
from nexa_ai.cost import usd_to_micros
from nexa_ai.errors import (
    AIContentRejected,
    AIError,
    AIInternalError,
    AIModelUnavailable,
    AIProviderError,
)
from nexa_ai.gateway import Gateway, GatewayStream, KeepAlive
from nexa_ai.types import ChatRequest, Feature, Usage, UsageEvent
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
    ChatEvent,
    GenerationError,
    MessageCancelled,
    MessageComplete,
    MessageStart,
    TextDelta,
    UsageReport,
)
from nexa_runtime.chat.prompts import DEFAULT_PROMPT_VERSION
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
from nexa_runtime.ratelimit import Limiter

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


@dataclass
class _Progress:
    started: float
    parts: list[str] = field(default_factory=list)
    alias: str = ""
    provider: str = ""
    provider_model: str = ""
    usage: Usage | None = None
    usage_event_id: UUID | None = None
    finish_reason: str = ""
    ttft_ms: int | None = None
    ended: bool = False

    @property
    def text(self) -> str:
        return "".join(self.parts)

    @property
    def call_finished(self) -> bool:
        """The gateway has metered the model call: only its closing event is still to come."""
        return self.ended or self.usage is not None

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
    ) -> None:
        self._engine = engine
        self._memory = memory
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
        return [MessageOut.from_row(m) for m in rows], next_before

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
                    return await self._replay(conn, conversation, earlier)
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
        return Generation(conversation, user, assistant, principal, request_id)

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
            )
            changes: dict[str, Any] = {"message_count": count}
            if body.model and body.model != conversation.model:
                changes["model"] = body.model
            conversation = await repo.update_conversation(conn, conversation.id, **changes)
        return Generation(conversation, user, assistant, principal, request_id)

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
        """The generation itself. The gateway stream is read in a separate task, so a stop
        request is noticed within a quarter of a second even while the model is thinking."""
        assistant_id: UUID = gen.assistant_message.id
        progress = _Progress(started=time.perf_counter())
        stop = self._cancel.start(assistant_id)
        events: GatewayStream | None = None
        pump: asyncio.Task[None] | None = None
        done = False
        try:
            yield MessageStart(
                conversation_id=gen.conversation.id,
                request_id=gen.request_id,
                user_message=MessageOut.from_row(gen.user_message),
                assistant_message=MessageOut.from_row(gen.assistant_message),
            )
            request, ctx = await self._prepare(gen)
            # Marked before the gateway call: between opening the gateway stream (which holds
            # a quota reservation) and handing it to the pump nothing is awaited, so it cannot
            # be abandoned unclosed.
            await self._mark_streaming(assistant_id)
            events = await self._gateway.open_stream(request, ctx)
            progress.usage_event_id = events.call_id
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
            # Started eagerly, so the pump owns the gateway stream (and closes it on the way
            # out) even when a stop is already waiting. (Python 3.13 needs the loop passed.)
            pump = asyncio.Task(
                _pump(events, queue), loop=asyncio.get_running_loop(), eager_start=True
            )
            last_flush = time.monotonic()
            while True:
                # A stop that arrives once the model has finished changes nothing.
                if stop.is_set() and not progress.call_finished:
                    await _stop(pump)
                    pump = None
                    await _close(events)
                    _take_usage(progress, events)
                    done = True
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
                    text = progress.add_text(item.text)
                    if not text:
                        continue
                    if progress.ttft_ms is None:
                        progress.ttft_ms = _elapsed_ms(progress.started)
                    yield TextDelta(message_id=assistant_id, text=text)
                    if time.monotonic() - last_flush > _FLUSH_SECONDS:
                        await self._flush(assistant_id, progress.text)
                        last_flush = time.monotonic()
                elif isinstance(item, UsageEvent):
                    # Sent before the end of the stream, whether it succeeded or failed.
                    progress.usage = item.usage
                    yield UsageReport(message_id=assistant_id, **item.usage.model_dump())
                elif isinstance(item, GatewayEnd):
                    progress.finish_reason = item.finish_reason.value
                    progress.ended = True
                elif isinstance(item, GatewayError):
                    done = True
                    out, _ = await self._settle(
                        gen, progress, status="failed", error_code=item.code
                    )
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
                _take_usage(progress, events)
                out, _ = await self._settle(gen, progress, status="failed", error=exc)
                yield _error_event(exc, out, gen.request_id)
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as exc:
            log.error("chat.generation.bug", error=type(exc).__name__, message_id=str(assistant_id))
            if not done:
                done = True
                _take_usage(progress, events)
                internal = AIInternalError()
                out, _ = await self._settle(gen, progress, status="failed", error=internal)
                yield _error_event(internal, out, gen.request_id)
        finally:
            self._cancel.finish(assistant_id)
            if not done:
                # The client went away, or the server is shutting down.
                self._abandon(gen, progress, pump, events)
            elif pump is not None and not pump.done():
                # Closes the gateway stream, which closes the provider's and records usage.
                pump.cancel()
                self._track(pump)

    async def complete(self, gen: Generation) -> Exchange:
        """The non-streaming path: wait for the whole reply. A stop request ends it too; the
        reply is then returned as cancelled."""
        assistant_id: UUID = gen.assistant_message.id
        progress = _Progress(started=time.perf_counter())
        stop = self._cancel.start(assistant_id)
        try:
            request, ctx = await self._prepare(gen)
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
        if response is None:
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
            log.warning("chat.generation.interrupted", message_id=str(message_id))
        return row

    async def _replay(
        self, conn: Any, conversation: repo.ConversationRow, user: repo.MessageRow
    ) -> Exchange:
        reply = await repo.message_at(conn, conversation.id, user.sequence + 1)
        if reply is None or reply.role != "assistant":
            raise Conflict("The earlier message has no reply to return.")
        if reply.status in ("pending", "streaming"):
            if self._generating(conversation) and conversation.active_generation_id == reply.id:
                raise GenerationInProgress("The reply to that message is still being generated.")
            # Its generation died with its process: report that, not a reply pending forever.
            reply = await self._interrupted(conn, reply.id) or reply
        return Exchange(
            user_message=MessageOut.from_row(user), assistant_message=MessageOut.from_row(reply)
        )

    async def _prepare(self, gen: Generation) -> tuple[ChatRequest, AIRequestContext]:
        """The gateway request: system instructions, relevant memories, the rolling summary
        and the bounded recent history (docs/architecture/memory-engine.md#context)."""
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
                role=row.role, content=row.content, status=row.status, sequence=row.sequence
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
            estimated_tokens=built.estimated_tokens,
        )
        request = ChatRequest(
            model=model or None,
            messages=built.messages,
            max_output_tokens=self._settings.chat_max_output_tokens,
            feature=Feature.CHAT,
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
        try:
            row = await self._finalize(gen, progress, status=status, code=code)
            if status == "completed":
                self._after_turn(gen, row)
            return MessageOut.from_row(row), True
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
        self, gen: Generation, progress: _Progress, *, status: str, code: str
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
            "latency_ms": _elapsed_ms(progress.started),
            "ttft_ms": progress.ttft_ms,
            "usage_event_id": progress.usage_event_id,
            "request_id": gen.request_id or "",
            "finish_reason": progress.finish_reason,
            "error_code": code,
            "completed_at": repo.now(),
        }
        async with self._engine.begin() as conn:
            # The conversation row first (see the module docstring).
            await repo.update_conversation(conn, gen.conversation.id, last_message_at=repo.now())
            await repo.release_generation(conn, gen.conversation.id, gen.assistant_message.id)
            row = await repo.update_message(conn, gen.assistant_message.id, **values)
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
    if progress.usage is None and events is not None and events.usage is not None:
        progress.usage = events.usage


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

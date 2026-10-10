"""The chat engine: conversation management and the reply-generation lifecycle.

    user submits ──► authorize ──► validate ──► store user message + pending assistant message
                                                  (one transaction; the conversation is locked
                                                   for exactly one generation at a time)
    ──► build context ──► AI Gateway stream ──► relay deltas, flush partial text
    ──► completed | failed | cancelled, usage copied from the gateway, lock released

Everything here is scoped by the caller's principal (organization from the token, ownership
checked with the shared policy). The gateway does authorization, quotas and metering for the
model call itself; this service only persists what happened to it.
"""

import asyncio
import time
from collections.abc import AsyncIterator
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
from nexa_ai.gateway import Gateway, KeepAlive
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
from nexa_runtime.chat.context import ContextPolicy, HistoryItem, build_context
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


class ChatService:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        gateway: Gateway,
        cancel: CancelRegistry,
        settings: RuntimeSettings,
        limiter: Limiter | None = None,
    ) -> None:
        self._engine = engine
        self._gateway = gateway
        self._cancel = cancel
        self._limiter = limiter or Limiter(None)
        self._settings = settings
        self._policy = ContextPolicy(
            max_messages=settings.chat_context_max_messages,
            max_tokens=settings.chat_context_max_tokens,
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

    async def stream(self, gen: Generation) -> AsyncIterator[ChatEvent | KeepAlive]:
        """The `nexa.chat.v1` event stream for one generation. Exactly one terminal event.

        The gateway stream is read in a separate task, so a stop request is noticed within a
        quarter of a second even while the model is still thinking."""
        assistant_id: UUID = gen.assistant_message.id
        progress = _Progress(started=time.perf_counter())
        stop = self._cancel.start(assistant_id)
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
            events = await self._gateway.open_stream(request, ctx)
            await self._mark_streaming(assistant_id)
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
            pump = asyncio.create_task(_pump(events, queue))
            last_flush = time.monotonic()
            while True:
                if stop.is_set():
                    await _stop(pump)
                    pump = None
                    done = True
                    row = await self._finalize(gen, progress, status="cancelled")
                    yield MessageCancelled(message=MessageOut.from_row(row))
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
                    progress.usage_event_id = UUID(item.id)
                elif isinstance(item, GatewayDelta):
                    if progress.ttft_ms is None:
                        progress.ttft_ms = _elapsed_ms(progress.started)
                    progress.parts.append(item.text)
                    yield TextDelta(message_id=assistant_id, text=item.text)
                    if time.monotonic() - last_flush > _FLUSH_SECONDS:
                        await self._flush(assistant_id, progress.text)
                        last_flush = time.monotonic()
                elif isinstance(item, UsageEvent):
                    progress.usage = item.usage
                    yield UsageReport(message_id=assistant_id, **item.usage.model_dump())
                elif isinstance(item, GatewayEnd):
                    progress.finish_reason = item.finish_reason.value
                    progress.ended = True
                elif isinstance(item, GatewayError):
                    done = True
                    row = await self._finalize(gen, progress, status="failed", error_code=item.code)
                    yield GenerationError(
                        message_id=assistant_id,
                        code=item.code,
                        message=item.message,
                        retryable=item.retryable,
                        request_id=gen.request_id,
                        assistant_message=MessageOut.from_row(row),
                    )
                    return
            if not progress.ended:
                raise AIProviderError("The provider ended the stream without finishing.")
            empty = _empty_answer_error(progress)
            done = True
            if empty is not None:
                row = await self._finalize(gen, progress, status="failed", error=empty)
                yield _error_event(empty, row, gen.request_id)
                return
            row = await self._finalize(gen, progress, status="completed")
            yield MessageComplete(message=MessageOut.from_row(row))
        except NexaError as exc:
            if not done:
                done = True
                row = await self._finalize(gen, progress, status="failed", error=exc)
                yield _error_event(exc, row, gen.request_id)
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as exc:
            log.error("chat.generation.bug", error=type(exc).__name__, message_id=str(assistant_id))
            if not done:
                done = True
                internal = AIInternalError()
                row = await self._finalize(gen, progress, status="failed", error=internal)
                yield _error_event(internal, row, gen.request_id)
        finally:
            self._cancel.finish(assistant_id)
            if pump is not None and not pump.done():
                # Closes the gateway stream, which closes the provider's and records usage.
                pump.cancel()
                self._track(pump)
            if not done:
                # The client went away (or the server is shutting down): keep the text that
                # arrived, in a task of its own so the cancellation cannot interrupt it.
                self._finalize_detached(gen, progress)

    async def complete(self, gen: Generation) -> Exchange:
        """The non-streaming path: wait for the whole reply."""
        progress = _Progress(started=time.perf_counter())
        try:
            request, ctx = await self._prepare(gen)
            await self._mark_streaming(gen.assistant_message.id)
            response = await self._gateway.chat(request, ctx)
        except NexaError as exc:
            await self._finalize(gen, progress, status="failed", error=exc)
            raise
        except asyncio.CancelledError:
            self._finalize_detached(gen, progress)
            raise
        progress.parts.append(response.content)
        progress.alias = response.model
        progress.provider = response.provider
        progress.provider_model = response.provider_model
        progress.usage = response.usage
        progress.usage_event_id = UUID(response.id)
        progress.finish_reason = response.finish_reason.value
        progress.ended = True
        empty = _empty_answer_error(progress)
        if empty is not None:
            await self._finalize(gen, progress, status="failed", error=empty)
            raise empty
        row = await self._finalize(gen, progress, status="completed")
        return Exchange(
            user_message=MessageOut.from_row(gen.user_message),
            assistant_message=MessageOut.from_row(row),
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
        if not await repo.acquire_generation(
            conn, conversation.id, message_id, stale_before=self._stale_before()
        ):
            raise GenerationInProgress()

    async def _replay(
        self, conn: Any, conversation: repo.ConversationRow, user: repo.MessageRow
    ) -> Exchange:
        reply = await repo.message_at(conn, conversation.id, user.sequence + 1)
        if reply is None or reply.role != "assistant":
            raise Conflict("The earlier message has no reply to return.")
        if reply.status in ("pending", "streaming") and self._generating(conversation):
            raise GenerationInProgress("The reply to that message is still being generated.")
        return Exchange(
            user_message=MessageOut.from_row(user), assistant_message=MessageOut.from_row(reply)
        )

    async def _prepare(self, gen: Generation) -> tuple[ChatRequest, AIRequestContext]:
        """The gateway request: system instructions plus the bounded recent history."""
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
        history = [
            HistoryItem(role=row.role, content=row.content, status=row.status)
            for row in rows
            if row.role in ("user", "assistant")
        ]
        built = build_context(
            history,
            prompt_version=gen.conversation.prompt_version,
            context_window=context_window,
            max_output_tokens=self._settings.chat_max_output_tokens,
            policy=self._policy,
        )
        log.info(
            "chat.context.built",
            conversation_id=str(gen.conversation.id),
            message_id=str(gen.assistant_message.id),
            included=built.included,
            dropped=built.dropped,
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

    async def _finalize(
        self,
        gen: Generation,
        progress: _Progress,
        *,
        status: str,
        error: NexaError | None = None,
        error_code: str | None = None,
    ) -> repo.MessageRow:
        code = error_code or (error.error_code.value if error else "")
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
            row = await repo.update_message(conn, gen.assistant_message.id, **values)
            await repo.release_generation(conn, gen.conversation.id, gen.assistant_message.id)
            await repo.update_conversation(conn, gen.conversation.id, last_message_at=repo.now())
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

    def _finalize_detached(self, gen: Generation, progress: _Progress) -> None:
        self._track(
            asyncio.get_running_loop().create_task(
                self._finalize(gen, progress, status="cancelled")
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


def _organization(principal: Principal) -> UUID:
    if principal.organization_id is None:  # pragma: no cover - CurrentPrincipal guarantees it
        raise NotFound
    return principal.organization_id


def _error_event(error: NexaError, row: repo.MessageRow, request_id: str | None) -> GenerationError:
    retry_after = None
    if isinstance(error, AIError) and error.retry_after_seconds is not None:
        retry_after = max(1, int(error.retry_after_seconds + 0.999))
    retryable = isinstance(error, AIError) and (error.retryable or error.fallback_eligible)
    return GenerationError(
        message_id=row.id,
        code=error.error_code.value,
        message=error.message,
        retryable=retryable,
        retry_after=retry_after,
        request_id=request_id,
        assistant_message=MessageOut.from_row(row),
    )


def _empty_answer_error(progress: _Progress) -> AIError | None:
    """A reply with no text is a failure: a refusal says so, anything else is retryable."""
    if progress.text.strip():
        return None
    if progress.finish_reason == "content_filter":
        return AIContentRejected()
    return EmptyResponse()


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


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


async def _close(iterator: AsyncIterator[Any]) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception as exc:
            log.debug("chat.stream.close_failed", error=type(exc).__name__)

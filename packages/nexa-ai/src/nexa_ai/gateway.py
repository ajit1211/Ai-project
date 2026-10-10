"""The AI Gateway: the one entry point for every model call in NEXA.

    gateway = Gateway(providers={...}, catalog=..., quota=..., usage=...)
    response = await gateway.chat(request, ctx)
    events = await gateway.open_stream(request, ctx)   # async iterator of stream events

For each call the gateway checks the permission and the organization's policy, resolves
the logical model to candidate models, reserves quota, then walks the candidates with
retries and fallback, under one deadline. It validates structured output, meters tokens and
cost, writes one usage record and one log line, and returns NEXA's normalized shapes. The
rules are documented in docs/architecture/ai-gateway.md.

Infrastructure is injected (catalog, quota, usage sink), so this module has no database,
Redis or web framework dependency, and an agent run in Phase 11 goes through exactly the
same checks as a playground request.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from nexa_ai.context import AIRequestContext
from nexa_ai.cost import cost_micros, micros_to_usd
from nexa_ai.errors import (
    AIAuthenticationError,
    AIContentRejected,
    AIContextTooLarge,
    AIError,
    AIInternalError,
    AIInvalidRequest,
    AIModelUnavailable,
    AIOutputInvalid,
    AIProviderError,
    AIRateLimited,
    AITimeout,
)
from nexa_ai.metrics import GatewayMetrics
from nexa_ai.providers.base import (
    Activity,
    AIProvider,
    ProviderCall,
    ProviderEvent,
    ProviderResult,
    StreamEnd,
    StreamStart,
    TextChunk,
    Timeouts,
    TokenCounts,
)
from nexa_ai.registry import Alias, OrganizationPolicy, Registry
from nexa_ai.retry import RetryPolicy
from nexa_ai.router import (
    Candidate,
    candidates,
    explain_empty,
    required_capabilities,
    resolve_alias,
)
from nexa_ai.structured import OutputMismatch, check_schema, parse_structured, repair_instruction
from nexa_ai.tokens import estimate_message_tokens, estimate_text_tokens
from nexa_ai.types import (
    ChatRequest,
    ChatResponse,
    FinishReason,
    ImagePart,
    JSONSchemaFormat,
    Message,
    MessageDelta,
    MessageEnd,
    MessageStart,
    Role,
    StreamError,
    StreamEvent,
    ToolCallPart,
    ToolResultPart,
    Usage,
    UsageEvent,
)
from nexa_common.errors import PermissionDenied
from nexa_common.ids import new_id
from nexa_common.logs import get_logger

log = get_logger("nexa.ai.gateway")

AI_CHAT_PERMISSION = "ai.chat"
# Do not start another attempt (retry or fallback) with less time than this left.
MIN_ATTEMPT_SECONDS = 2.0
_USAGE_WRITE_DELAYS = (0.0, 0.25, 1.0)

T = TypeVar("T")


# --- Injected infrastructure ---------------------------------------------------------------


class CatalogSource(Protocol):
    """The model registry and organization policies (Runtime reads them from PostgreSQL)."""

    async def registry(self) -> Registry: ...

    async def policy(self, organization_id: UUID) -> OrganizationPolicy: ...


class QuotaGuard(Protocol):
    """Request, token and concurrency limits (Runtime keeps them in Redis).

    `reserve` counts the request and holds the worst-case token cost before any provider is
    called, so concurrent requests cannot jointly overshoot a token limit. `settle` replaces
    the reservation with the real count when the call ends, however it ends."""

    async def reserve(
        self, ctx: AIRequestContext, policy: OrganizationPolicy, *, tokens: int
    ) -> object:
        """Raise AIQuotaExceeded when any limit would be exceeded; otherwise return a ticket."""
        ...

    async def settle(self, ticket: object, *, tokens_used: int) -> None:
        """Release the reservation and charge `tokens_used`. Must never raise."""
        ...


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One row of the usage ledger: metadata only, never prompt or completion text.

    `id` is the gateway call ID: the `id` of the ChatResponse and of the `message.start`
    event, so a caller can link its own records (a message, an agent step) to this row."""

    id: UUID
    created_at: datetime
    organization_id: UUID
    user_id: UUID
    actor_type: str
    request_id: str | None
    source_type: str | None
    source_id: UUID | None
    feature: str
    alias: str
    provider: str
    provider_model: str
    model_id: UUID | None
    price_id: UUID | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    total_tokens: int
    cost_micros: int | None  # None when the model has no price
    latency_ms: int
    ttft_ms: int | None
    status: str  # success | error | cancelled
    error_code: str | None
    attempts: int
    fallback_used: bool
    stream: bool
    structured: bool
    usage_source: str  # provider | estimated


class UsageSink(Protocol):
    async def record(self, usage: UsageRecord) -> None: ...


class NoQuota:
    async def reserve(
        self, ctx: AIRequestContext, policy: OrganizationPolicy, *, tokens: int
    ) -> object:
        return None

    async def settle(self, ticket: object, *, tokens_used: int) -> None:
        return None


class MemoryUsageSink:
    """Keeps records in a list. For tests and for scripts that do not need a database."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)


@dataclass(frozen=True, slots=True)
class KeepAlive:
    """Not an API event: tells the SSE layer to send a comment so proxies keep the stream."""


KEEPALIVE = KeepAlive()


# --- Internal bookkeeping ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    call_id: UUID
    request: ChatRequest
    ctx: AIRequestContext
    alias: Alias
    policy: OrganizationPolicy
    candidates: tuple[Candidate, ...]
    max_output_tokens: int
    estimated_input_tokens: int
    stream: bool
    started: float
    ticket: object = None


@dataclass
class _Tally:
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    estimated: bool = False
    serving: Candidate | None = None
    last_tried: Candidate | None = None
    ttft_ms: int | None = None

    def add(self, usage: TokenCounts) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_input_tokens += usage.cached_input_tokens
        self.cache_write_input_tokens += usage.cache_write_input_tokens

    def add_estimate(self, *, input_tokens: int, output_tokens: int = 0) -> None:
        self.estimated = True
        self.add(TokenCounts(input_tokens=input_tokens, output_tokens=output_tokens))

    def counts(self) -> TokenCounts:
        return TokenCounts(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cached_input_tokens,
            cache_write_input_tokens=self.cache_write_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class _Done:
    pass


@dataclass(frozen=True, slots=True)
class _Failed:
    error: BaseException


_DONE = _Done()

# Which error to report when every candidate failed: the one most useful to the caller.
_SUMMARY_PRIORITY: tuple[type[AIError], ...] = (
    AIRateLimited,
    AITimeout,
    AIProviderError,
    AIContextTooLarge,
    AIModelUnavailable,
    AIAuthenticationError,
)

Decision = Literal["retry", "next", "fatal"]


class GatewayStream:
    """The event stream of one gateway call, returned by `Gateway.open_stream`.

    Closing it settles the call however far it got: the quota reservation is released and a
    usage row is written, even when not a single event was read. (An async generator that was
    never started runs no cleanup when closed; this wrapper does it instead.) Every caller
    must close it; `sse_bytes` and the chat engine do.

    `call_id` is the call's usage row id, known before the first event. `usage` holds the
    call's final token counts once the gateway has metered it: after the `usage` event, or
    after the stream was closed early.
    """

    def __init__(self, gateway: "Gateway", plan: _Plan) -> None:
        self.call_id: UUID = plan.call_id
        self.usage: Usage | None = None
        self._gateway = gateway
        self._plan = plan
        self._events = gateway._stream(plan, self)
        self._started = False
        self._closed = False

    def __aiter__(self) -> "GatewayStream":
        return self

    async def __anext__(self) -> StreamEvent | KeepAlive:
        self._started = True
        return await self._events.__anext__()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._started:
            # Planned and reserved but never run: release the reservation, record the call.
            self.usage = self._gateway._finish_detached(self._plan, _Tally())
        await self._events.aclose()


@dataclass
class _Fallback:
    """Retry and fallback decisions for one gateway call, shared by both execution paths.

    - Retryable errors are retried on the same model with backoff while time allows.
    - Fallback-eligible errors move to the next candidate.
    - Provider-wide errors (credentials, billing) skip every other model at that provider.
    - A provider's context-length error moves only to candidates with a larger window.
    - Everything else is fatal: the caller's request is at fault.
    """

    gateway: "Gateway"
    plan: _Plan
    tally: _Tally
    deadline: float
    failures: list[AIError] = field(default_factory=list)
    dead_providers: set[str] = field(default_factory=set)
    min_window: int = 0

    def candidates(self) -> Iterator[Candidate]:
        for candidate in self.plan.candidates:
            if candidate.model.provider in self.dead_providers:
                continue
            if candidate.model.context_window <= self.min_window:
                continue
            yield candidate

    def can_attempt(self) -> bool:
        if self.tally.attempts >= self.gateway.max_attempts:
            return False
        return self.deadline - self.gateway._clock() >= MIN_ATTEMPT_SECONDS

    def start(self, candidate: Candidate) -> None:
        self.tally.attempts += 1
        self.tally.last_tried = candidate

    def on_failure(
        self, candidate: Candidate, error: AIError, retries: int
    ) -> tuple[Decision, float]:
        self.gateway._note_failure(self.plan, candidate, error)
        self.failures.append(error)
        if isinstance(error, AITimeout):
            # The provider received the input and may have billed it; count it.
            self.tally.add_estimate(input_tokens=self.plan.estimated_input_tokens)
        delay = self.gateway.retry.delay(error, retries)
        if delay is not None and (
            self.gateway._clock() + delay + MIN_ATTEMPT_SECONDS < self.deadline
        ):
            self.gateway.metrics.observe_retry(
                provider=candidate.model.provider, model=candidate.model.provider_model
            )
            return "retry", delay
        if error.provider_wide:
            self.dead_providers.add(candidate.model.provider)
        if isinstance(error, AIContextTooLarge):
            if error.provider is None:
                return "fatal", 0.0
            self.min_window = max(self.min_window, candidate.model.context_window)
            return "next", 0.0
        return ("next" if error.fallback_eligible else "fatal"), 0.0

    def final_error(self) -> AIError:
        return _summarize(self.failures, self.plan)


class Gateway:
    def __init__(
        self,
        *,
        providers: Mapping[str, AIProvider],
        catalog: CatalogSource,
        quota: QuotaGuard | None = None,
        usage: UsageSink | None = None,
        retry: RetryPolicy | None = None,
        timeouts: Timeouts | None = None,
        default_max_output_tokens: int = 2048,
        max_output_tokens_cap: int = 8192,
        max_attempts: int = 5,
        ping_interval: float = 15.0,
        metrics: GatewayMetrics | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.providers = dict(providers)
        self.catalog = catalog
        self.quota: QuotaGuard = quota or NoQuota()
        self.usage: UsageSink = usage or MemoryUsageSink()
        self.retry = retry or RetryPolicy()
        self.timeouts = timeouts or Timeouts()
        self.default_max_output_tokens = default_max_output_tokens
        self.max_output_tokens_cap = max_output_tokens_cap
        self.max_attempts = max_attempts
        self.ping_interval = ping_interval
        self.metrics = metrics or GatewayMetrics()
        self._sleep = sleep
        self._clock = clock
        self._background: set[asyncio.Task[None]] = set()

    # --- Public API --------------------------------------------------------------------

    async def available_models(self, ctx: AIRequestContext) -> list[Alias]:
        """The logical models this organization may use right now."""
        _require_chat(ctx)
        policy = await self.catalog.policy(ctx.organization_id)
        if not policy.ai_enabled:
            return []
        registry = await self.catalog.registry()
        return [
            alias
            for alias in sorted(registry.aliases.values(), key=lambda a: a.name)
            if alias.is_active
            and policy.allows(alias)
            and any(t.is_active and t.provider in self.providers for t in alias.targets)
        ]

    async def chat(self, request: ChatRequest, ctx: AIRequestContext) -> ChatResponse:
        plan = await self._plan(request, ctx, stream=False)
        tally = _Tally()
        fallback = _Fallback(self, plan, tally, plan.started + self.timeouts.total)
        try:
            result, candidate = await self._run(
                fallback, lambda c: self._attempt_chat(plan, c, plan.request, fallback.deadline)
            )
            tally.serving = candidate
            tally.add(result.usage)
            parsed = None
            if isinstance(plan.request.response_format, JSONSchemaFormat):
                result, parsed = await self._structured(
                    fallback, candidate, result, plan.request.response_format
                )
        except AIError as error:
            error.usage = await self._finish(plan, tally, status="error", error=error)
            error.call_id = plan.call_id
            raise
        except asyncio.CancelledError:
            self._finish_detached(plan, tally)
            raise
        except Exception as exc:
            log.error("ai.gateway.bug", error=type(exc).__name__, request_id=ctx.request_id)
            internal = AIInternalError()
            internal.usage = await self._finish(plan, tally, status="error", error=internal)
            internal.call_id = plan.call_id
            raise internal from exc
        usage = await self._finish(plan, tally, status="success")
        return ChatResponse(
            id=str(plan.call_id),
            model=plan.alias.name,
            provider=candidate.model.provider,
            provider_model=candidate.model.provider_model,
            content=result.content,
            parsed=parsed,
            finish_reason=result.finish_reason,
            usage=usage,
            latency_ms=self._elapsed_ms(plan.started),
            attempts=tally.attempts,
            fallback_used=candidate.is_fallback,
        )

    async def open_stream(self, request: ChatRequest, ctx: AIRequestContext) -> GatewayStream:
        """Check everything that can be checked before a provider is called (permission,
        policy, model, quota), raising errors normally, then return the event stream.
        From here on every failure, before or after the first token, is an `error` event,
        preceded by a `usage` event with what the call consumed. The caller must close the
        stream (see `GatewayStream`)."""
        plan = await self._plan(request, ctx, stream=True)
        return GatewayStream(self, plan)

    async def drain(self) -> None:
        """Wait for usage writes started by cancelled calls."""
        while pending := [task for task in self._background if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain()
        for provider in self.providers.values():
            try:
                await provider.aclose()
            except Exception as exc:
                log.warning(
                    "ai.provider.close_failed", provider=provider.name, error=type(exc).__name__
                )

    # --- Planning ----------------------------------------------------------------------

    async def _plan(self, request: ChatRequest, ctx: AIRequestContext, *, stream: bool) -> _Plan:
        started = self._clock()
        _require_chat(ctx)
        _reject_unsupported(request, stream=stream)
        if isinstance(request.response_format, JSONSchemaFormat):
            check_schema(request.response_format.json_schema)

        policy = await self.catalog.policy(ctx.organization_id)
        if not policy.ai_enabled:
            raise PermissionDenied("AI features are disabled for this organization.")
        registry = await self.catalog.registry()
        alias = resolve_alias(registry, policy, request.model)

        max_output = self._max_output_tokens(request, policy)
        estimated = estimate_message_tokens(request.messages)
        required = required_capabilities(request, stream=stream)
        found = candidates(
            alias,
            required=required,
            available_providers=self.providers.keys(),
            estimated_input_tokens=estimated,
            max_output_tokens=max_output,
        )
        if not found:
            raise explain_empty(
                alias,
                required=required,
                available_providers=self.providers.keys(),
                estimated_input_tokens=estimated,
            )
        plan = _Plan(
            call_id=new_id(),
            request=request,
            ctx=ctx,
            alias=alias,
            policy=policy,
            candidates=tuple(found),
            max_output_tokens=max_output,
            estimated_input_tokens=estimated,
            stream=stream,
            started=started,
        )
        # A round trip to Redis. If the call is cancelled meanwhile (a stop, a shutdown), the
        # reservation may still land: it is then released and the call recorded as cancelled,
        # so a cancelled call never keeps a request slot or its worst-case tokens.
        reserving = asyncio.ensure_future(
            self.quota.reserve(ctx, policy, tokens=_worst_case_tokens(plan))
        )
        try:
            ticket = await asyncio.shield(reserving)
        except asyncio.CancelledError:
            reserving.add_done_callback(lambda done: self._settle_late(plan, done))
            raise
        return replace(plan, ticket=ticket)

    def _settle_late(self, plan: _Plan, reserving: "asyncio.Future[object]") -> None:
        if reserving.cancelled() or reserving.exception() is not None:
            return  # nothing was reserved
        self._finish_detached(replace(plan, ticket=reserving.result()), _Tally())

    def _max_output_tokens(self, request: ChatRequest, policy: OrganizationPolicy) -> int:
        cap = (
            self.max_output_tokens_cap
            if policy.max_output_tokens is None
            else policy.max_output_tokens
        )
        wanted = request.max_output_tokens or self.default_max_output_tokens
        return max(1, min(wanted, cap))

    def _call(
        self, plan: _Plan, candidate: Candidate, request: ChatRequest, request_timeout: float
    ) -> ProviderCall:
        model = candidate.model
        return ProviderCall(
            request=request,
            model=model.provider_model,
            max_output_tokens=min(plan.max_output_tokens, model.max_output_tokens),
            timeouts=replace(self.timeouts, request=request_timeout),
            temperature=request.temperature if model.accepts_temperature else None,
            end_user_id=plan.ctx.end_user_id,
            request_id=plan.ctx.request_id,
        )

    # --- Execution: retries and fallback -----------------------------------------------

    async def _run(
        self, fallback: _Fallback, attempt: Callable[[Candidate], Awaitable[T]]
    ) -> tuple[T, Candidate]:
        for candidate in fallback.candidates():
            retries = 0
            while fallback.can_attempt():
                fallback.start(candidate)
                try:
                    return await attempt(candidate), candidate
                except AIError as error:
                    decision, delay = fallback.on_failure(candidate, error, retries)
                    if decision == "retry":
                        retries += 1
                        await self._sleep(delay)
                        continue
                    if decision == "fatal":
                        raise
                    break
            else:
                break  # out of attempts or time
        raise fallback.final_error()

    async def _attempt_chat(
        self, plan: _Plan, candidate: Candidate, request: ChatRequest, deadline: float
    ) -> ProviderResult:
        limit = min(self.timeouts.request, deadline - self._clock())
        model = candidate.model
        if limit <= 0:
            raise AITimeout(provider=model.provider, provider_model=model.provider_model)
        provider = self.providers[model.provider]
        call = self._call(plan, candidate, request, limit)
        try:
            async with asyncio.timeout(limit):
                return await provider.chat(call)
        except AIError:
            raise
        except TimeoutError as exc:
            raise AITimeout(provider=model.provider, provider_model=model.provider_model) from exc
        except Exception as exc:
            # Log the type only: an exception message may quote request content.
            log.error("ai.provider.bug", provider=model.provider, error=type(exc).__name__)
            raise AIInternalError(provider=model.provider) from exc

    async def _structured(
        self,
        fallback: _Fallback,
        candidate: Candidate,
        result: ProviderResult,
        fmt: JSONSchemaFormat,
    ) -> tuple[ProviderResult, object]:
        """Validate the answer; on a mismatch, ask the same model once more with the error.

        A refusal or a truncated answer is not repaired: neither would be fixed by asking
        again, and the caller would pay twice."""
        plan, model = fallback.plan, candidate.model
        _check_structured_finish(result, model.provider, model.provider_model)
        try:
            return result, parse_structured(result.content, fmt.json_schema)
        except OutputMismatch as first:
            log.info("ai.structured.repair", alias=plan.alias.name, problems=first.summary)
            repair = plan.request.model_copy(
                update={
                    "messages": [
                        *plan.request.messages,
                        Message(role=Role.ASSISTANT, content=result.content or "(empty)"),
                        Message(role=Role.USER, content=repair_instruction(first.reason)),
                    ]
                }
            )
        same_model = replace(fallback, plan=replace(plan, candidates=(candidate,)), failures=[])
        repaired, _ = await self._run(
            same_model, lambda c: self._attempt_chat(plan, c, repair, fallback.deadline)
        )
        fallback.tally.add(repaired.usage)
        _check_structured_finish(repaired, model.provider, model.provider_model)
        try:
            return repaired, parse_structured(repaired.content, fmt.json_schema)
        except OutputMismatch as second:
            raise AIOutputInvalid(
                provider=model.provider,
                provider_model=model.provider_model,
                provider_message=second.summary,
            ) from second

    # --- Streaming -----------------------------------------------------------------------

    async def _stream(
        self, plan: _Plan, sink: GatewayStream
    ) -> AsyncGenerator[StreamEvent | KeepAlive]:
        tally = _Tally()
        fallback = _Fallback(self, plan, tally, plan.started + self.timeouts.total)
        events: AsyncIterator[ProviderEvent | KeepAlive] | None = None
        finished = False
        text: list[str] = []
        start: StreamStart | None = None
        try:
            # 1. Open a stream and wait for its first answer text, falling back freely:
            #    nothing but keep-alive comments has reached the client yet.
            opened: tuple[Candidate, ProviderEvent] | None = None
            fatal: AIError | None = None
            for candidate in fallback.candidates():
                retries = 0
                while opened is None and fatal is None and fallback.can_attempt():
                    fallback.start(candidate)
                    events = self._provider_events(plan, candidate, fallback.deadline)
                    start = None
                    try:
                        while opened is None:
                            item = await anext(events)
                            if isinstance(item, KeepAlive):
                                yield item
                            elif isinstance(item, StreamStart):
                                start = item
                            elif isinstance(item, TextChunk | StreamEnd):
                                opened = (candidate, item)
                    except (AIError, StopAsyncIteration) as raised:
                        error = (
                            raised
                            if isinstance(raised, AIError)
                            else AIProviderError(
                                "The provider ended the stream without an answer.",
                                provider=candidate.model.provider,
                            )
                        )
                        await _close(events)
                        events = None
                        if start is not None and not isinstance(error, AITimeout):
                            # The provider accepted the input before failing; count it.
                            # (Timeouts are counted by the fallback policy.)
                            tally.add_estimate(
                                input_tokens=start.input_tokens or plan.estimated_input_tokens
                            )
                        decision, delay = fallback.on_failure(candidate, error, retries)
                        if decision == "retry":
                            retries += 1
                            await self._sleep(delay)
                            continue
                        if decision == "fatal":
                            fatal = error
                        break
                if opened is not None or fatal is not None or not fallback.can_attempt():
                    break

            if opened is None:
                error = fatal or fallback.final_error()
                finished = True
                usage = await self._finish(plan, tally, status="error", error=error, sink=sink)
                yield UsageEvent(usage=usage)
                yield _stream_error(error, plan)
                return

            # 2. Relay the stream. From here on a failure is an `error` event: the client has
            #    seen text from this model, so switching models is not allowed.
            candidate, item = opened
            tally.serving = candidate
            yield MessageStart(
                id=str(plan.call_id),
                model=plan.alias.name,
                provider=candidate.model.provider,
                provider_model=candidate.model.provider_model,
                request_id=plan.ctx.request_id,
            )
            assert events is not None  # noqa: S101 - set together with `opened`
            end: StreamEnd | None = None
            failure: AIError | None = None
            try:
                while True:
                    if isinstance(item, TextChunk):
                        if tally.ttft_ms is None:
                            tally.ttft_ms = self._elapsed_ms(plan.started)
                        text.append(item.text)
                        yield MessageDelta(text=item.text)
                    elif isinstance(item, KeepAlive):
                        yield item
                    elif isinstance(item, StreamEnd):
                        end = item
                        break
                    item = await anext(events)
            except StopAsyncIteration:
                failure = AIProviderError(
                    "The provider stream ended unexpectedly.", provider=candidate.model.provider
                )
            except AIError as error:
                failure = error

            self._count_stream_usage(plan, tally, start, end, text)
            finished = True
            if failure is not None or end is None:
                failure = failure or AIProviderError(provider=candidate.model.provider)
                self._note_failure(plan, candidate, failure)
                usage = await self._finish(plan, tally, status="error", error=failure, sink=sink)
                yield UsageEvent(usage=usage)
                yield _stream_error(failure, plan)
                return
            usage = await self._finish(plan, tally, status="success", sink=sink)
            yield UsageEvent(usage=usage)
            yield MessageEnd(
                finish_reason=end.finish_reason,
                latency_ms=self._elapsed_ms(plan.started),
                attempts=tally.attempts,
                fallback_used=candidate.is_fallback,
            )
        except Exception as exc:
            # A bug, not a provider failure: end the stream cleanly instead of truncating it.
            log.error("ai.gateway.bug", error=type(exc).__name__, request_id=plan.ctx.request_id)
            if not finished:
                finished = True
                self._count_stream_usage(plan, tally, start, None, text)
                usage = await self._finish(
                    plan, tally, status="error", error=AIInternalError(), sink=sink
                )
                yield UsageEvent(usage=usage)
            yield _stream_error(AIInternalError(), plan)
        finally:
            if events is not None:
                await _close(events)
            if not finished:
                # The client went away (or the task was cancelled). Record what was used.
                self._count_stream_usage(plan, tally, start, None, text)
                sink.usage = self._finish_detached(plan, tally)

    async def _provider_events(
        self, plan: _Plan, candidate: Candidate, deadline: float
    ) -> AsyncIterator[ProviderEvent | KeepAlive]:
        """The provider's stream, read in a separate task so the gateway can send keep-alives
        and enforce the first-token, idle and total timeouts without cancelling it."""
        model = candidate.model
        provider = self.providers[model.provider]
        call = self._call(plan, candidate, plan.request, self.timeouts.request)
        queue: asyncio.Queue[ProviderEvent | _Done | _Failed] = asyncio.Queue(maxsize=64)

        async def pump() -> None:
            try:
                async for event in provider.stream(call):
                    await queue.put(event)
                await queue.put(_DONE)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await queue.put(_Failed(exc))

        task = asyncio.create_task(pump())
        timeout_error = AITimeout(provider=model.provider, provider_model=model.provider_model)
        got_text = False
        last = self._clock()
        try:
            while True:
                limit = self.timeouts.idle if got_text else self.timeouts.first_token
                remaining = min(last + limit, deadline) - self._clock()
                if remaining <= 0:
                    raise timeout_error
                wait = min(remaining, self.ping_interval)
                try:
                    async with asyncio.timeout(wait):
                        item = await queue.get()
                except TimeoutError:
                    if wait >= remaining:
                        raise timeout_error from None
                    yield KEEPALIVE
                    continue
                if isinstance(item, _Done):
                    return
                if isinstance(item, _Failed):
                    if isinstance(item.error, AIError):
                        raise item.error
                    log.error(
                        "ai.provider.bug", provider=model.provider, error=type(item.error).__name__
                    )
                    raise AIInternalError(provider=model.provider) from item.error
                if isinstance(item, Activity):
                    last = self._clock()
                    continue
                if isinstance(item, TextChunk | StreamEnd):
                    got_text = True
                    last = self._clock()
                yield item
        finally:
            task.cancel()

    def _count_stream_usage(
        self,
        plan: _Plan,
        tally: _Tally,
        start: StreamStart | None,
        end: StreamEnd | None,
        text: list[str],
    ) -> None:
        if end is not None and end.usage is not None:
            tally.add(end.usage)
            return
        if tally.serving is None and not text:
            return  # no stream got as far as an answer; abandoned attempts are counted already
        input_tokens = (
            start.input_tokens
            if start is not None and start.input_tokens
            else plan.estimated_input_tokens
        )
        tally.add_estimate(
            input_tokens=input_tokens, output_tokens=estimate_text_tokens("".join(text))
        )

    # --- Metering ------------------------------------------------------------------------

    def _usage_record(
        self, plan: _Plan, tally: _Tally, *, status: str, error: AIError | None
    ) -> tuple[UsageRecord, Usage]:
        candidate = tally.serving or tally.last_tried or plan.candidates[0]
        model = candidate.model
        counts = tally.counts()
        cost = cost_micros(counts, model.price) if model.price else None
        if model.price is None and counts.total_tokens:
            log.error("ai.price.missing", provider=model.provider, model=model.provider_model)
        record = UsageRecord(
            id=plan.call_id,
            created_at=datetime.now(UTC),
            organization_id=plan.ctx.organization_id,
            user_id=plan.ctx.user_id,
            actor_type=plan.ctx.actor_type,
            request_id=plan.ctx.request_id,
            source_type=plan.ctx.source_type,
            source_id=plan.ctx.source_id,
            feature=plan.request.feature.value,
            alias=plan.alias.name,
            provider=model.provider,
            provider_model=model.provider_model,
            model_id=model.id,
            price_id=model.price.id if model.price else None,
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,
            cached_input_tokens=counts.cached_input_tokens,
            cache_write_input_tokens=counts.cache_write_input_tokens,
            total_tokens=counts.total_tokens,
            cost_micros=cost,
            latency_ms=self._elapsed_ms(plan.started),
            ttft_ms=tally.ttft_ms,
            status=status,
            error_code=error.error_code.value if error else None,
            attempts=tally.attempts,
            fallback_used=candidate.is_fallback and tally.serving is not None,
            stream=plan.stream,
            structured=plan.request.wants_structured_output,
            usage_source="estimated" if tally.estimated else "provider",
        )
        usage = Usage(
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,
            total_tokens=counts.total_tokens,
            cached_input_tokens=counts.cached_input_tokens,
            cache_write_input_tokens=counts.cache_write_input_tokens,
            cost_usd=micros_to_usd(cost) if cost is not None else None,
        )
        return record, usage

    async def _finish(
        self,
        plan: _Plan,
        tally: _Tally,
        *,
        status: str,
        error: AIError | None = None,
        sink: GatewayStream | None = None,
    ) -> Usage:
        record, usage = self._usage_record(plan, tally, status=status, error=error)
        self._observe(record)
        if sink is not None:
            # Known before the write below, so a stop during it does not lose the numbers.
            sink.usage = usage
        # Shielded: if the request is cancelled now (the client left right after the last
        # token), the quota settle and the ledger row still complete in their own task.
        await asyncio.shield(self._spawn_persist(plan, record))
        return usage

    def _finish_detached(self, plan: _Plan, tally: _Tally) -> Usage:
        """Record a cancelled call without awaiting: the current task is being cancelled, so
        the write runs in its own task (awaited by `drain`, and at shutdown)."""
        record, usage = self._usage_record(plan, tally, status="cancelled", error=None)
        self._observe(record)
        self._spawn_persist(plan, record)
        return usage

    def _spawn_persist(self, plan: _Plan, record: UsageRecord) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(self._persist(plan, record))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _persist(self, plan: _Plan, record: UsageRecord) -> None:
        try:
            await self.quota.settle(plan.ticket, tokens_used=record.total_tokens)
        except Exception as exc:
            log.error(
                "ai.quota.settle_failed", request_id=record.request_id, error=type(exc).__name__
            )
        for attempt, delay in enumerate(_USAGE_WRITE_DELAYS, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self.usage.record(record)
                return
            except Exception as exc:
                log.warning("ai.usage.write_retry", attempt=attempt, error=type(exc).__name__)
        # The ledger is the cost truth: keep the full row in the log so it can be replayed.
        row = {k: (str(v) if v is not None else None) for k, v in asdict(record).items()}
        log.error("ai.usage.write_failed", usage_event=row)

    def _observe(self, record: UsageRecord) -> None:
        self.metrics.observe_call(
            alias=record.alias,
            provider=record.provider,
            model=record.provider_model,
            status=record.status,
            error_code=record.error_code,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cost_micros=record.cost_micros or 0,
            latency_ms=record.latency_ms,
            fallback_used=record.fallback_used,
        )
        fields = {
            "usage_event_id": str(record.id),
            "organization_id": str(record.organization_id),
            "user_id": str(record.user_id),
            "feature": record.feature,
            "alias": record.alias,
            "provider": record.provider,
            "provider_model": record.provider_model,
            "status": record.status,
            "error_code": record.error_code,
            "attempts": record.attempts,
            "fallback_used": record.fallback_used,
            "stream": record.stream,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "cost_micros": record.cost_micros,
            "latency_ms": record.latency_ms,
            "ttft_ms": record.ttft_ms,
            "usage_source": record.usage_source,
        }
        if record.status == "success":
            log.info("ai.request.completed", **fields)
        else:
            log.warning("ai.request.completed", **fields)

    def _note_failure(self, plan: _Plan, candidate: Candidate, error: AIError) -> None:
        self.metrics.observe_attempt_failure(
            provider=candidate.model.provider,
            model=candidate.model.provider_model,
            error_code=error.error_code.value,
        )
        log.warning("ai.attempt.failed", alias=plan.alias.name, **error.log_fields())

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._clock() - started) * 1000))


# --- Helpers -------------------------------------------------------------------------------


def _require_chat(ctx: AIRequestContext) -> None:
    if AI_CHAT_PERMISSION not in ctx.permissions:
        raise PermissionDenied("You do not have permission to use AI models.")


def _reject_unsupported(request: ChatRequest, *, stream: bool) -> None:
    """Refuse what Phase 04 cannot serve, before any model is chosen."""
    if stream and request.wants_structured_output:
        raise AIInvalidRequest("Structured output cannot be streamed yet.")
    for message in request.messages:
        if message.role is Role.TOOL:
            raise AIInvalidRequest("Tool messages are not supported yet.")
        for part in message.parts:
            if isinstance(part, ImagePart):
                raise AIInvalidRequest("Image input is not supported yet.")
            if isinstance(part, ToolCallPart | ToolResultPart):
                raise AIInvalidRequest("Tool calls are not supported yet.")


def _worst_case_tokens(plan: _Plan) -> int:
    """What a call can cost at most, reserved against token quotas before it starts. A
    structured request may need a repair turn: the conversation again, plus the first answer,
    plus a second answer."""
    one_call = plan.estimated_input_tokens + plan.max_output_tokens
    if plan.request.wants_structured_output:
        return 2 * one_call + plan.max_output_tokens
    return one_call


def _check_structured_finish(result: ProviderResult, provider: str, model: str) -> None:
    if result.finish_reason is FinishReason.CONTENT_FILTER:
        raise AIContentRejected(provider=provider, provider_model=model)
    if result.finish_reason is FinishReason.LENGTH:
        raise AIOutputInvalid(
            "The answer was cut off before the JSON document was complete. "
            "Increase max_output_tokens.",
            provider=provider,
            provider_model=model,
        )


def _summarize(failures: list[AIError], plan: _Plan) -> AIError:
    """Every candidate failed: report the error most useful to the caller."""
    if not failures:
        return AIProviderError(f"No model for '{plan.alias.name}' could answer in time.")
    for kind in _SUMMARY_PRIORITY:
        matching = [f for f in failures if isinstance(f, kind)]
        if matching:
            if kind is AIRateLimited:
                return min(matching, key=lambda f: f.retry_after_seconds or 0)
            return matching[-1]
    return failures[-1]


def _stream_error(error: AIError, plan: _Plan) -> StreamError:
    return StreamError(
        code=error.error_code.value,
        message=error.message,
        retryable=error.retryable or error.fallback_eligible,
        request_id=plan.ctx.request_id,
    )


async def _close(iterator: AsyncIterator[object]) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception as exc:
            log.debug("ai.stream.close_failed", error=type(exc).__name__)

"""The gateway end to end against the mock provider: policy, retries, fallback, structured
output, usage and cost."""

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from nexa_ai.context import AIRequestContext
from nexa_ai.cost import cost_micros
from nexa_ai.errors import (
    AIAuthenticationError,
    AIContentRejected,
    AIContextTooLarge,
    AIInvalidRequest,
    AIModelUnavailable,
    AIOutputInvalid,
    AIProviderError,
    AIQuotaExceeded,
    AIRateLimited,
    AITimeout,
)
from nexa_ai.gateway import Gateway, MemoryUsageSink
from nexa_ai.providers.base import ProviderCall, ProviderResult, Timeouts, TokenCounts
from nexa_ai.providers.mock import MockProvider
from nexa_ai.registry import OrganizationPolicy
from nexa_ai.testing import PRIMARY, StaticCatalog, ask, make_registry
from nexa_ai.types import ChatRequest, FinishReason, ImagePart, Message, Role
from nexa_common.errors import PermissionDenied

GatewayFactory = Callable[..., Gateway]

pytestmark = pytest.mark.anyio

PERSON = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["name", "skills"],
}


class TestSuccess:
    async def test_normalized_response(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        response = await make_gateway().chat(ask("Explain Django middleware"), ctx)

        assert response.model == "nexa-balanced"
        assert response.provider == "mock"
        assert response.provider_model == "mock-primary"
        assert "Explain Django middleware" in response.content
        assert response.finish_reason is FinishReason.STOP
        assert response.usage.total_tokens == (
            response.usage.input_tokens + response.usage.output_tokens
        )
        assert response.attempts == 1
        assert response.fallback_used is False
        UUID(response.id)  # also the usage row's ID

    async def test_deterministic(self, make_gateway: GatewayFactory, ctx: AIRequestContext) -> None:
        gateway = make_gateway()
        first = await gateway.chat(ask("same question"), ctx)
        second = await gateway.chat(ask("same question"), ctx)
        assert first.content == second.content
        assert first.usage == second.usage

    async def test_usage_record_and_cost(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        response = await make_gateway().chat(ask("hello", feature="playground"), ctx)

        [record] = usage.records
        assert str(record.id) == response.id
        assert record.organization_id == ctx.organization_id
        assert record.user_id == ctx.user_id
        assert record.request_id == "req_test"
        assert record.feature == "playground"
        assert record.alias == "nexa-balanced"
        assert record.provider_model == "mock-primary"
        assert record.status == "success"
        assert record.total_tokens == record.input_tokens + record.output_tokens
        assert PRIMARY.price is not None
        expected = cost_micros(
            TokenCounts(input_tokens=record.input_tokens, output_tokens=record.output_tokens),
            PRIMARY.price,
        )
        # $3/M input and $15/M output, so the cost is at least the output share.
        assert record.cost_micros == expected > record.output_tokens * 15 - 1
        assert response.usage.cost_usd == f"{expected / 1_000_000:.6f}"
        assert record.price_id == PRIMARY.price.id
        assert record.model_id == PRIMARY.id

    async def test_output_tokens_are_capped(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, provider: MockProvider
    ) -> None:
        gateway = make_gateway(max_output_tokens_cap=300)
        await gateway.chat(ask("hi", max_output_tokens=10_000), ctx)
        assert provider.calls[-1].max_output_tokens == 300

        policy_ctx = ctx
        catalog = StaticCatalog(
            make_registry(), {policy_ctx.organization_id: OrganizationPolicy(max_output_tokens=50)}
        )
        await make_gateway(catalog=catalog).chat(ask("hi", max_output_tokens=10_000), policy_ctx)
        assert provider.calls[-1].max_output_tokens == 50

    async def test_model_selection(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        response = await make_gateway().chat(ask("hi", model="nexa-fast"), ctx)
        assert response.model == "nexa-fast"
        assert response.provider_model == "mock-backup"


class TestPolicy:
    async def test_permission_is_required(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        no_ai = AIRequestContext(
            request_id=None,
            organization_id=ctx.organization_id,
            user_id=ctx.user_id,
            permissions=frozenset({"organization.view"}),
        )
        with pytest.raises(PermissionDenied):
            await make_gateway().chat(ask("hi"), no_ai)

    async def test_ai_disabled_for_the_organization(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, provider: MockProvider
    ) -> None:
        catalog = StaticCatalog(
            make_registry(), {ctx.organization_id: OrganizationPolicy(ai_enabled=False)}
        )
        gateway = make_gateway(catalog=catalog)
        with pytest.raises(PermissionDenied):
            await gateway.chat(ask("hi"), ctx)
        assert await gateway.available_models(ctx) == []
        assert provider.calls == []

    async def test_restricted_models_and_grants(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        with pytest.raises(AIModelUnavailable):
            await make_gateway().chat(ask("hi", model="nexa-reasoning"), ctx)
        names = [a.name for a in await make_gateway().available_models(ctx)]
        assert "nexa-reasoning" not in names

        catalog = StaticCatalog(
            make_registry(),
            {
                ctx.organization_id: OrganizationPolicy(
                    granted_aliases=frozenset({"nexa-reasoning"})
                )
            },
        )
        response = await make_gateway(catalog=catalog).chat(ask("hi", model="nexa-reasoning"), ctx)
        assert response.model == "nexa-reasoning"

    async def test_grants_do_not_leak_to_other_organizations(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        other = uuid4()
        catalog = StaticCatalog(
            make_registry(),
            {other: OrganizationPolicy(granted_aliases=frozenset({"nexa-reasoning"}))},
        )
        with pytest.raises(AIModelUnavailable):
            await make_gateway(catalog=catalog).chat(ask("hi", model="nexa-reasoning"), ctx)

    async def test_quota_is_checked_before_the_provider(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, provider: MockProvider
    ) -> None:
        class Exhausted:
            async def reserve(self, *_: object, **__: object) -> object:
                raise AIQuotaExceeded(retry_after_seconds=30)

            async def settle(self, *_: object, **__: object) -> None:
                return None

        with pytest.raises(AIQuotaExceeded) as caught:
            await make_gateway(quota=Exhausted()).chat(ask("hi"), ctx)
        assert caught.value.headers["Retry-After"] == "30"
        assert provider.calls == []

    async def test_worst_case_is_reserved_then_settled_with_the_real_count(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        reserved: list[int] = []
        settled: list[tuple[object, int]] = []

        class Recording:
            async def reserve(self, *_: object, tokens: int, **__: object) -> object:
                reserved.append(tokens)
                return "ticket-1"

            async def settle(self, ticket: object, *, tokens_used: int) -> None:
                settled.append((ticket, tokens_used))

        gateway = make_gateway(quota=Recording())
        await gateway.chat(ask("hi", max_output_tokens=500), ctx)
        assert reserved[0] >= 500  # the whole output budget is held up front
        assert settled == [("ticket-1", usage.records[0].total_tokens)]

        # A structured request reserves room for the repair turn as well.
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        fmt = {"type": "json_schema", "schema": schema}
        await gateway.chat(ask("hi", max_output_tokens=500, response_format=fmt), ctx)
        assert reserved[1] > 2 * reserved[0]

    async def test_failed_calls_release_their_reservation(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        settled: list[int] = []

        class Recording:
            async def reserve(self, *_: object, **__: object) -> object:
                return object()

            async def settle(self, ticket: object, *, tokens_used: int) -> None:
                settled.append(tokens_used)

        with pytest.raises(AIInvalidRequest):
            await make_gateway(quota=Recording()).chat(ask("[[fail:invalid]]"), ctx)
        assert settled == [0]

    async def test_a_reservation_cancelled_in_flight_is_released(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        sent = asyncio.Event()
        land = asyncio.Event()
        released = asyncio.Event()
        settled: list[tuple[object, int]] = []

        class SlowQuota:
            async def reserve(self, *_: object, **__: object) -> object:
                sent.set()
                await land.wait()  # Redis has applied it; the reply is still on its way
                return "ticket-1"

            async def settle(self, ticket: object, *, tokens_used: int) -> None:
                settled.append((ticket, tokens_used))
                released.set()

        gateway = make_gateway(quota=SlowQuota())
        call = asyncio.create_task(gateway.chat(ask("hi"), ctx))
        await sent.wait()
        call.cancel()  # a stop (or a shutdown) while the reservation is in flight
        with pytest.raises(asyncio.CancelledError):
            await call
        land.set()
        async with asyncio.timeout(2):
            await released.wait()
        await gateway.drain()
        assert settled == [("ticket-1", 0)]  # released, not held until it expires
        assert usage.records[-1].status == "cancelled"
        assert usage.records[-1].total_tokens == 0

    async def test_a_raised_error_names_its_usage_row(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        with pytest.raises(AITimeout) as raised:
            await make_gateway().chat(ask("[[fail:timeout]]"), ctx)
        record = usage.records[-1]
        assert raised.value.call_id == record.id
        assert raised.value.usage is not None
        assert raised.value.usage.total_tokens == record.total_tokens

    async def test_errors_before_the_call_is_planned_name_no_row(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        with pytest.raises(AIModelUnavailable) as raised:
            await make_gateway().chat(ask("hi", model="nexa-nope"), ctx)
        assert raised.value.call_id is None
        assert usage.records == []

    @pytest.mark.parametrize(
        "message",
        [
            Message(role=Role.USER, content=[ImagePart(url="https://example.com/a.png")]),
            Message(role=Role.TOOL, content="result"),
        ],
    )
    async def test_unsupported_inputs_are_refused(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, message: Message
    ) -> None:
        with pytest.raises(AIInvalidRequest):
            await make_gateway().chat(ChatRequest(messages=[message]), ctx)

    async def test_context_too_large_never_reaches_a_provider(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, provider: MockProvider
    ) -> None:
        with pytest.raises(AIContextTooLarge):
            await make_gateway().chat(ask("x" * 70_000), ctx)
        assert provider.calls == []


class TestRetriesAndFallback:
    async def test_rate_limit_is_retried_on_the_same_model(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, provider: MockProvider
    ) -> None:
        provider.script_failures("mock-primary", "rate_limit")
        response = await make_gateway().chat(ask("hi"), ctx)
        assert response.provider_model == "mock-primary"
        assert response.attempts == 2
        assert response.fallback_used is False

    async def test_provider_outage_falls_back(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        response = await make_gateway().chat(ask("hi"), ctx)
        assert response.fallback_used is False

        provider = MockProvider(failures={"mock-primary": ["provider"] * 3})
        response = await make_gateway(providers={"mock": provider}).chat(ask("hi"), ctx)
        assert response.provider_model == "mock-backup"
        assert response.fallback_used is True
        assert response.attempts == 4  # 1 + 2 retries on primary, then backup
        assert usage.records[-1].fallback_used is True
        assert usage.records[-1].provider_model == "mock-backup"

    @pytest.mark.parametrize("kind", ["timeout", "model"])
    async def test_fallback_without_retry(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, kind: str
    ) -> None:
        provider = MockProvider(failures={"mock-primary": [kind]})
        response = await make_gateway(providers={"mock": provider}).chat(ask("hi"), ctx)
        assert response.provider_model == "mock-backup"
        assert response.attempts == 2

    @pytest.mark.parametrize(
        ("kind", "error"),
        [
            ("invalid", AIInvalidRequest),
            ("content", AIContentRejected),
        ],
    )
    async def test_no_retry_and_no_fallback(
        self,
        make_gateway: GatewayFactory,
        ctx: AIRequestContext,
        usage: MemoryUsageSink,
        kind: str,
        error: type[Exception],
    ) -> None:
        provider = MockProvider(failures={"mock-primary": [kind]})
        with pytest.raises(error):
            await make_gateway(providers={"mock": provider}).chat(ask("hi"), ctx)
        assert len(provider.calls) == 1
        [record] = usage.records
        assert record.status == "error"
        assert record.error_code is not None
        assert record.total_tokens == 0

    async def test_account_faults_skip_every_model_at_that_provider(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        # Both targets are at the "mock" provider: a rejected key fails them all at once.
        provider = MockProvider(failures={"mock-primary": ["auth"]})
        with pytest.raises(AIAuthenticationError):
            await make_gateway(providers={"mock": provider}).chat(ask("hi"), ctx)
        assert [c.model for c in provider.calls] == ["mock-primary"]

    async def test_context_error_falls_back_only_to_larger_windows(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        provider = MockProvider(failures={"mock-primary": ["context"]})
        response = await make_gateway(providers={"mock": provider}).chat(ask("hi"), ctx)
        assert response.provider_model == "mock-backup"  # 16k window > primary's 8k

        provider = MockProvider(failures={"mock-backup": ["context"]})
        with pytest.raises(AIContextTooLarge):
            # nexa-fast has only the backup model: nothing larger to try.
            await make_gateway(providers={"mock": provider}).chat(ask("hi", model="nexa-fast"), ctx)
        assert len(provider.calls) == 1

    async def test_timed_out_attempts_are_metered_as_estimates(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        clean = await make_gateway().chat(ask("hi"), ctx)
        provider = MockProvider(failures={"mock-primary": ["timeout"]})
        await make_gateway(providers={"mock": provider}).chat(ask("hi"), ctx)
        record = usage.records[-1]
        assert record.usage_source == "estimated"
        # The serving call's input plus the abandoned attempt's estimated input.
        assert record.input_tokens > clean.usage.input_tokens

    async def test_every_candidate_failing_reports_the_most_useful_error(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        provider = MockProvider(
            failures={"mock-primary": ["rate_limit"] * 3, "mock-backup": ["auth"]}
        )
        with pytest.raises(AIRateLimited) as caught:
            await make_gateway(providers={"mock": provider}).chat(ask("hi"), ctx)
        assert "Retry-After" in caught.value.headers

        provider = MockProvider(
            failures={"mock-primary": ["provider"] * 3, "mock-backup": ["model"]}
        )
        with pytest.raises(AIProviderError):
            await make_gateway(providers={"mock": provider}).chat(ask("hi"), ctx)

    async def test_attempts_are_bounded(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        provider = MockProvider(
            failures={"mock-primary": ["provider"] * 9, "mock-backup": ["provider"] * 9}
        )
        with pytest.raises(AIProviderError):
            await make_gateway(providers={"mock": provider}, max_attempts=4).chat(ask("hi"), ctx)
        assert len(provider.calls) == 4

    async def test_timeout_is_enforced(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        # Real sleeping provider, 50 ms request timeout: both candidates time out.
        slow = MockProvider()
        gateway = make_gateway(providers={"mock": slow}, timeouts=Timeouts(request=0.05))
        with pytest.raises(AITimeout):
            await gateway.chat(ask("[[slow:2000]] hi"), ctx)
        assert len(slow.calls) == 2

    async def test_unexpected_adapter_bugs_become_internal_errors(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        class Broken(MockProvider):
            async def chat(self, call: object) -> object:  # type: ignore[override]
                raise KeyError("secret detail")

        from nexa_ai.errors import AIInternalError

        with pytest.raises(AIInternalError) as caught:
            await make_gateway(providers={"mock": Broken()}).chat(ask("hi"), ctx)
        assert "secret detail" not in caught.value.message
        assert usage.records[-1].error_code == "AI_INTERNAL_ERROR"


class TestStructuredOutput:
    async def test_valid_output_is_parsed(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        response = await make_gateway().chat(
            ask("Describe Ajit", response_format={"type": "json_schema", "schema": PERSON}), ctx
        )
        assert isinstance(response.parsed, dict)
        assert set(response.parsed) == {"name", "skills"}
        assert response.attempts == 1

    async def test_one_repair_attempt(
        self,
        make_gateway: GatewayFactory,
        ctx: AIRequestContext,
        usage: MemoryUsageSink,
        provider: MockProvider,
    ) -> None:
        response = await make_gateway().chat(
            ask("[[bad_json_once]]", response_format={"type": "json_schema", "schema": PERSON}),
            ctx,
        )
        assert response.parsed is not None
        assert response.attempts == 2
        repair_turn = provider.calls[-1].request.messages[-1]
        assert repair_turn.role is Role.USER
        assert "did not match the required JSON schema" in repair_turn.text
        # Both calls' tokens are on the usage record.
        assert usage.records[-1].input_tokens > provider.calls[0].request.messages[0].text.count(
            " "
        )

    async def test_invalid_after_repair(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        with pytest.raises(AIOutputInvalid):
            await make_gateway().chat(
                ask("[[bad_json]]", response_format={"type": "json_schema", "schema": PERSON}), ctx
            )
        assert usage.records[-1].status == "error"
        assert usage.records[-1].error_code == "AI_OUTPUT_INVALID"
        assert usage.records[-1].output_tokens > 0  # the failed answers still cost tokens

    async def test_models_without_structured_output_are_skipped(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        with pytest.raises(AIModelUnavailable, match="structured_output"):
            await make_gateway().chat(
                ask(
                    "hi",
                    model="nexa-basic",
                    response_format={"type": "json_schema", "schema": PERSON},
                ),
                ctx,
            )

    async def test_truncated_json_is_not_repaired(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        class Truncating(MockProvider):
            async def chat(self, call: ProviderCall) -> ProviderResult:
                self.calls.append(call)
                return ProviderResult(
                    content='{"name": "Aj',
                    finish_reason=FinishReason.LENGTH,
                    usage=TokenCounts(5, 5),
                )

        truncating = Truncating()
        with pytest.raises(AIOutputInvalid, match="max_output_tokens"):
            await make_gateway(providers={"mock": truncating}).chat(
                ask("hi", response_format={"type": "json_schema", "schema": PERSON}), ctx
            )
        assert len(truncating.calls) == 1

    async def test_refusal_is_content_rejected_without_repair(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        class Refusing(MockProvider):
            async def chat(self, call: ProviderCall) -> ProviderResult:
                self.calls.append(call)
                return ProviderResult(
                    content="", finish_reason=FinishReason.CONTENT_FILTER, usage=TokenCounts(7, 0)
                )

        refusing = Refusing()
        with pytest.raises(AIContentRejected):
            await make_gateway(providers={"mock": refusing}).chat(
                ask("hi", response_format={"type": "json_schema", "schema": PERSON}), ctx
            )
        assert len(refusing.calls) == 1
        assert usage.records[-1].input_tokens == 7  # the refused call is still metered

    async def test_unpriced_models_record_unknown_cost(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
    ) -> None:
        response = await make_gateway().chat(ask("hi", model="nexa-basic"), ctx)
        assert response.usage.cost_usd is None
        assert usage.records[-1].cost_micros is None

    async def test_invalid_schema(
        self, make_gateway: GatewayFactory, ctx: AIRequestContext
    ) -> None:
        with pytest.raises(AIInvalidRequest):
            await make_gateway().chat(
                ask("hi", response_format={"type": "json_schema", "schema": {"type": "array"}}), ctx
            )


async def test_cancelled_call_is_still_recorded(
    make_gateway: GatewayFactory, ctx: AIRequestContext, usage: MemoryUsageSink
) -> None:
    gateway = make_gateway(providers={"mock": MockProvider()})
    task = asyncio.create_task(gateway.chat(ask("[[slow:2000]] hi"), ctx))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await gateway.drain()
    assert usage.records[-1].status == "cancelled"

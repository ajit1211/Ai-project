"""Types, cost, retry policy, structured output and routing: the pure parts of the gateway."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from nexa_ai.cost import Price, cost_micros, micros_to_usd, usd_to_micros
from nexa_ai.errors import (
    AIContextTooLarge,
    AIInvalidRequest,
    AIModelUnavailable,
    AIProviderError,
    AIRateLimited,
    AITimeout,
)
from nexa_ai.providers.base import TokenCounts
from nexa_ai.registry import Alias, OrganizationPolicy
from nexa_ai.retry import RetryPolicy
from nexa_ai.router import candidates, explain_empty, required_capabilities, resolve_alias
from nexa_ai.structured import OutputMismatch, check_schema, parse_structured
from nexa_ai.testing import BACKUP, CHAT_ONLY, PRIMARY, ask, make_registry
from nexa_ai.types import Capability, ChatRequest, ImagePart, Message, Role, TextPart

# --- Request model ---------------------------------------------------------------------------


class TestChatRequest:
    def test_minimal_request(self) -> None:
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Explain FastAPI dependency injection."}]}
        )
        assert request.model is None
        assert request.messages[0].text == "Explain FastAPI dependency injection."
        assert request.feature == "api"

    def test_parts_and_text(self) -> None:
        message = Message(role=Role.USER, content=[TextPart(text="a"), TextPart(text="b")])
        assert message.text == "ab"

    @pytest.mark.parametrize(
        "payload",
        [
            {"messages": []},
            {"messages": [{"role": "system", "content": "only a system prompt"}]},
            {"messages": [{"role": "robot", "content": "hi"}]},
            {"messages": [{"role": "user", "content": "hi"}], "temperature": 3},
            {"messages": [{"role": "user", "content": "hi"}], "max_output_tokens": 0},
            {"messages": [{"role": "user", "content": "hi"}], "model": "bad model!"},
            {"messages": [{"role": "user", "content": "hi"}], "stop": ["", "x"]},
            {"messages": [{"role": "user", "content": "hi"}], "feature": "workflow"},
            # Provider-specific parameters are not part of the normalized interface.
            {"messages": [{"role": "user", "content": "hi"}], "top_k": 5},
            # Identity can never come from the body.
            {"messages": [{"role": "user", "content": "hi"}], "organization_id": "x"},
        ],
    )
    def test_invalid_requests_are_refused(self, payload: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            ChatRequest.model_validate(payload)

    def test_json_schema_format_uses_the_schema_key(self) -> None:
        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_schema", "schema": {"type": "object"}},
            }
        )
        assert request.wants_structured_output


# --- Cost ------------------------------------------------------------------------------------


class TestCost:
    PRICE = Price(input_micros_per_million=3_000_000, output_micros_per_million=15_000_000)

    def test_total_tokens_is_input_plus_output(self) -> None:
        usage = TokenCounts(input_tokens=100, output_tokens=200)
        assert usage.total_tokens == 300

    def test_cost_formula(self) -> None:
        # 1,000 in at $3/M + 2,000 out at $15/M = $0.003 + $0.030
        usage = TokenCounts(input_tokens=1_000, output_tokens=2_000)
        assert cost_micros(usage, self.PRICE) == 33_000
        assert micros_to_usd(33_000) == "0.033000"

    def test_cache_reads_and_writes_have_their_own_rates(self) -> None:
        price = Price(
            3_000_000,
            15_000_000,
            cached_input_micros_per_million=300_000,
            cache_write_micros_per_million=3_750_000,
        )
        # 1M input tokens in total: 400k read from cache, 100k written to it, 500k plain.
        usage = TokenCounts(
            input_tokens=1_000_000, cached_input_tokens=400_000, cache_write_input_tokens=100_000
        )
        expected = 500_000 * 3 + 400_000 * 3 // 10 + 100_000 * 375 // 100
        assert cost_micros(usage, price) == expected

    def test_cache_counts_never_exceed_the_input(self) -> None:
        price = Price(3_000_000, 15_000_000, cached_input_micros_per_million=0)
        usage = TokenCounts(input_tokens=10, cached_input_tokens=50)
        assert cost_micros(usage, price) == 0  # all ten are cached reads, priced at zero

    def test_rounds_to_the_nearest_micro_dollar(self) -> None:
        assert cost_micros(TokenCounts(input_tokens=1), self.PRICE) == 3
        tiny = Price(input_micros_per_million=400_000, output_micros_per_million=0)
        assert cost_micros(TokenCounts(input_tokens=1), tiny) == 0
        assert cost_micros(TokenCounts(input_tokens=2), tiny) == 1  # 0.8 rounds up

    def test_conversions(self) -> None:
        assert usd_to_micros("3.00") == 3_000_000
        assert usd_to_micros(Decimal("0.25")) == 250_000
        assert micros_to_usd(1) == "0.000001"


# --- Retry policy ----------------------------------------------------------------------------


class TestRetryPolicy:
    policy = RetryPolicy(max_retries=2, base_delay=0.5, max_delay=4.0, max_retry_after=4.0)

    def test_retryable_errors_back_off_with_jitter(self) -> None:
        error = AIProviderError()
        for attempt in range(2):
            delay = self.policy.delay(error, attempt)
            assert delay is not None
            assert 0 <= delay <= min(4.0, 0.5 * 2**attempt)

    def test_retries_are_bounded(self) -> None:
        assert self.policy.delay(AIProviderError(), 2) is None

    def test_short_retry_after_is_honoured_long_is_not(self) -> None:
        assert self.policy.delay(AIRateLimited(retry_after_seconds=1.5), 0) == 1.5
        assert self.policy.delay(AIRateLimited(retry_after_seconds=30), 0) is None

    @pytest.mark.parametrize(
        "error", [AITimeout(), AIInvalidRequest(), AIContextTooLarge(), AIModelUnavailable()]
    )
    def test_non_retryable_errors(self, error: AIProviderError) -> None:
        assert self.policy.delay(error, 0) is None


# --- Structured output -----------------------------------------------------------------------

PERSON = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "skills"],
    "additionalProperties": False,
}


class TestStructuredOutput:
    def test_valid_document(self) -> None:
        value = parse_structured('{"name": "Ajit", "skills": ["Python", "Django"]}', PERSON)
        assert value == {"name": "Ajit", "skills": ["Python", "Django"]}

    def test_code_fences_are_tolerated(self) -> None:
        text = '```json\n{"name": "Ajit", "skills": []}\n```'
        assert parse_structured(text, PERSON)["name"] == "Ajit"

    def test_invalid_json(self) -> None:
        with pytest.raises(OutputMismatch) as caught:
            parse_structured("{name: Ajit}", PERSON)
        assert "not valid JSON" in caught.value.reason
        assert caught.value.summary == "invalid JSON"

    def test_schema_violations_are_described(self) -> None:
        with pytest.raises(OutputMismatch) as caught:
            parse_structured('{"name": 5, "extra": true}', PERSON)
        assert "skills" in caught.value.reason
        assert "name" in caught.value.reason

    def test_invalid_schema_is_refused_up_front(self) -> None:
        with pytest.raises(AIInvalidRequest):
            check_schema({"type": "object", "properties": {"a": {"type": "not-a-type"}}})
        with pytest.raises(AIInvalidRequest, match="JSON object"):
            check_schema({"type": "array"})

    def test_oversized_schema_is_refused(self) -> None:
        huge = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(5000)}}
        with pytest.raises(AIInvalidRequest, match="too large"):
            check_schema(huge)


# --- Router ----------------------------------------------------------------------------------


class TestRouter:
    registry = make_registry()
    policy = OrganizationPolicy()

    def test_default_alias(self) -> None:
        assert resolve_alias(self.registry, self.policy, None).name == "nexa-balanced"

    def test_unknown_and_inactive_aliases(self) -> None:
        with pytest.raises(AIModelUnavailable, match="Unknown model"):
            resolve_alias(self.registry, self.policy, "nexa-imaginary")
        inactive = make_registry(Alias("nexa-off", "Off", is_active=False, targets=(PRIMARY,)))
        with pytest.raises(AIModelUnavailable):
            resolve_alias(inactive, self.policy, "nexa-off")

    def test_restricted_alias_needs_a_grant(self) -> None:
        with pytest.raises(AIModelUnavailable, match="not enabled"):
            resolve_alias(self.registry, self.policy, "nexa-reasoning")
        granted = OrganizationPolicy(granted_aliases=frozenset({"nexa-reasoning"}))
        assert resolve_alias(self.registry, granted, "nexa-reasoning").name == "nexa-reasoning"

    def test_candidates_follow_priority(self) -> None:
        alias = self.registry.aliases["nexa-balanced"]
        found = candidates(
            alias,
            required=frozenset({Capability.CHAT}),
            available_providers={"mock"},
            estimated_input_tokens=10,
            max_output_tokens=100,
        )
        assert [c.model.provider_model for c in found] == ["mock-primary", "mock-backup"]
        assert [c.is_fallback for c in found] == [False, True]

    def test_unconfigured_providers_are_skipped(self) -> None:
        alias = self.registry.aliases["nexa-balanced"]
        found = candidates(
            alias,
            required=frozenset({Capability.CHAT}),
            available_providers={"anthropic"},
            estimated_input_tokens=10,
            max_output_tokens=100,
        )
        assert found == []
        error = explain_empty(
            alias,
            required=frozenset({Capability.CHAT}),
            available_providers={"anthropic"},
            estimated_input_tokens=10,
        )
        # An operations fault, not the caller's: 503 and retryable.
        assert isinstance(error, AIProviderError)
        assert error.http_status == 503

    def test_capability_mismatch(self) -> None:
        alias = self.registry.aliases["nexa-basic"]
        required = required_capabilities(ask("hi"), stream=True)
        assert Capability.STREAMING in required
        assert (
            candidates(
                alias,
                required=required,
                available_providers={"mock"},
                estimated_input_tokens=10,
                max_output_tokens=100,
            )
            == []
        )
        error = explain_empty(
            alias, required=required, available_providers={"mock"}, estimated_input_tokens=10
        )
        assert "streaming" in error.message

    def test_vision_requirement_is_derived_from_parts(self) -> None:
        request = ChatRequest(
            messages=[Message(role=Role.USER, content=[ImagePart(url="https://x/y.png")])]
        )
        assert Capability.VISION in required_capabilities(request, stream=False)

    def test_context_window_drops_small_models(self) -> None:
        alias = self.registry.aliases["nexa-balanced"]
        found = candidates(
            alias,
            required=frozenset({Capability.CHAT}),
            available_providers={"mock"},
            estimated_input_tokens=7_000,
            max_output_tokens=1_500,
        )
        # PRIMARY (8k) cannot hold 7k + 1.5k; BACKUP (16k) can.
        assert [c.model for c in found] == [BACKUP]
        error = explain_empty(
            alias,
            required=frozenset({Capability.CHAT}),
            available_providers={"mock"},
            estimated_input_tokens=20_000,
        )
        assert isinstance(error, AIContextTooLarge)

    def test_alias_capabilities_are_the_union_of_active_targets(self) -> None:
        alias = Alias("mixed", "Mixed", targets=(CHAT_ONLY, PRIMARY))
        assert Capability.STRUCTURED_OUTPUT in alias.capabilities
        assert alias.context_window == 8_000


class TestUntrustedSchemas:
    """The response schema comes from the client and must never cause I/O or runaway work."""

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {"a": {"$ref": "http://169.254.169.254/latest/"}}},
            {"type": "object", "properties": {"a": {"$ref": "file:///etc/passwd"}}},
            {"type": "object", "properties": {"a": {"$ref": "data:application/json,{}"}}},
            {"type": "object", "$id": "http://evil.example/root.json", "properties": {}},
            {"type": "object", "$schema": "http://evil.example/meta"},
            {"type": "object", "properties": {"a": {"$dynamicRef": "http://x/y"}}},
            {"type": "object", "properties": {"a": {"type": "string", "pattern": "(a+)+$"}}},
            {"type": "object", "patternProperties": {"^x": {"type": "string"}}},
        ],
    )
    def test_unsafe_schemas_are_refused_without_any_fetch(
        self, schema: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_network(*_: object, **__: object) -> None:
            raise AssertionError("the validator tried to fetch something")

        monkeypatch.setattr("urllib.request.urlopen", no_network)
        with pytest.raises(AIInvalidRequest):
            check_schema(schema)

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {"a": {"$ref": "#"}}, "required": ["a"]},
            {"type": "object", "$ref": "#"},
            {
                "type": "object",
                "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},
                "properties": {"x": {"$ref": "#/$defs/a"}},
            },
        ],
    )
    def test_recursive_schemas_are_refused_not_a_crash(self, schema: dict[str, object]) -> None:
        with pytest.raises(AIInvalidRequest, match="Recursive"):
            check_schema(schema)

    def test_references_that_fan_out_are_refused_before_validating_anything(self) -> None:
        """Each level references the next one twice: 2^n evaluations without a loop. The
        validator does not memoize, so 40 levels would never finish."""
        defs: dict[str, object] = {"d40": {"type": "object"}}
        for level in range(40):
            nxt = f"#/$defs/d{level + 1}"
            defs[f"d{level}"] = {"allOf": [{"$ref": nxt}, {"$ref": nxt}]}
        schema = {"type": "object", "allOf": [{"$ref": "#/$defs/d0"}], "$defs": defs}
        with pytest.raises(AIInvalidRequest, match="expand too much"):
            check_schema(schema)

    def test_a_definition_shared_by_many_properties_is_fine(self) -> None:
        schema = {
            "type": "object",
            "$defs": {"name": {"type": "string", "maxLength": 80}},
            "properties": {f"p{i}": {"$ref": "#/$defs/name"} for i in range(30)},
        }
        check_schema(schema)
        assert parse_structured('{"p1": "Ada"}', schema) == {"p1": "Ada"}

    def test_missing_local_reference(self) -> None:
        with pytest.raises(AIInvalidRequest, match="reference"):
            check_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}})

    def test_local_references_still_work(self) -> None:
        schema = {
            "type": "object",
            "$defs": {"skill": {"type": "string"}},
            "properties": {"skills": {"type": "array", "items": {"$ref": "#/$defs/skill"}}},
            "required": ["skills"],
        }
        check_schema(schema)
        assert parse_structured('{"skills": ["Python"]}', schema) == {"skills": ["Python"]}
        with pytest.raises(OutputMismatch):
            parse_structured('{"skills": [1]}', schema)

    def test_depth_limit(self) -> None:
        deep: dict[str, object] = {"type": "string"}
        for _ in range(30):
            deep = {"type": "object", "properties": {"x": deep}}
        with pytest.raises(AIInvalidRequest, match="deeply"):
            check_schema(deep)

    def test_format_is_only_an_annotation(self) -> None:
        schema = {"type": "object", "properties": {"email": {"type": "string", "format": "email"}}}
        check_schema(schema)
        assert parse_structured('{"email": "not an email"}', schema)["email"] == "not an email"

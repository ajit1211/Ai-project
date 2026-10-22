"""The tool contract without a database: registration rules, argument and output validation,
the calculator's parser (and what it refuses to do), the clock, and what the model is told
about a result (docs/architecture/tools.md)."""

import inspect
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from nexa_ai.types import ToolCallPart
from nexa_common.access import Principal
from nexa_runtime.tools.builtin import calculator as calculator_module
from nexa_runtime.tools.builtin.calculator import CALCULATOR, evaluate, formatted
from nexa_runtime.tools.builtin.clock import make_clock_tool
from nexa_runtime.tools.executor import DECLINED, result_part
from nexa_runtime.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolFailure,
    ToolOutput,
    ToolRegistry,
    registry_of,
)
from nexa_runtime.tools.validation import (
    ArgumentsRejected,
    OutputRejected,
    arguments_hash,
    redacted,
    validate_arguments,
    validate_output,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _noop(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    return ToolOutput(data={"items": []})


def definition(**changes: Any) -> ToolDefinition:
    values: dict[str, Any] = {
        "id": "list_things",
        "name": "List things",
        "description": "Lists things.",
        "version": "1",
        "category": "test",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 20},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        },
        "handler": _noop,
        "redact": frozenset({"query"}),
    }
    values.update(changes)
    return ToolDefinition(**values)


def call(arguments: dict[str, Any] | None = None, raw: str | None = None) -> ToolCallPart:
    return ToolCallPart(
        id="call_1", name="list_things", arguments=arguments or {}, raw_arguments=raw
    )


def context() -> ToolContext:
    principal = Principal(user_id=uuid4(), session_id=None, organization_id=uuid4())
    return ToolContext(
        principal=principal, conversation_id=uuid4(), message_id=uuid4(), invocation_id=uuid4()
    )


class TestRegistry:
    def test_a_valid_tool_registers_once(self) -> None:
        registry = ToolRegistry()
        registry.register(definition())
        assert registry.get("list_things") is not None
        assert "list_things" in registry
        with pytest.raises(ValueError, match="already registered"):
            registry.register(definition())

    @pytest.mark.parametrize(
        ("changes", "reason"),
        [
            ({"id": "List-Things"}, "id"),
            ({"version": "v1"}, "version"),
            ({"description": ""}, "description"),
            ({"input_schema": {"type": "string"}}, "input schema"),
            (
                {"input_schema": {"type": "object", "properties": {}}},
                "additionalProperties",
            ),
            (
                {"output_schema": {"type": "object", "properties": {"a": {"pattern": "x"}}}},
                "output",
            ),
            ({"timeout_seconds": 0}, "timeout"),
            ({"timeout_seconds": 61}, "timeout"),
            ({"max_output_bytes": 10**7}, "limits"),
            ({"read_only": False, "max_retries": 1, "concurrency_safe": False}, "retried"),
            ({"read_only": False}, "runs alone"),
            ({"risk": "high"}, "needs approval"),
            ({"requires_approval": True}, "describe"),
            (
                {
                    "requires_approval": True,
                    "approval_details": lambda a: {},
                    "redact": frozenset({"query"}),
                },
                "redact",
            ),
            ({"scopes": frozenset()}, "scope"),
        ],
    )
    def test_unsafe_or_ambiguous_definitions_are_refused(
        self, changes: dict[str, Any], reason: str
    ) -> None:
        with pytest.raises(ValueError, match=reason):
            registry_of([definition(**changes)])

    def test_a_synchronous_handler_is_refused(self) -> None:
        def sync(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
            return ToolOutput(data={"items": []})

        with pytest.raises(ValueError, match="async"):
            registry_of([definition(handler=sync)])

    def test_the_model_sees_only_id_description_and_schema(self) -> None:
        spec = definition().spec()
        assert spec.model_dump() == {
            "name": "list_things",
            "description": "Lists things.",
            "input_schema": definition().input_schema,
        }


class TestArguments:
    def test_valid_arguments_get_their_defaults(self) -> None:
        assert validate_arguments(definition(), call({"query": "a"})) == {"query": "a", "limit": 3}

    @pytest.mark.parametrize(
        ("arguments", "raw", "code"),
        [
            ({}, None, "invalid_arguments"),  # missing required
            ({"query": "a", "extra": 1}, None, "invalid_arguments"),  # unknown property
            ({"query": 5}, None, "invalid_arguments"),  # wrong type
            ({"query": "a", "limit": 50}, None, "invalid_arguments"),  # out of range
            ({}, "not json", "invalid_arguments"),  # malformed
            ({"query": "a" * 5000}, None, "arguments_too_large"),
        ],
    )
    def test_invalid_arguments_are_rejected(
        self, arguments: dict[str, Any], raw: str | None, code: str
    ) -> None:
        with pytest.raises(ArgumentsRejected) as raised:
            validate_arguments(definition(), call(arguments, raw))
        assert raised.value.code == code

    def test_errors_never_quote_the_value(self) -> None:
        with pytest.raises(ArgumentsRejected) as raised:
            validate_arguments(definition(), call({"query": "SECRET-" * 3}))
        assert "SECRET" not in raised.value.message
        assert "query" in raised.value.message

    def test_deep_nesting_is_rejected(self) -> None:
        nested: Any = "x"
        for _ in range(12):
            nested = {"a": nested}
        with pytest.raises(ArgumentsRejected):
            validate_arguments(definition(), call({"query": "a", "deep": nested}))

    def test_the_hash_identifies_one_exact_action(self) -> None:
        tool = definition()
        first = arguments_hash(tool, {"query": "a", "limit": 3})
        assert first == arguments_hash(tool, {"limit": 3, "query": "a"})  # key order is irrelevant
        assert first != arguments_hash(tool, {"query": "b", "limit": 3})
        assert first != arguments_hash(definition(version="2"), {"query": "a", "limit": 3})

    def test_redacted_names_are_not_stored(self) -> None:
        assert redacted(definition(), {"query": "secret", "limit": 3}) == {
            "query": "[redacted]",
            "limit": 3,
        }


class TestOutput:
    def test_output_must_match_its_schema(self) -> None:
        with pytest.raises(OutputRejected):
            validate_output(definition(), {"items": [1, 2]})
        with pytest.raises(OutputRejected):
            validate_output(definition(), ["not", "an", "object"])

    def test_long_lists_are_shortened_and_marked(self) -> None:
        tool = definition(max_output_bytes=200)
        data, truncated = validate_output(tool, {"items": [f"item {n:03}" for n in range(100)]})
        assert truncated
        assert 0 < len(data["items"]) < 100
        assert len(json.dumps(data)) <= 220

    def test_what_cannot_be_shortened_fails(self) -> None:
        tool = definition(
            max_output_bytes=50,
            output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        )
        with pytest.raises(OutputRejected) as raised:
            validate_output(tool, {"text": "x" * 500})
        assert raised.value.code == "output_too_large"


class TestCalculator:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("2 + 3 * 4", "14"),
            ("(2 + 3) * 4", "20"),
            ("2 ^ 3 ^ 2", "512"),  # right-associative
            ("2 ** 10", "1024"),
            ("-3 ^ 2", "-9"),
            ("--3", "3"),
            ("10 / 4", "2.5"),
            ("10 // 4", "2"),
            ("10 % 4", "2"),
            ("-7 % 3", "2"),
            ("0.1 + 0.2", "0.3"),
            ("sqrt(16) + abs(-2)", "6"),
            ("round(pi, 2)", "3.14"),
            ("max(1, 5, 3) - min(4, 2)", "3"),
            ("log10(1000) + ln(e)", "4"),
            ("log(8, 2)", "3"),
            ("1e3 + .5", "1000.5"),
            ("floor(2.7) + ceil(2.1)", "5"),
        ],
    )
    def test_arithmetic(self, expression: str, expected: str) -> None:
        assert formatted(evaluate(expression)) == expected

    @pytest.mark.parametrize(
        ("expression", "code"),
        [
            ("1 / 0", "division_by_zero"),
            ("5 % 0", "division_by_zero"),
            ("sqrt(-1)", "math_error"),
            ("ln(0)", "math_error"),
            ("9 ^ 9 ^ 9", "too_large"),
            ("10 ^ 400", "too_large"),
            ("exp(1000)", "too_large"),
            ("2 +", "invalid_expression"),
            ("(1 + 2", "invalid_expression"),
            ("2 (3)", "invalid_expression"),
            ("", "invalid_expression"),
            ("1" * 201, "invalid_expression"),
            ("(" * 30 + "1" + ")" * 30, "invalid_expression"),
            # Code is not arithmetic: none of these is ever evaluated.
            ("__import__('os').system('id')", "invalid_expression"),
            ("().__class__.__bases__", "invalid_expression"),
            ("open('/etc/passwd')", "invalid_expression"),
            ("exec", "invalid_expression"),
            ("1; 2", "invalid_expression"),
            ("a = 1", "invalid_expression"),
            ("lambda: 1", "invalid_expression"),
        ],
    )
    def test_refusals(self, expression: str, code: str) -> None:
        with pytest.raises(ToolFailure) as raised:
            evaluate(expression)
        assert raised.value.code == code

    def test_no_dynamic_evaluation_anywhere(self) -> None:
        source = inspect.getsource(calculator_module)
        # The builtins themselves, not methods such as re.compile.
        assert not re.search(r"(?<![\w.])(eval|exec|compile|__import__)\(", source)
        assert "import ast" not in source

    async def test_the_handler_reports_text_and_number(self) -> None:
        output = await CALCULATOR.handler(context(), {"expression": "6 *  7"})
        assert output.data == {"expression": "6 * 7", "result": 42.0, "result_text": "42"}


class TestClock:
    async def test_the_injected_clock_is_used(self) -> None:
        fixed = datetime(2026, 10, 22, 9, 30, 15, 999, tzinfo=UTC)
        tool = make_clock_tool(lambda: fixed)
        output = await tool.handler(context(), {"timezone": "Asia/Kolkata"})
        assert output.data["utc"] == "2026-10-22T09:30:15Z"
        assert output.data["timezone"] == "UTC"
        assert output.data["weekday"] == "Thursday"
        assert output.data["local"]["time"] == "2026-10-22T15:00:15+05:30"
        validate_output(tool, output.data)

    async def test_an_unknown_zone_is_an_error_not_utc(self) -> None:
        tool = make_clock_tool(lambda: datetime(2026, 1, 1, tzinfo=UTC))
        with pytest.raises(ToolFailure) as raised:
            await tool.handler(context(), {"timezone": "Mars/Olympus"})
        assert raised.value.code == "unknown_timezone"


class TestWhatTheModelIsTold:
    def test_results_are_data_with_an_explicit_status(self) -> None:
        ok = result_part("c1", "calculator", "succeeded", {"result": 4}, False, "", "")
        assert not ok.is_error
        assert json.loads(ok.content) == {
            "status": "ok",
            "tool": "calculator",
            "result": {"result": 4},
        }
        cut = result_part("c1", "list_things", "succeeded", {"items": []}, True, "", "")
        assert json.loads(cut.content)["truncated"] is True

    def test_failures_are_never_presented_as_results(self) -> None:
        failed = result_part("c1", "calculator", "failed", None, False, "division_by_zero", "No.")
        assert failed.is_error
        assert json.loads(failed.content) == {
            "status": "error",
            "tool": "calculator",
            "error": {"code": "division_by_zero", "message": "No."},
        }
        declined = result_part("c1", "save_memory", "rejected", None, False, DECLINED, "x")
        assert declined.is_error
        assert json.loads(declined.content)["status"] == "declined"

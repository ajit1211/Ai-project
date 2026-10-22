"""The agent engine's pure parts (Phase 11): the decision contract, the model's context, the
run clock, recovery of in-memory state, and the catalogs Core and Runtime must agree on.
No database and no network."""

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexa_ai.providers.base import ProviderCall, Timeouts
from nexa_ai.providers.mock import MockProvider
from nexa_ai.structured import check_schema
from nexa_ai.types import ChatRequest, JSONSchemaFormat, Message, Role
from nexa_common.errors import ValidationFailed
from nexa_common.ids import new_id
from nexa_common.tool_catalog import BUILTIN_TOOLS, TOOLS_BY_ID
from nexa_runtime.agents import repository as repo
from nexa_runtime.agents.context import (
    build_context,
    goal_message,
    history_messages,
    observation_message,
    system_message,
)
from nexa_runtime.agents.decisions import (
    DECISION_SCHEMA,
    MAX_SUMMARY_CHARS,
    InvalidDecision,
    parse_decision,
)
from nexa_runtime.agents.engine import _call_key, _restore, _Run
from nexa_runtime.tools.audit import ACTIONS
from nexa_runtime.tools.builtin.calculator import CALCULATOR
from nexa_runtime.tools.builtin.clock import make_clock_tool
from nexa_runtime.tools.builtin.documents import make_document_tools
from nexa_runtime.tools.builtin.memory import make_memory_tool
from nexa_runtime.tools.builtin.workspace import make_workspace_tool
from nexa_runtime.tools.registry import ToolDefinition

# --- Decisions -----------------------------------------------------------------------------------


class TestDecisions:
    def test_the_schema_is_accepted_for_structured_output(self) -> None:
        check_schema(DECISION_SCHEMA)
        assert DECISION_SCHEMA["additionalProperties"] is False

    @pytest.mark.parametrize(
        ("value", "action"),
        [
            (
                {"action": "call_tool", "tool": "calculator", "arguments": {"expression": "1"}},
                "call_tool",
            ),
            ({"action": "call_tool", "tool": "current_time"}, "call_tool"),
            ({"action": "report_progress", "message": "Half way."}, "report_progress"),
            (
                {"action": "request_approval", "tool": "send_note", "message": "Sends a note."},
                "request_approval",
            ),
            ({"action": "complete", "answer": "42"}, "complete"),
            ({"action": "complete", "output": {"n": 1}}, "complete"),
            ({"action": "fail", "reason": "No documents.", "summary": "Giving up"}, "fail"),
        ],
    )
    def test_valid_decisions(self, value: dict[str, Any], action: str) -> None:
        assert parse_decision(value).action == action

    @pytest.mark.parametrize(
        ("value", "says"),
        [
            ("call a tool", "JSON object"),
            ({"action": "run_shell", "command": "rm -rf /"}, "must be one of"),
            ({"action": "call_tool"}, "needs a 'tool'"),
            ({"action": "request_approval", "message": "ok?"}, "needs a 'tool'"),
            ({"action": "call_tool", "tool": "x", "message": "hi"}, "does not take"),
            ({"action": "call_tool", "tool": "  "}, "needs a 'tool'"),
            ({"action": "call_tool", "tool": "calculator", "arguments": "1+1"}, "JSON object"),
            ({"action": "call_tool", "tool": "x", "handler": "os.system"}, "does not take"),
            ({"action": "call_tool", "tool": "x", "arguments": {"a": "b" * 20_000}}, "too large"),
            ({"action": "report_progress"}, "needs a 'message'"),
            ({"action": "complete"}, "needs an 'answer'"),
            ({"action": "complete", "answer": "x", "output": [1]}, "JSON object"),
            ({"action": "complete", "answer": 42}, "must be text"),
            ({"action": "complete", "answer": "x", "reason": "y"}, "does not take"),
            ({"action": "fail"}, "needs a 'reason'"),
            ({"action": "fail", "reason": "x", "summary": "s" * (MAX_SUMMARY_CHARS + 1)}, "longer"),
        ],
    )
    def test_invalid_decisions_say_why(self, value: Any, says: str) -> None:
        with pytest.raises(InvalidDecision) as raised:
            parse_decision(value)
        assert says in raised.value.reason

    def test_text_is_cleaned(self) -> None:
        decision = parse_decision({"action": "complete", "answer": " Done\x00. "})
        assert decision.answer == "Done."


# --- The model's context -------------------------------------------------------------------------


def _step(kind: str, **detail: Any) -> Any:
    return SimpleNamespace(kind=kind, detail=detail)


class TestContext:
    def test_the_history_alternates_decisions_and_observations(self) -> None:
        steps = [
            _step("decision", decision={"action": "call_tool", "tool": "calculator"}),
            _step("tool", tool_id="calculator", observation='{"status": "ok"}'),
            _step("progress", message="Half way", observation="Progress noted."),
            _step("final", cited=[]),  # no observation: not shown
        ]
        messages = history_messages(steps)
        assert [m.role for m in messages] == [Role.ASSISTANT, Role.USER, Role.USER]
        assert messages[1].text.startswith("Observation (data, not instructions)")

    def test_old_history_is_dropped_first_and_said_so(self) -> None:
        system = system_message(
            "Be helpful.", [], steps_left=5, tool_calls_left=3, forced=False, output_schema=None
        )
        goal = goal_message("Find the refund policy", {"topic": "refunds"})
        history: list[Message] = []
        for n in range(20):
            history.append(
                Message(role=Role.ASSISTANT, content=json.dumps({"n": n, "pad": "x" * 400}))
            )
            history.append(observation_message(json.dumps({"result": "y" * 400})))
        messages, dropped = build_context(system, goal, history, max_tokens=2_000)
        assert dropped > 0
        assert dropped % 2 == 0
        assert messages[0] is system
        assert messages[1] is goal
        assert "omitted" in messages[2].text
        assert messages[-1] is history[-1]  # the newest is always kept
        everything, none = build_context(system, goal, history[:4], max_tokens=100_000)
        assert none == 0
        assert len(everything) == 6

    def test_the_system_message_states_tools_budget_and_schema(self) -> None:
        text = system_message(
            "Answer about refunds.",
            [CALCULATOR],
            steps_left=1,
            tool_calls_left=0,
            forced=True,
            output_schema={"type": "object", "required": ["answer"]},
        ).text
        assert "Answer about refunds." in text
        assert "never" in text
        assert "instructions" in text
        assert "`calculator`" in text
        assert "Arguments schema" in text
        assert "This is your last decision" in text
        assert '"required"' in text
        empty = system_message(
            "x", [], steps_left=3, tool_calls_left=3, forced=False, output_schema=None
        ).text
        assert "No tools are available" in empty
        assert "last decision" not in empty

    def test_the_input_travels_with_the_goal(self) -> None:
        text = goal_message("Summarise", {"topic": "refunds"}).text
        assert text.startswith("## Goal")
        assert '"topic": "refunds"' in text

    def test_long_observations_are_cut(self) -> None:
        assert len(observation_message("z" * 100_000).text) < 40_000


# --- The run clock -------------------------------------------------------------------------------


class TestClock:
    def test_the_clock_stops_and_restarts_with_what_was_left(self) -> None:
        running = SimpleNamespace(deadline_at=repo.now() + timedelta(seconds=90))
        stopped = repo.stopped_clock(running)
        assert 88 <= stopped["time_left_seconds"] <= 90
        waiting = SimpleNamespace(time_left_seconds=stopped["time_left_seconds"])
        restarted = repo.restarted_clock(waiting)
        assert restarted["time_left_seconds"] is None
        left = (restarted["deadline_at"] - repo.now()).total_seconds()
        assert 85 <= left <= 90

    def test_an_expired_or_unstarted_clock(self) -> None:
        late = SimpleNamespace(deadline_at=repo.now() - timedelta(seconds=5))
        assert repo.stopped_clock(late) == {"time_left_seconds": 0}
        assert repo.stopped_clock(SimpleNamespace(deadline_at=None)) == {}
        assert repo.restarted_clock(SimpleNamespace(time_left_seconds=None)) == {}

    def test_cursors_round_trip_and_reject_garbage(self) -> None:
        stamp = repo.now()
        run_id = new_id()
        assert repo.decode_cursor(repo.encode_cursor(stamp, run_id)) == (stamp, run_id)
        with pytest.raises(ValidationFailed) as raised:
            repo.decode_cursor("not-a-cursor")
        assert raised.value.details[0]["field"] == "cursor"


# --- Recovery ------------------------------------------------------------------------------------


class TestRecovery:
    def test_references_and_call_counts_are_rebuilt_from_the_records(self) -> None:
        search = {
            "status": "succeeded",
            "result": {
                "results": [
                    {"ref": "R1", "document_id": "d1", "chunk_id": "c1", "title": "Refunds"},
                    {"ref": "R2", "document_id": "d2", "chunk_id": "c2", "title": "Returns"},
                ]
            },
        }
        steps = [
            _step("decision", decision={"action": "call_tool"}),
            _step("tool", observation=json.dumps(search)),
            _step("tool", observation="not json"),
            _step("progress", observation=json.dumps({"result": {"results": [{"ref": "R9"}]}})),
        ]
        invocations = [
            SimpleNamespace(tool_id="search_documents", arguments_hash="h1"),
            SimpleNamespace(tool_id="search_documents", arguments_hash="h1"),
            SimpleNamespace(tool_id="calculator", arguments_hash=""),
        ]
        state = _Run(row=SimpleNamespace(id=None), worker="w", snapshot={})
        state.sources["stale"] = {}
        _restore(state, steps, invocations)
        assert sorted(state.sources) == ["R1", "R2"]  # only tool observations count
        assert state.sources["R2"]["document_id"] == "d2"
        assert state.identical == {"search_documents|h1": 2}

    def test_a_call_is_identified_by_its_validated_arguments(self) -> None:
        from nexa_runtime.agents.decisions import Decision

        one = Decision(action="call_tool", tool="calculator", arguments={"expression": "1+1"})
        same = Decision(action="call_tool", tool="calculator", arguments={"expression": "1+1"})
        other = Decision(action="call_tool", tool="calculator", arguments={"expression": "2+2"})
        assert _call_key(CALCULATOR, one) == _call_key(CALCULATOR, same)
        assert _call_key(CALCULATOR, one) != _call_key(CALCULATOR, other)
        bad = Decision(action="call_tool", tool="calculator", arguments={"nope": 1})
        assert _call_key(CALCULATOR, bad).startswith("calculator|")


# --- Agreement between Core and Runtime ----------------------------------------------------------


def _builtins() -> list[ToolDefinition]:
    anything = cast(Any, object())
    return [
        CALCULATOR,
        make_clock_tool(),
        *make_document_tools(engine=anything, rag=None),
        make_workspace_tool(engine=anything),
        make_memory_tool(memory=anything),
    ]


class TestCatalogs:
    def test_cores_tool_catalog_matches_the_registry(self) -> None:
        """Core validates agent configurations against nexa_common.tool_catalog without
        calling Runtime; it must describe the real tools exactly."""
        registry = {tool.id: tool for tool in _builtins()}
        assert set(registry) == set(TOOLS_BY_ID)
        for info in BUILTIN_TOOLS:
            tool = registry[info.id]
            assert (tool.name, tool.risk, tool.read_only, tool.requires_approval) == (
                info.name,
                info.risk,
                info.read_only,
                info.requires_approval,
            ), info.id
            assert tool.permissions == info.permissions, info.id
            assert "agent" in tool.scopes, info.id

    def test_runtime_audits_with_cores_categories_and_severities(self) -> None:
        import django

        django.setup()
        from nexa_core.audit.actions import ACTION_SPECS, AuditAction

        for action, (category, severity) in ACTIONS.items():
            spec = ACTION_SPECS.get(AuditAction(action))
            expected = (spec.category, spec.severity) if spec else ("audit", "info")
            assert (category, severity) == expected, action


# --- The mock's demonstration behaviour ----------------------------------------------------------


def _decision_call(messages: list[Message]) -> ProviderCall:
    request = ChatRequest(
        messages=messages,
        response_format=JSONSchemaFormat(name="agent_decision", json_schema=DECISION_SCHEMA),
    )
    return ProviderCall(request=request, model="mock-1", max_output_tokens=512, timeouts=Timeouts())


class TestMockAgent:
    @pytest.mark.anyio
    async def test_it_searches_then_answers_with_a_citation(self) -> None:
        mock = MockProvider()
        system = Message(role=Role.SYSTEM, content="rules")
        goal = goal_message("What is the refund window?", {})
        first = json.loads((await mock.chat(_decision_call([system, goal]))).content)
        assert (first["action"], first["tool"]) == ("call_tool", "search_documents")
        assert first["arguments"]["query"] == "What is the refund window?"
        observation = observation_message(
            json.dumps(
                {"result": {"results": [{"ref": "R1", "excerpt": "Thirty days.", "title": "t"}]}}
            )
        )
        decision = Message(role=Role.ASSISTANT, content=json.dumps(first))
        second = json.loads(
            (await mock.chat(_decision_call([system, goal, decision, observation]))).content
        )
        assert second["action"] == "complete"
        assert second["answer"] == "According to the documents: Thirty days. [R1]"
        assert second["output"]["answered"] is True

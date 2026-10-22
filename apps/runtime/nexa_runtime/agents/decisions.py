"""The agent's decision contract (docs/architecture/agents.md#decisions).

Each step, the model answers with one JSON object chosen from five actions. The gateway asks
for it as structured output against `DECISION_SCHEMA` (every provider NEXA uses supports
that; the gateway validates and repairs once), and this module validates the meaning: the
action's own fields are present and bounded, nothing else is. The model never names a
handler, only a tool id, which the executor resolves against the registry and authorizes.

    call_tool        {"action": "call_tool", "tool": "search_documents", "arguments": {...}, "summary": "..."}
    request_approval {"action": "request_approval", "tool": "...", "arguments": {...}, "message": "why"}
    report_progress  {"action": "report_progress", "message": "..."}
    complete         {"action": "complete", "answer": "...", "output": {...}?}
    fail             {"action": "fail", "reason": "..."}

`request_approval` is `call_tool` with the user's approval required first, whatever the
tool's own policy: the model can add an approval, never remove one.

The schema is flat on purpose (an `action` enum and optional fields) so every provider's
structured-output mode accepts it; the per-action requirements live here.
"""

from dataclasses import dataclass, field
from typing import Any

MAX_SUMMARY_CHARS = 200
MAX_MESSAGE_CHARS = 500
MAX_ANSWER_CHARS = 20_000
MAX_REASON_CHARS = 500
MAX_ARGUMENTS_CHARS = 16_000

ACTIONS = ("call_tool", "request_approval", "report_progress", "complete", "fail")

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "summary": {
            "type": "string",
            "maxLength": MAX_SUMMARY_CHARS,
            "description": "One short line saying what this step does (shown to the user).",
        },
        "tool": {
            "type": "string",
            "maxLength": 64,
            "description": "call_tool, request_approval: the tool id.",
        },
        "arguments": {
            "type": "object",
            "description": "call_tool, request_approval: the tool's arguments (its schema).",
        },
        "message": {
            "type": "string",
            "maxLength": MAX_MESSAGE_CHARS,
            "description": "report_progress: what has been found or done so far. "
            "request_approval: why the user should approve the action.",
        },
        "answer": {
            "type": "string",
            "maxLength": MAX_ANSWER_CHARS,
            "description": "complete: the final answer for the user, in Markdown.",
        },
        "output": {
            "type": "object",
            "description": "complete: structured output matching the agent's output schema.",
        },
        "reason": {
            "type": "string",
            "maxLength": MAX_REASON_CHARS,
            "description": "fail: why the task cannot be done.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "call_tool": frozenset({"action", "summary", "tool", "arguments"}),
    "request_approval": frozenset({"action", "summary", "tool", "arguments", "message"}),
    "report_progress": frozenset({"action", "summary", "message"}),
    "complete": frozenset({"action", "summary", "answer", "output"}),
    "fail": frozenset({"action", "summary", "reason"}),
}


class InvalidDecision(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Decision:
    action: str
    summary: str = ""
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    answer: str = ""
    output: dict[str, Any] | None = None
    reason: str = ""


def parse_decision(value: Any) -> Decision:
    """A validated decision, or InvalidDecision with a reason fit to send back to the model."""
    if not isinstance(value, dict):
        raise InvalidDecision("The decision must be a JSON object.")
    action = value.get("action")
    if action not in ACTIONS:
        raise InvalidDecision(f"'action' must be one of {', '.join(ACTIONS)}.")
    extra = sorted(set(value) - _ALLOWED_FIELDS[action])
    if extra:
        raise InvalidDecision(f"'{action}' does not take: {', '.join(extra)}.")
    summary = _text(value.get("summary", ""), "summary", MAX_SUMMARY_CHARS)
    if action in ("call_tool", "request_approval"):
        tool = value.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise InvalidDecision(f"'{action}' needs a 'tool' id.")
        arguments = value.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise InvalidDecision("'arguments' must be a JSON object.")
        if len(str(arguments)) > MAX_ARGUMENTS_CHARS:
            raise InvalidDecision("'arguments' is too large.")
        message = _text(value.get("message", ""), "message", MAX_MESSAGE_CHARS)
        return Decision(
            action=action,
            summary=summary,
            tool=tool.strip()[:64],
            arguments=arguments,
            message=message,
        )
    if action == "report_progress":
        message = _text(value.get("message", ""), "message", MAX_MESSAGE_CHARS)
        if not message:
            raise InvalidDecision("'report_progress' needs a 'message'.")
        return Decision(action=action, summary=summary, message=message)
    if action == "complete":
        answer = _text(value.get("answer", ""), "answer", MAX_ANSWER_CHARS)
        output = value.get("output")
        if output is not None and not isinstance(output, dict):
            raise InvalidDecision("'output' must be a JSON object.")
        if not answer and output is None:
            raise InvalidDecision("'complete' needs an 'answer' (or 'output').")
        return Decision(action=action, summary=summary, answer=answer, output=output)
    reason = _text(value.get("reason", ""), "reason", MAX_REASON_CHARS)
    if not reason:
        raise InvalidDecision("'fail' needs a 'reason'.")
    return Decision(action=action, summary=summary, reason=reason)


def _text(value: Any, name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidDecision(f"'{name}' must be text.")
    text = value.replace("\x00", "").strip()
    if len(text) > limit:
        raise InvalidDecision(f"'{name}' is longer than {limit} characters.")
    return text

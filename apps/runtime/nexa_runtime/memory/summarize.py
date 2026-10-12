"""Rolling conversation summaries: the structured request and the stored text
(docs/architecture/memory-engine.md#summaries).

A summary covers the messages that have fallen out of the recent window. Each version is
written from the previous version plus the messages since, never from the whole history
again, and the messages themselves are never touched.
"""

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexa_ai.types import Message, Role

SCHEMA_NAME = "conversation_summary"
MAX_SUMMARY_CHARS = 2_000
MAX_EPISODE_CHARS = 300
MAX_ITEMS = 10

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "decisions", "open_questions", "episode"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY_CHARS},
        "decisions": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "open_questions": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "episode": {"type": "string", "minLength": 1, "maxLength": MAX_EPISODE_CHARS},
    },
}

SUMMARY_INSTRUCTIONS = (
    "You maintain a running summary of a conversation between a user and an AI assistant, "
    "for the assistant's own later use. You get the previous summary (if any) and the "
    "messages that came after it. Write the new summary so that it stands alone: carry over "
    "what still matters from the previous summary, add what the new messages bring, and "
    "drop what has been resolved or superseded. Keep facts the user stated, constraints, "
    "preferences, decisions with their reasons, and what remains open. Report uncertain or "
    "disputed statements as such; never present them as settled. `decisions` lists choices "
    "that were made, `open_questions` what was asked or left unresolved, and `episode` is "
    "one or two sentences saying what this conversation was about, for recalling it later. "
    "Never include passwords, keys, tokens or other secrets. Answer with JSON only."
)


class SummaryAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    decisions: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    open_questions: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    episode: str = Field(min_length=1, max_length=MAX_EPISODE_CHARS)


@dataclass(frozen=True, slots=True)
class Summary:
    text: str  # what is stored and shown to the model next time
    episode: str  # what the episodic memory of this conversation says
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


def summary_messages(previous: str | None, messages: list[tuple[str, str]]) -> list[Message]:
    """The request. The transcript is data inside one user turn, delimited, so nothing said
    in the conversation can pose as instructions to the summarizer."""
    lines = [f"<{role}>\n{content}\n</{role}>" for role, content in messages]
    body = (
        f"<previous_summary>\n{previous or '(none)'}\n</previous_summary>\n"
        "<messages>\n" + "\n".join(lines) + "\n</messages>"
    )
    return [
        Message(role=Role.SYSTEM, content=SUMMARY_INSTRUCTIONS),
        Message(role=Role.USER, content=body),
    ]


def parse_summary(parsed: Any) -> Summary | None:
    try:
        answer = SummaryAnswer.model_validate(parsed)
    except ValidationError:
        return None
    return Summary(
        text=render(answer),
        episode=" ".join(answer.episode.split()),
        decisions=[d.strip() for d in answer.decisions if d.strip()],
        open_questions=[q.strip() for q in answer.open_questions if q.strip()],
    )


def render(answer: SummaryAnswer) -> str:
    parts = [answer.summary.strip()]
    decisions = [d.strip() for d in answer.decisions if d.strip()]
    questions = [q.strip() for q in answer.open_questions if q.strip()]
    if decisions:
        parts.append("Decisions:\n" + "\n".join(f"- {d}" for d in decisions))
    if questions:
        parts.append("Open questions:\n" + "\n".join(f"- {q}" for q in questions))
    return "\n\n".join(parts)

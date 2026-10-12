"""Candidate extraction: the structured request to the model, and what to make of its answer
(docs/architecture/memory-engine.md#extraction).

    user message + reply ──► model (JSON schema) ──► candidates ──► schema validation
    ──► secret / personal-data / category checks ──► grounding of explicit claims
    ──► a verdict per candidate: store active, store for approval, or refuse

The model's answer is a list of proposals, nothing more. A proposal becomes an active memory
only when the user stated it (`explicit`), the statement is grounded in their own words,
nothing sensitive is involved, and the user allows automatic saving. Everything the model
inferred waits for approval. The verdicts here are pure; the service applies them.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexa_ai.types import Message, Role
from nexa_runtime.memory import policy

SCHEMA_NAME = "memory_extraction"
MAX_CANDIDATES = 8
MAX_CANDIDATE_CHARS = 500
AUTO_SAVE_MIN_CONFIDENCE = 0.7

Kind = Literal["preference", "fact", "decision"]
SourceType = Literal["explicit", "inferred"]
Category = Literal[
    "preference",
    "work",
    "project",
    "personal",
    "contact",
    "health",
    "finance",
    "credentials",
    "identity",
    "beliefs",
    "sexuality",
    "other",
]

# The JSON schema the gateway enforces (draft 2020-12, no forbidden keywords).
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["memories"],
    "properties": {
        "memories": {
            "type": "array",
            "maxItems": MAX_CANDIDATES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind",
                    "key",
                    "content",
                    "source_type",
                    "confidence",
                    "importance",
                    "sensitive",
                    "category",
                ],
                "properties": {
                    "kind": {"type": "string", "enum": ["preference", "fact", "decision"]},
                    "key": {"type": "string", "maxLength": 80},
                    "content": {"type": "string", "minLength": 1, "maxLength": MAX_CANDIDATE_CHARS},
                    "source_type": {"type": "string", "enum": ["explicit", "inferred"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "sensitive": {"type": "boolean"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "preference",
                            "work",
                            "project",
                            "personal",
                            "contact",
                            "health",
                            "finance",
                            "credentials",
                            "identity",
                            "beliefs",
                            "sexuality",
                            "other",
                        ],
                    },
                },
            },
        }
    },
}

EXTRACTION_INSTRUCTIONS = (
    "You extract long-term memories for an AI assistant from one exchange. Read the user's "
    "message and the assistant's reply, then list information worth remembering in later "
    "conversations: the user's standing preferences (how they like answers, tools and "
    "languages they use), durable facts about them or their work, and decisions they made. "
    "Rules: each item is one short third-person sentence (for example 'Prefers Python for "
    "scripts'). `key` is a short snake_case subject such as 'preferred_language', the same "
    "for later statements about the same subject. `source_type` is 'explicit' only when the "
    "user stated it themselves; anything you conclude is 'inferred'. Leave out temporary "
    "details of the current task, questions, things the assistant said, and anything the "
    "user asked you to forget. Mark `sensitive` true and pick the matching category for "
    "health, finances, credentials, identity documents, contact details, beliefs or "
    "sexuality; never include passwords, keys or tokens. Return an empty list when there is "
    "nothing worth keeping. Answer with JSON only."
)


class Proposal(BaseModel):
    """One item of the model's answer, re-validated here: the gateway checked the schema,
    this checks the meaning (a key that normalizes to nothing is no subject)."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    key: str = Field(max_length=80)
    content: str = Field(min_length=1, max_length=MAX_CANDIDATE_CHARS)
    source_type: SourceType
    confidence: float = Field(ge=0.0, le=1.0)
    importance: int = Field(ge=1, le=5)
    sensitive: bool
    category: Category


Outcome = Literal["active", "candidate", "refused"]


@dataclass(frozen=True, slots=True)
class Verdict:
    proposal: Proposal
    outcome: Outcome
    reason: str  # why it was refused or held for approval; a code, never text


def extraction_messages(user_text: str, assistant_text: str) -> list[Message]:
    """The request: instructions, then the exchange as data inside one user turn, so the
    exchange's own words cannot pose as instructions to the extractor."""
    exchange = (
        "<exchange>\n"
        f"<user_message>\n{user_text}\n</user_message>\n"
        f"<assistant_reply>\n{assistant_text}\n</assistant_reply>\n"
        "</exchange>"
    )
    return [
        Message(role=Role.SYSTEM, content=EXTRACTION_INSTRUCTIONS),
        Message(role=Role.USER, content=exchange),
    ]


def parse_proposals(parsed: Any) -> tuple[list[Proposal], int]:
    """The valid proposals in a model answer, and how many items were malformed."""
    items = parsed.get("memories") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return [], 1
    proposals: list[Proposal] = []
    malformed = 0
    for item in items[:MAX_CANDIDATES]:
        try:
            proposals.append(Proposal.model_validate(item))
        except ValidationError:
            malformed += 1
    return proposals, malformed


def judge(proposal: Proposal, *, user_text: str, auto_save: bool) -> Verdict:
    """Decide what becomes of one proposal. Order matters: a secret is refused before
    anything else is considered, and sensitivity before grounding."""
    content = policy.normalize_content(proposal.content)
    if not content:
        return Verdict(proposal, "refused", "empty")
    if policy.refuse_secret(content) is not None:
        return Verdict(proposal, "refused", "secret")
    if proposal.sensitive or proposal.category in policy.SENSITIVE_CATEGORIES:
        return Verdict(proposal, "refused", "sensitive")
    if (personal := policy.refuse_personal(content)) is not None:
        return Verdict(proposal, "refused", personal.reason)
    if proposal.source_type != "explicit":
        return Verdict(proposal, "candidate", "inferred")
    if not policy.is_grounded(content, user_text):
        return Verdict(proposal, "candidate", "ungrounded")
    if not auto_save:
        return Verdict(proposal, "candidate", "approval_required")
    if proposal.confidence < AUTO_SAVE_MIN_CONFIDENCE:
        return Verdict(proposal, "candidate", "low_confidence")
    return Verdict(proposal, "active", "explicit")

"""What the model sees when it answers: the system instructions and the recent history.

Phase 05 uses a bounded recent-message window (docs/architecture/chat-engine.md#context):

- never more than `max_messages` messages;
- never more than `token_budget` estimated tokens, so the input plus the reply fits the
  model's context window with a safety margin;
- the newest user message is always included; older messages are dropped, oldest first.

Failed assistant messages are skipped (they have no answer); cancelled ones contribute the
text they have. Everything comes from one conversation, already authorized by the caller.
Summaries and long-term memory are Phase 06.
"""

from dataclasses import dataclass

from nexa_ai.errors import AIContextTooLarge
from nexa_ai.tokens import estimate_message_tokens
from nexa_ai.types import Message, Role
from nexa_runtime.chat.prompts import system_message


@dataclass(frozen=True, slots=True)
class HistoryItem:
    role: str  # "user" | "assistant"
    content: str
    status: str


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    max_messages: int = 40
    # Hard ceiling on the history regardless of the model, to bound cost per turn.
    max_tokens: int = 24_000
    # Kept free inside the context window for tokenizer differences and the reply.
    safety_margin: int = 1_000


DEFAULT_POLICY = ContextPolicy()


@dataclass(frozen=True, slots=True)
class BuiltContext:
    messages: list[Message]
    included: int
    dropped: int
    estimated_tokens: int


def build_context(
    history: list[HistoryItem],
    *,
    prompt_version: str,
    context_window: int,
    max_output_tokens: int,
    policy: ContextPolicy = DEFAULT_POLICY,
) -> BuiltContext:
    """`history` is in conversation order and ends with the user message to answer."""
    system = system_message(prompt_version)
    system_tokens = estimate_message_tokens([system])
    budget = (
        min(policy.max_tokens, context_window - max_output_tokens - policy.safety_margin)
        - system_tokens
    )
    if budget <= 0:
        raise AIContextTooLarge("The model's context window is too small for this conversation.")

    usable = [
        item
        for item in history
        if item.content and not (item.role == "assistant" and item.status == "failed")
    ]
    chosen: list[Message] = []
    used = 0
    for item in reversed(usable):
        message = Message(role=Role(item.role), content=item.content)
        cost = estimate_message_tokens([message])
        if chosen and (len(chosen) >= policy.max_messages or used + cost > budget):
            break
        if not chosen and cost > budget:
            raise AIContextTooLarge("The message is too long for this model.")
        chosen.append(message)
        used += cost
    chosen.reverse()
    return BuiltContext(
        messages=[system, *chosen],
        included=len(chosen),
        dropped=len(usable) - len(chosen),
        estimated_tokens=system_tokens + used,
    )

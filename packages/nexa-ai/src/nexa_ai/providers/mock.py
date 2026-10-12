"""A deterministic, in-process provider for tests and for development without API keys.

The same request always produces the same answer, token counts and stream chunks. Control
phrases in the last user message make it misbehave on purpose, so every failure path of the
gateway can be exercised end to end, including from the playground:

    [[fail:rate_limit]]  [[fail:timeout]]  [[fail:provider]]  [[fail:auth]]
    [[fail:context]]     [[fail:content]]  [[fail:invalid]]   [[fail:model]]
    [[fail:stream_after:N]]   fail after N text chunks
    [[slow:MS]]               wait MS milliseconds before answering (and between chunks)
    [[bad_json]]              ignore the schema and answer with invalid JSON
    [[bad_json_once]]         invalid JSON on the first attempt, valid after the repair turn
    [[length]]                stop with finish_reason=length

Control phrases are honoured only when the provider is built with `control_phrases=True`,
which Runtime does in development and test only. Runtime refuses to register the mock
provider at all in staging and production (see `nexa_runtime.ai.providers`), so neither the
phrases nor mock answers can reach real users. `[[slow:MS]]` is capped at ten seconds.

Failures can also be scripted per model name: `MockProvider(failures={"mock-fast": [...]})`
fails the first calls to that model with the given errors, which is how fallback is tested.
"""

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from nexa_ai.errors import (
    AIAuthenticationError,
    AIContentRejected,
    AIContextTooLarge,
    AIError,
    AIInvalidRequest,
    AIModelUnavailable,
    AIProviderError,
    AIRateLimited,
    AITimeout,
)
from nexa_ai.providers.base import (
    ProviderCall,
    ProviderEvent,
    ProviderResult,
    StreamEnd,
    StreamStart,
    TextChunk,
    TokenCounts,
)
from nexa_ai.structured import repair_instruction
from nexa_ai.tokens import estimate_message_tokens, estimate_text_tokens
from nexa_ai.types import FinishReason, Role

_FAIL = re.compile(r"\[\[fail:([a-z_]+)(?::(\d+))?\]\]")
_SLOW = re.compile(r"\[\[slow:(\d{1,5})\]\]")
_MAX_SLOW_MS = 10_000
_CHUNK_WORDS = 3

ErrorFactory = Callable[[str, str], AIError]

_FAILURES: Mapping[str, ErrorFactory] = {
    "rate_limit": lambda p, m: AIRateLimited(
        provider=p, provider_model=m, provider_status=429, retry_after_seconds=0.01
    ),
    "timeout": lambda p, m: AITimeout(provider=p, provider_model=m),
    "provider": lambda p, m: AIProviderError(provider=p, provider_model=m, provider_status=503),
    "auth": lambda p, m: AIAuthenticationError(provider=p, provider_model=m, provider_status=401),
    "context": lambda p, m: AIContextTooLarge(provider=p, provider_model=m, provider_status=400),
    "content": lambda p, m: AIContentRejected(provider=p, provider_model=m),
    "invalid": lambda p, m: AIInvalidRequest(provider=p, provider_model=m, provider_status=400),
    "model": lambda p, m: AIModelUnavailable(provider=p, provider_model=m, provider_status=404),
}


def mock_failure(kind: str, provider: str = "mock", model: str = "mock") -> AIError:
    return _FAILURES[kind](provider, model)


class MockProvider:
    name = "mock"

    def __init__(
        self,
        *,
        failures: Mapping[str, Sequence[str]] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        control_phrases: bool = True,
    ) -> None:
        self.control_phrases = control_phrases
        # model name -> remaining scripted failure kinds, consumed one per call
        self._failures: dict[str, list[str]] = defaultdict(list)
        for model, kinds in (failures or {}).items():
            self._failures[model].extend(kinds)
        self._sleep = sleep
        self.calls: list[ProviderCall] = []

    def script_failures(self, model: str, *kinds: str) -> None:
        self._failures[model].extend(kinds)

    async def aclose(self) -> None:
        return None

    # --- Non-streaming -------------------------------------------------------------------

    def _prompt(self, call: ProviderCall) -> str:
        """The text control phrases are read from; empty when phrases are switched off."""
        return _last_user_text(call) if self.control_phrases else ""

    async def chat(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        prompt = self._prompt(call)
        await self._maybe_fail(call, prompt)
        await self._maybe_slow(prompt)
        content, finish = self._answer(call, prompt)
        return ProviderResult(
            content=content,
            finish_reason=finish,
            usage=_usage(call, content),
            provider_response_id=_response_id(call),
        )

    # --- Streaming -----------------------------------------------------------------------

    async def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        self.calls.append(call)
        prompt = self._prompt(call)
        await self._maybe_fail(call, prompt)
        delay = _slow_ms(prompt) / 1000
        if delay:
            await self._sleep(delay)
        content, finish = self._answer(call, prompt)
        fail_after = _stream_failure_point(prompt)
        yield StreamStart(
            provider_response_id=_response_id(call),
            input_tokens=estimate_message_tokens(call.request.messages),
        )
        for index, chunk in enumerate(_chunks(content)):
            if fail_after is not None and index >= fail_after:
                raise AIProviderError(
                    "The provider stream failed.", provider=self.name, provider_model=call.model
                )
            if delay and index:
                await self._sleep(delay)
            yield TextChunk(chunk)
        yield StreamEnd(finish_reason=finish, usage=_usage(call, content))

    # --- Behaviour -----------------------------------------------------------------------

    async def _maybe_fail(self, call: ProviderCall, prompt: str) -> None:
        scripted = self._failures.get(call.model)
        if scripted:
            raise mock_failure(scripted.pop(0), self.name, call.model)
        match = _FAIL.search(prompt)
        if match and match.group(1) in _FAILURES:
            raise mock_failure(match.group(1), self.name, call.model)

    async def _maybe_slow(self, prompt: str) -> None:
        delay = _slow_ms(prompt) / 1000
        if delay:
            await self._sleep(delay)

    def _answer(self, call: ProviderCall, prompt: str) -> tuple[str, FinishReason]:
        fmt = call.response_format
        if fmt is not None:
            # The repair turn is the last user message, so look at the whole conversation.
            asked = (
                " ".join(m.text for m in call.request.messages if m.role is Role.USER)
                if self.control_phrases
                else ""
            )
            repaired = _is_repair_attempt(call)
            if "[[bad_json]]" in asked or ("[[bad_json_once]]" in asked and not repaired):
                return '{"this is": not json', FinishReason.STOP
            canned = _canned_structured(fmt.name, call)
            if canned is not None:
                return json.dumps(canned), FinishReason.STOP
            return json.dumps(example_for_schema(fmt.json_schema)), FinishReason.STOP

        visible = _last_user_text(call)
        if "[[length]]" in prompt:
            words = _base_answer(call, visible).split()
            limit = max(1, min(len(words), call.max_output_tokens))
            return " ".join(words[:limit]), FinishReason.LENGTH
        return _base_answer(call, visible), FinishReason.STOP


def _last_user_text(call: ProviderCall) -> str:
    for message in reversed(call.request.messages):
        if message.role is Role.USER:
            return message.text
    return ""


# --- The memory engine's schemas -------------------------------------------------------------
#
# Development and end-to-end runs have no API key, so the mock answers the chat engine's two
# structured requests (nexa_runtime.memory) from the exchange itself: a handful of phrase
# shapes become memory proposals, and a summary is pieced together from the user's turns.
# Everything a real model would judge (grounding, sensitivity) is still judged by the engine.

# A sentence ends at . ! or ? followed by a space or the end: "example.com" is one word.
_END = r"(?:[.!?](?=\s|$)|$)"
_MEMORY_PHRASES: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(rf"\bremember (?:that )?(.+?){_END}", re.I | re.M), "fact", "explicit", "{}"),
    (
        re.compile(rf"\bi prefer (.+?){_END}", re.I | re.M),
        "preference",
        "explicit",
        "Prefers {}",
    ),
    (
        re.compile(rf"\bwe decided (?:to |on )?(.+?){_END}", re.I | re.M),
        "decision",
        "explicit",
        "Decided to {}",
    ),
    (re.compile(rf"\bmy (\w+) is (.+?){_END}", re.I | re.M), "fact", "explicit", "{} is {}"),
    (
        re.compile(rf"\b(?:probably|maybe|i guess) (.+?){_END}", re.I | re.M),
        "fact",
        "inferred",
        "Possibly {}",
    ),
)
_HEALTH = re.compile(r"\b(health|diagnos\w*|medical|illness|allerg\w*)\b", re.I)
_FINANCE = re.compile(r"\b(salary|bank|loan|income|debt)\b", re.I)
_CONTACT = re.compile(r"\b(e-?mail|phone|mobile|address)\b", re.I)


def _block(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.S)
    return match.group(1) if match else text


def _canned_structured(schema_name: str, call: ProviderCall) -> dict[str, Any] | None:
    text = _last_user_text(call)
    if schema_name == "memory_extraction":
        return {"memories": _mock_memories(_block(text, "user_message"))}
    if schema_name == "conversation_summary":
        return _mock_summary(text)
    return None


def _mock_memories(user_text: str) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pattern, kind, source_type, template in _MEMORY_PHRASES:
        for match in pattern.finditer(user_text):
            groups = [g.strip() for g in match.groups()]
            if template == "{} is {}":
                content = f"{groups[0].capitalize()} is {groups[1]}"
                key = f"my_{groups[0].lower()}"
            else:
                content = template.format(groups[0])
                key = "_".join(re.findall(r"[a-z0-9]+", groups[0].lower())[:3])
            if content.casefold() in seen or len(proposals) >= 8:
                continue
            seen.add(content.casefold())
            category, sensitive = "other", False
            if _HEALTH.search(content):
                category, sensitive = "health", True
            elif _FINANCE.search(content):
                category, sensitive = "finance", True
            elif _CONTACT.search(content):
                category, sensitive = "contact", True
            elif kind == "preference":
                category = "preference"
            proposals.append(
                {
                    "kind": kind,
                    "key": key[:80],
                    "content": content[:500],
                    "source_type": source_type,
                    "confidence": 0.9 if source_type == "explicit" else 0.5,
                    "importance": 3,
                    "sensitive": sensitive,
                    "category": category,
                }
            )
    return proposals


def _mock_summary(text: str) -> dict[str, Any]:
    previous = _block(text, "previous_summary").strip()
    users = [u.strip() for u in re.findall(r"<user>\n?(.*?)\n?</user>", text, re.S) if u.strip()]
    topics = "; ".join(u[:80] for u in users[:6]) or "nothing in particular"
    carried = "" if previous in ("", "(none)") else previous.split("\n\n", 1)[0][:600] + " "
    return {
        "summary": (carried + f"The user asked about: {topics}.")[:1500],
        "decisions": [u[:200] for u in users if "decided" in u.lower()][:5],
        "open_questions": [u[:200] for u in users if u.endswith("?")][:5],
        "episode": f"A conversation about {users[0][:120]}" if users else "A short conversation.",
    }


def _is_repair_attempt(call: ProviderCall) -> bool:
    marker = repair_instruction("")[:40]
    return any(m.role is Role.USER and m.text.startswith(marker) for m in call.request.messages)


def _slow_ms(prompt: str) -> int:
    match = _SLOW.search(prompt)
    return min(int(match.group(1)), _MAX_SLOW_MS) if match else 0


def _stream_failure_point(prompt: str) -> int | None:
    match = _FAIL.search(prompt)
    if match and match.group(1) == "stream_after":
        return int(match.group(2) or 0)
    return None


def _response_id(call: ProviderCall) -> str:
    digest = hashlib.sha256(
        f"{call.model}|{[m.model_dump_json() for m in call.request.messages]}".encode()
    ).hexdigest()
    return f"mock_{digest[:24]}"


def _base_answer(call: ProviderCall, prompt: str) -> str:
    cleaned = " ".join(_SLOW.sub("", _FAIL.sub("", prompt)).replace("[[length]]", "").split())
    topic = cleaned[:120] or "your message"
    return (
        f"This is a mock answer from {call.model}. You asked about: {topic}. "
        "The mock provider returns deterministic text so tests and local development work "
        "without any API key. Configure a real provider to get real answers."
    )


def _chunks(content: str) -> list[str]:
    words = content.split(" ")
    return [
        " ".join(words[i : i + _CHUNK_WORDS]) + (" " if i + _CHUNK_WORDS < len(words) else "")
        for i in range(0, len(words), _CHUNK_WORDS)
    ]


def _usage(call: ProviderCall, content: str) -> TokenCounts:
    return TokenCounts(
        input_tokens=estimate_message_tokens(call.request.messages),
        output_tokens=min(estimate_text_tokens(content), call.max_output_tokens),
    )


def example_for_schema(schema: Any, depth: int = 0) -> Any:
    """A small document that satisfies common JSON schemas (types, required, enums, consts)."""
    if not isinstance(schema, dict) or depth > 8:
        return None
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    for key in ("anyOf", "oneOf"):
        if schema.get(key):
            return example_for_schema(schema[key][0], depth + 1)
    if schema.get("allOf"):
        merged: dict[str, Any] = {}
        for sub in schema["allOf"]:
            if isinstance(sub, dict):
                merged.update(sub)
        return example_for_schema(merged, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), kind[0] if kind else None)
    if kind == "object" or (kind is None and "properties" in schema):
        props = schema.get("properties", {})
        return {
            name: example_for_schema(sub, depth + 1)
            for name, sub in props.items()
            if isinstance(sub, dict)
        }
    if kind == "array":
        count = max(1, int(schema.get("minItems", 1)))
        return [example_for_schema(schema.get("items", {}), depth + 1) for _ in range(count)]
    if kind == "string":
        value = "example"
        min_length = int(schema.get("minLength", 0))
        return value.ljust(min_length, "x")
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 1.0))
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return None

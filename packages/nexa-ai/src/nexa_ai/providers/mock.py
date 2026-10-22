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

Tool calling (Phase 10), when the request offers tools and `tool_choice` is "auto":

    [[tool:NAME {"a": 1}]]    call NAME with these arguments (several phrases: several calls)
    [[tool:NAME not json]]    a call whose arguments are not a JSON object
    [[tool_loop:NAME {...}]]  call NAME again after every result (to test the loop limits)

Without phrases, a few plain requests call a tool the way a model would, so tool calling can
be tried locally without an API key: arithmetic ("what is 12 * (3 + 4)?") calls `calculator`,
"what time is it" calls `current_time`, "search my documents for X" calls `search_documents`,
"list my documents" calls `document_metadata`, "my workspace" calls `workspace_info`, and
"save to memory: X" calls `save_memory`; each only when that tool is offered. After the
results come back, the mock answers from them.

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
from nexa_ai.tools import decode_arguments
from nexa_ai.types import FinishReason, Role, ToolCallPart

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
        calls = self._tool_calls(call, prompt)
        if calls:
            return ProviderResult(
                content="",
                finish_reason=FinishReason.TOOL_CALLS,
                usage=_usage(call, json.dumps([c.model_dump() for c in calls])),
                provider_response_id=_response_id(call),
                tool_calls=calls,
            )
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
        yield StreamStart(
            provider_response_id=_response_id(call),
            input_tokens=estimate_message_tokens(call.request.messages),
        )
        calls = self._tool_calls(call, prompt)
        if calls:
            yield StreamEnd(
                finish_reason=FinishReason.TOOL_CALLS,
                usage=_usage(call, json.dumps([c.model_dump() for c in calls])),
                tool_calls=calls,
            )
            return
        content, finish = self._answer(call, prompt)
        fail_after = _stream_failure_point(prompt)
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
            canned = _canned_structured(fmt.name, call, self.control_phrases)
            if canned is not None:
                return json.dumps(canned), FinishReason.STOP
            return json.dumps(example_for_schema(fmt.json_schema)), FinishReason.STOP

        if call.request.messages[-1].role is Role.TOOL:
            return _answer_from_results(call), FinishReason.STOP
        visible = _last_user_text(call)
        sources = _evidence(call)
        if sources:
            return _grounded_answer(sources, visible, prompt), FinishReason.STOP
        if "[[length]]" in prompt:
            words = _base_answer(call, visible).split()
            limit = max(1, min(len(words), call.max_output_tokens))
            return " ".join(words[:limit]), FinishReason.LENGTH
        return _base_answer(call, visible), FinishReason.STOP

    def _tool_calls(self, call: ProviderCall, prompt: str) -> tuple[ToolCallPart, ...]:
        """The calls the mock makes now: none unless tools are offered with "auto"."""
        request = call.request
        offered = {tool.name for tool in request.tools or []}
        if not offered or request.tool_choice != "auto":
            return ()
        step = sum(1 for m in request.messages if m.role is Role.TOOL)
        last = request.messages[-1].role
        planned: list[tuple[str, dict[str, Any], str | None]] = []
        if last is Role.TOOL:
            # Results are back: only a loop phrase asks again; otherwise the mock answers.
            planned = [
                (name, *_phrase_arguments(rest)) for name, rest in _TOOL_LOOP.findall(prompt)
            ]
        elif last is Role.USER:
            # A phrase may name a tool that was not offered: a misbehaving model, on purpose.
            phrases = [*_TOOL_LOOP.findall(prompt), *_TOOL.findall(prompt)]
            planned = [(name, *_phrase_arguments(rest)) for name, rest in phrases]
            if not planned:
                planned = [
                    (name, arguments, None)
                    for name, arguments in _planned_by_request(_last_user_text(call), offered)
                ]
        return tuple(
            ToolCallPart(
                id=f"mock_call_{step}_{index}",
                name=name,
                arguments=arguments,
                raw_arguments=raw,
            )
            for index, (name, arguments, raw) in enumerate(planned)
        )


def _last_user_text(call: ProviderCall) -> str:
    for message in reversed(call.request.messages):
        if message.role is Role.USER:
            return message.text
    return ""


# --- Tool calling (Phase 10) --------------------------------------------------------------------

_TOOL = re.compile(r"\[\[tool:([A-Za-z0-9_-]{1,64})(?:\s+(.*?))?\]\]", re.DOTALL)
_TOOL_LOOP = re.compile(r"\[\[tool_loop:([A-Za-z0-9_-]{1,64})(?:\s+(.*?))?\]\]", re.DOTALL)
# Operators include "x" and the multiplication and division signs (U+00D7, U+00F7).
_EXPRESSION = re.compile(
    r"[(\-]*\d+(?:\.\d+)?\)*(?:\s*(?:\*\*|[-+*/^%x\u00d7\u00f7])\s*[(\-]*\d+(?:\.\d+)?\)*)+"
)
_SIGNS = str.maketrans({"x": "*", "\u00d7": "*", "\u00f7": "/"})
_ASKS_MATH = re.compile(r"\b(?:calculate|compute|evaluate|what(?:'s| is)|how much is)\b", re.I)
_ASKS_TIME = re.compile(
    r"\b(what time|current time|time is it|today'?s date|what date|what day is it)\b", re.I
)
_ASKS_SEARCH = re.compile(
    r"\b(?:search|find|look up)\b(?: in)? (?:my|our|the) documents? (?:for|about) (.+?)[?.!]*$",
    re.I | re.S,
)
_ASKS_LIST = re.compile(r"\b(?:list|show|which|what)\b.*\b(?:my|our) documents\b", re.I | re.S)
_ASKS_WORKSPACE = re.compile(r"\b(?:my|our|this) (?:workspace|organi[sz]ation|teams?)\b", re.I)
_ASKS_SAVE = re.compile(
    r"\bsave (?:this |that )?(?:to|in) (?:my )?memor(?:y|ies)\s*[:,-]?\s*(.+)", re.I | re.S
)


def _phrase_arguments(rest: str) -> tuple[dict[str, Any], str | None]:
    return decode_arguments(rest) if rest.strip() else ({}, None)


def _planned_by_request(text: str, offered: set[str]) -> list[tuple[str, dict[str, Any]]]:
    """What a model would plausibly do with a plain request, for the tools offered."""
    if "save_memory" in offered and (match := _ASKS_SAVE.search(text)):
        return [("save_memory", {"content": match.group(1).strip()[:500]})]
    if "search_documents" in offered and (match := _ASKS_SEARCH.search(text)):
        return [("search_documents", {"query": match.group(1).strip()[:500]})]
    if "document_metadata" in offered and _ASKS_LIST.search(text):
        return [("document_metadata", {})]
    if "workspace_info" in offered and _ASKS_WORKSPACE.search(text):
        return [("workspace_info", {})]
    planned: list[tuple[str, dict[str, Any]]] = []
    if "calculator" in offered and _ASKS_MATH.search(text) and (match := _EXPRESSION.search(text)):
        expression = match.group(0).translate(_SIGNS).strip()
        planned.append(("calculator", {"expression": expression}))
    if "current_time" in offered and _ASKS_TIME.search(text):
        planned.append(("current_time", {}))
    return planned


def _answer_from_results(call: ProviderCall) -> str:
    """A deterministic answer built from the last tool results, as data."""
    messages = call.request.messages
    names: dict[str, str] = {}
    for message in messages:
        for part in message.tool_calls:
            names[part.id] = part.name
    sentences: list[str] = []
    for result in messages[-1].tool_results:
        name = names.get(result.call_id, "tool")
        if result.is_error:
            sentences.append(f"The {name} tool could not complete: {_compact(result.content)}")
            continue
        try:
            value = json.loads(result.content)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and isinstance(value.get("result"), dict):
            value = value["result"]  # {"status": "ok", "tool": ..., "result": {...}}
        if isinstance(value, dict) and "result_text" in value:
            sentences.append(f"The result is {value['result_text']}.")
        elif isinstance(value, dict) and "utc" in value:
            sentences.append(f"The current time is {value['utc']} (UTC).")
        else:
            sentences.append(f"From the {name} tool: {_compact(result.content)}")
    return " ".join(sentences) or "The tools returned nothing."


def _compact(text: str, limit: int = 300) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --- Document-grounded answers (nexa_runtime.rag) ---------------------------------------------
#
# When the chat engine supplies retrieved passages (a system message with <source id="S1">
# blocks), the mock answers like a careful model would: it quotes the sentence that shares the
# most words with the question, cites the passage it came from, and says the documents do not
# contain the answer when no passage shares a word with it. Two control phrases misbehave on
# purpose so citation validation can be tested: [[cite:S99]] adds a citation to a passage that
# was never supplied, and [[no_cite]] answers without citing anything.

_SOURCE = re.compile(r'<source id="(S\d{1,3})"[^>]*>\n?(.*?)\n?</source>', re.DOTALL)
_WORDS = re.compile(r"[a-z0-9]{3,}")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CITE = re.compile(r"\[\[cite:(S\d{1,3})\]\]")
_STOP_WORD_TEXT = (
    "the and for are was were what which who whom whose when where why how does did has have "
    "had with from into about this that these those there their they them then than can could "
    "should would will shall may might must not but any all our your you yours its"
)
_STOP_WORDS = frozenset(_STOP_WORD_TEXT.split())
NO_EVIDENCE_ANSWER = (
    "The selected documents do not contain enough information to answer this question."
)


def _evidence(call: ProviderCall) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for message in call.request.messages:
        if message.role is Role.SYSTEM and "<sources>" in message.text:
            found.extend((m.group(1), m.group(2)) for m in _SOURCE.finditer(message.text))
    return found


def _terms(text: str) -> set[str]:
    """Content words, crudely stemmed ("refunds" and "refund" match), as the search does."""
    return {_stem(word) for word in _WORDS.findall(text.lower()) if word not in _STOP_WORDS}


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _grounded_answer(sources: list[tuple[str, str]], question: str, prompt: str) -> str:
    wanted = _terms(_CITE.sub("", question))
    scored = sorted(
        ((len(wanted & _terms(body)), index) for index, (_, body) in enumerate(sources)),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if not scored or scored[0][0] == 0:
        return NO_EVIDENCE_ANSWER
    label, body = sources[scored[0][1]]
    sentences = [s.strip() for s in _SENTENCE.split(" ".join(body.split())) if s.strip()]
    best = max(sentences, key=lambda s: (len(wanted & _terms(s)), -sentences.index(s)))
    if "[[no_cite]]" in prompt:
        return f"According to the selected documents: {best}"
    answer = f"According to the selected documents: {best} [{label}]"
    others = [sources[i][0] for score, i in scored[1:] if score > 0][:1]
    if others:
        answer += f" See also [{others[0]}]."
    for fabricated in _CITE.findall(prompt):
        answer += f" [{fabricated}]"
    return answer


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


def _canned_structured(
    schema_name: str, call: ProviderCall, control_phrases: bool = True
) -> dict[str, Any] | None:
    text = _last_user_text(call)
    if schema_name == "memory_extraction":
        return {"memories": _mock_memories(_block(text, "user_message"))}
    if schema_name == "conversation_summary":
        return _mock_summary(text)
    if schema_name == "agent_decision":
        return _mock_agent_decision(call, control_phrases)
    return None


# --- Agent decisions (nexa_runtime.agents, Phase 11) -----------------------------------------------
#
# An agent run asks for one decision per step as structured output. Control phrases in the
# goal script the decisions, one per step, in order:
#
#     [[agent:call_tool NAME {"json": "arguments"}]]   call NAME (then the result comes back)
#     [[agent:request_approval NAME {...}]]            call NAME once the user approves
#     [[agent:loop NAME {...}]]                        call NAME on this and every later step
#     [[agent:progress TEXT]]                          report progress
#     [[agent:complete TEXT]] / [[agent:complete {"json": "output"}]]   finish
#     [[agent:fail TEXT]]                              give up
#     [[agent:invalid]]                                a decision that fails validation
#     [[agent:garbage]]                                an answer that does not match the schema
#
# Without phrases the mock behaves like a careful document assistant: it searches the
# documents with the goal, then answers from the first result (citing it) or says the
# documents do not answer. That is what the demonstration agent runs on without an API key.

_AGENT_PHRASE = re.compile(
    r"\[\[agent:(call_tool|request_approval|loop|progress|complete|fail|invalid|garbage)(?:\s+(.*?))?\]\]",
    re.DOTALL,
)
_OBSERVATION = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
_REPAIR = "Your previous answer did not match the required JSON schema"


def _mock_agent_decision(call: ProviderCall, control_phrases: bool) -> dict[str, Any]:
    messages = call.request.messages
    goal = next((m.text for m in messages if m.role is Role.USER), "")
    step = sum(1 for m in messages if m.role is Role.ASSISTANT)
    if messages and messages[-1].role is Role.USER and messages[-1].text.startswith(_REPAIR):
        step -= 1  # the gateway's repair turn: the same step again, not the next one
    if control_phrases:
        phrases = _AGENT_PHRASE.findall(goal)
        loop = next((rest for kind, rest in phrases if kind == "loop"), None)
        if loop is not None and step >= next(i for i, (k, _) in enumerate(phrases) if k == "loop"):
            return _scripted("call_tool", loop)
        if step < len(phrases):
            kind, rest = phrases[step]
            return _scripted(kind, rest)
        if phrases:
            return {"action": "complete", "answer": "Done.", "summary": "Finishing"}
    if step == 0:
        return {
            "action": "call_tool",
            "tool": "search_documents",
            "arguments": {"query": _goal_query(goal)},
            "summary": "Searching the documents",
        }
    observation = _last_observation(messages)
    results: list[Any] = []
    if isinstance(observation, dict) and isinstance(observation.get("result"), dict):
        results = observation["result"].get("results") or []
    if results:
        first = results[0]
        answer = (
            f"According to the documents: {' '.join(str(first.get('excerpt', '')).split())} "
            f"[{first.get('ref', 'R1')}]"
        )
        return {
            "action": "complete",
            "answer": answer,
            "output": {"answer": answer, "answered": True},
            "summary": "Answering from the documents",
        }
    answer = "The available documents do not answer this question."
    return {
        "action": "complete",
        "answer": answer,
        "output": {"answer": answer, "answered": False},
        "summary": "No evidence found",
    }


def _scripted(kind: str, rest: str) -> dict[str, Any]:
    rest = (rest or "").strip()
    if kind in ("call_tool", "loop", "request_approval"):
        name, _, arguments = rest.partition(" ")
        decoded, _ = decode_arguments(arguments)
        if kind == "request_approval":
            return {
                "action": "request_approval",
                "tool": name,
                "arguments": decoded,
                "message": "Please confirm this action before I take it.",
                "summary": f"Asking to run {name}",
            }
        return {
            "action": "call_tool",
            "tool": name,
            "arguments": decoded,
            "summary": f"Calling {name}",
        }
    if kind == "progress":
        return {"action": "report_progress", "message": rest or "Working on it."}
    if kind == "complete":
        if rest.startswith("{"):
            output, _ = decode_arguments(rest)
            return {"action": "complete", "answer": "Done.", "output": output}
        return {"action": "complete", "answer": rest or "Done."}
    if kind == "fail":
        return {"action": "fail", "reason": rest or "Cannot do this."}
    if kind == "garbage":
        return {"action": "dance", "steps": 3}  # not the decision schema: repaired, then refused
    return {"action": "call_tool"}  # invalid: no tool named


def _goal_query(goal: str) -> str:
    body = goal.split("## Input", 1)[0]
    body = body.replace("## Goal", "").strip()
    return " ".join(body.split())[:200] or "documents"


def _last_observation(messages: Any) -> Any:
    for message in reversed(messages):
        if message.role is Role.USER and message.text.startswith("Observation"):
            match = _OBSERVATION.search(message.text)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    return None
            return None
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

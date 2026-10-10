"""Alias → ordered candidate models (architecture doc 12.3).

Pure functions: given a registry snapshot, the organization's policy and the request, decide
which concrete models may serve it and in which order. The gateway executes the list.
"""

from collections.abc import Collection
from dataclasses import dataclass

from nexa_ai.errors import AIContextTooLarge, AIError, AIModelUnavailable, AIProviderError
from nexa_ai.registry import Alias, ModelSpec, OrganizationPolicy, Registry
from nexa_ai.types import Capability, ChatRequest, ImagePart, ToolCallPart, ToolResultPart


@dataclass(frozen=True, slots=True)
class Candidate:
    alias: Alias
    model: ModelSpec
    position: int  # 0 = the preferred target

    @property
    def is_fallback(self) -> bool:
        return self.position > 0


def required_capabilities(request: ChatRequest, *, stream: bool) -> frozenset[Capability]:
    required = {Capability.CHAT}
    if stream:
        required.add(Capability.STREAMING)
    if request.wants_structured_output:
        required.add(Capability.STRUCTURED_OUTPUT)
    for message in request.messages:
        for part in message.parts:
            if isinstance(part, ImagePart):
                required.add(Capability.VISION)
            elif isinstance(part, ToolCallPart | ToolResultPart):
                required.add(Capability.TOOL_CALLING)
    return frozenset(required)


def resolve_alias(registry: Registry, policy: OrganizationPolicy, name: str | None) -> Alias:
    if name is None:
        alias = registry.default_alias
        if alias is None:
            raise AIModelUnavailable("No default model is configured.")
    else:
        alias = registry.get(name)
        if alias is None or not alias.is_active:
            raise AIModelUnavailable(f"Unknown model '{name}'.")
    if not policy.allows(alias):
        raise AIModelUnavailable(f"Model '{alias.name}' is not enabled for this organization.")
    return alias


def candidates(
    alias: Alias,
    *,
    required: frozenset[Capability],
    available_providers: Collection[str],
    estimated_input_tokens: int,
    max_output_tokens: int,
) -> list[Candidate]:
    """Every target that could serve the request, in priority order. Empty means none can."""
    found: list[Candidate] = []
    for position, model in enumerate(alias.targets):
        if not model.is_active or model.provider not in available_providers:
            continue
        if not model.supports(required):
            continue
        if estimated_input_tokens + min(max_output_tokens, model.max_output_tokens) > (
            model.context_window
        ):
            continue
        found.append(Candidate(alias=alias, model=model, position=position))
    return found


def explain_empty(
    alias: Alias,
    *,
    required: frozenset[Capability],
    available_providers: Collection[str],
    estimated_input_tokens: int,
) -> AIError:
    """The most useful reason why no candidate was found, for the error message.

    Missing providers or targets are an operations fault (503, retryable later); a missing
    capability or an oversized input is about the request (422 / 413)."""
    active = [t for t in alias.targets if t.is_active]
    if not active:
        return AIProviderError(f"Model '{alias.name}' is temporarily unavailable.")
    configured = [t for t in active if t.provider in available_providers]
    if not configured:
        return AIProviderError(f"Model '{alias.name}' is temporarily unavailable.")
    capable = [t for t in configured if t.supports(required)]
    if not capable:
        missing = sorted(c.value for c in required - alias.capabilities)
        what = ", ".join(missing) if missing else "the requested features"
        return AIModelUnavailable(f"Model '{alias.name}' does not support {what}.")
    largest = max(t.context_window for t in capable)
    return AIContextTooLarge(
        f"The input (about {estimated_input_tokens} tokens) plus the requested output does "
        f"not fit the context window of '{alias.name}' ({largest} tokens)."
    )

"""Helpers for tests of the gateway and of code that uses it. Not imported by application code."""

from typing import Any
from uuid import UUID, uuid4

from nexa_ai.cost import Price
from nexa_ai.registry import Alias, ModelSpec, OrganizationPolicy, Registry
from nexa_ai.types import Capability, ChatRequest, Message, Role

ALL_PHASE4 = frozenset({Capability.CHAT, Capability.STREAMING, Capability.STRUCTURED_OUTPUT})

PRIMARY = ModelSpec(
    id=uuid4(),
    provider="mock",
    provider_model="mock-primary",
    display_name="Mock primary",
    capabilities=ALL_PHASE4,
    context_window=8_000,
    max_output_tokens=2_000,
    price=Price(
        input_micros_per_million=3_000_000, output_micros_per_million=15_000_000, id=uuid4()
    ),
)
BACKUP = ModelSpec(
    id=uuid4(),
    provider="mock",
    provider_model="mock-backup",
    display_name="Mock backup",
    capabilities=ALL_PHASE4,
    context_window=16_000,
    max_output_tokens=4_000,
    price=Price(
        input_micros_per_million=1_000_000, output_micros_per_million=5_000_000, id=uuid4()
    ),
)
CHAT_ONLY = ModelSpec(
    id=uuid4(),
    provider="mock",
    provider_model="mock-chat-only",
    display_name="Mock chat only",
    capabilities=frozenset({Capability.CHAT}),
    context_window=8_000,
    max_output_tokens=1_000,
)


def make_registry(*aliases: Alias) -> Registry:
    """nexa-balanced (default: primary, then backup), nexa-fast (backup), nexa-basic (chat
    only), nexa-reasoning (primary, needs a grant)."""
    defaults = aliases or (
        Alias(
            name="nexa-balanced",
            display_name="NEXA Balanced",
            is_default=True,
            targets=(PRIMARY, BACKUP),
        ),
        Alias(name="nexa-fast", display_name="NEXA Fast", targets=(BACKUP,)),
        Alias(name="nexa-basic", display_name="NEXA Basic", targets=(CHAT_ONLY,)),
        Alias(
            name="nexa-reasoning",
            display_name="NEXA Reasoning",
            available_by_default=False,
            targets=(PRIMARY,),
        ),
    )
    return Registry(aliases={alias.name: alias for alias in defaults})


class StaticCatalog:
    def __init__(
        self, registry: Registry, policies: dict[UUID, OrganizationPolicy] | None = None
    ) -> None:
        self._registry = registry
        self.policies = policies or {}

    async def registry(self) -> Registry:
        return self._registry

    async def policy(self, organization_id: UUID) -> OrganizationPolicy:
        return self.policies.get(organization_id, OrganizationPolicy())


async def no_sleep(_: float) -> None:
    return None


def ask(text: str, **fields: Any) -> ChatRequest:
    return ChatRequest(messages=[Message(role=Role.USER, content=text)], **fields)

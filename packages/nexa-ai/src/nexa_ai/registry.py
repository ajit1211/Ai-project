"""The model registry as the gateway sees it: an immutable snapshot.

Core owns the tables (apps/core/nexa_core/ai/models.py); Runtime loads them into this shape
and refreshes it every few seconds. Nothing here touches a database.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from nexa_ai.cost import Price
from nexa_ai.types import Capability


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One concrete model at one provider."""

    id: UUID | None
    provider: str
    provider_model: str
    display_name: str
    capabilities: frozenset[Capability]
    context_window: int
    max_output_tokens: int
    is_active: bool = True
    price: Price | None = None
    # Some models only run at their default sampling temperature; the gateway then drops the
    # caller's value instead of letting the provider reject the request.
    accepts_temperature: bool = True

    def supports(self, required: frozenset[Capability]) -> bool:
        return required <= self.capabilities


@dataclass(frozen=True, slots=True)
class Alias:
    """A logical model such as `nexa-fast`, with its candidates in priority order."""

    name: str
    display_name: str
    description: str = ""
    is_active: bool = True
    is_default: bool = False
    available_by_default: bool = True
    targets: tuple[ModelSpec, ...] = ()

    @property
    def capabilities(self) -> frozenset[Capability]:
        """What a caller may rely on: the union of the active targets' capabilities."""
        sets = [t.capabilities for t in self.targets if t.is_active]
        return frozenset().union(*sets) if sets else frozenset()

    @property
    def context_window(self) -> int:
        active = [t.context_window for t in self.targets if t.is_active]
        return min(active) if active else 0

    @property
    def max_output_tokens(self) -> int:
        active = [t.max_output_tokens for t in self.targets if t.is_active]
        return min(active) if active else 0


@dataclass(frozen=True, slots=True)
class Registry:
    aliases: Mapping[str, Alias] = field(default_factory=dict)
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def default_alias(self) -> Alias | None:
        for alias in self.aliases.values():
            if alias.is_default and alias.is_active:
                return alias
        return None

    def get(self, name: str) -> Alias | None:
        return self.aliases.get(name)


@dataclass(frozen=True, slots=True)
class OrganizationPolicy:
    """AI settings for one organization. Platform-owned; defaults apply without a row.

    The limits here are per organization. Per-user limits are platform settings keyed by the
    user alone, so they follow a person across every organization they belong to."""

    ai_enabled: bool = True
    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    tokens_per_day: int | None = None
    max_output_tokens: int | None = None
    concurrent_requests: int | None = None
    # Aliases granted to this organization beyond those available to everyone.
    granted_aliases: frozenset[str] = frozenset()

    def allows(self, alias: Alias) -> bool:
        return alias.available_by_default or alias.name in self.granted_aliases

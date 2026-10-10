"""The model registry and organization AI policies, read from Core's tables and cached.

The registry is one snapshot per process, refreshed every `ttl` seconds by whichever request
first finds it stale (one refresh at a time). If the database is unreachable, the previous
snapshot is served for up to `max_stale` seconds and then requests fail with
AI_PROVIDER_UNAVAILABLE, so a model disabled in the admin stops being routed within the TTL
when the database is healthy, and never more than `max_stale` later when it is not.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_ai.cost import Price
from nexa_ai.errors import AIProviderError
from nexa_ai.registry import Alias, ModelSpec, OrganizationPolicy, Registry
from nexa_ai.types import Capability
from nexa_common.logs import get_logger
from nexa_runtime.ai import tables as t

log = get_logger("nexa.runtime.ai.catalog")

_KNOWN_CAPABILITIES = {c.value for c in Capability}
_POLICY_CACHE_LIMIT = 10_000
# After a failed load, wait this long before asking the database again; meanwhile requests
# get the stale copy (or fail fast) instead of queueing behind connection timeouts.
_RETRY_SECONDS = 5.0


class DatabaseCatalog:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        ttl: float = 30.0,
        max_stale: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._ttl = ttl
        self._max_stale = max_stale
        self._clock = clock
        self._registry: Registry | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self._policies: dict[UUID, tuple[float, OrganizationPolicy]] = {}
        self._registry_retry_at = 0.0
        self._policy_retry_at = 0.0

    async def registry(self) -> Registry:
        if self._registry is not None and self._clock() - self._loaded_at < self._ttl:
            return self._registry
        if self._clock() < self._registry_retry_at:
            return self._stale_registry(None)
        async with self._lock:
            if self._registry is not None and self._clock() - self._loaded_at < self._ttl:
                return self._registry  # another request refreshed it while we waited
            if self._clock() < self._registry_retry_at:
                return self._stale_registry(None)
            try:
                self._registry = await self._load_registry()
                self._loaded_at = self._clock()
            except Exception as exc:
                self._registry_retry_at = self._clock() + _RETRY_SECONDS
                return self._stale_registry(exc)
            return self._registry

    def _stale_registry(self, exc: Exception | None) -> Registry:
        age = self._clock() - self._loaded_at
        error = type(exc).__name__ if exc else "backing off"
        if self._registry is None or age > self._max_stale:
            log.error("ai.registry.unavailable", error=error)
            raise AIProviderError("AI models are temporarily unavailable.") from exc
        if exc is not None:
            log.warning("ai.registry.stale", age_seconds=round(age), error=error)
        return self._registry

    async def policy(self, organization_id: UUID) -> OrganizationPolicy:
        cached = self._policies.get(organization_id)
        if cached is not None and self._clock() - cached[0] < self._ttl:
            return cached[1]
        if self._clock() < self._policy_retry_at:
            return self._stale_policy(cached, None)
        try:
            policy = await self._load_policy(organization_id)
        except Exception as exc:
            self._policy_retry_at = self._clock() + _RETRY_SECONDS
            return self._stale_policy(cached, exc)
        if len(self._policies) >= _POLICY_CACHE_LIMIT:
            self._policies.clear()
        self._policies[organization_id] = (self._clock(), policy)
        return policy

    def _stale_policy(
        self, cached: tuple[float, OrganizationPolicy] | None, exc: Exception | None
    ) -> OrganizationPolicy:
        if cached is not None and self._clock() - cached[0] < self._max_stale:
            return cached[1]
        log.error("ai.policy.unavailable", error=type(exc).__name__ if exc else "backing off")
        raise AIProviderError("AI settings are temporarily unavailable.") from exc

    def invalidate(self) -> None:
        self._registry = None
        self._policies.clear()

    async def _load_registry(self) -> Registry:
        now = datetime.now(UTC)
        async with self._engine.connect() as conn:
            # The price in force per model: the latest effective_from that is not in the future.
            prices: dict[UUID, Price] = {}
            for row in await conn.execute(
                select(t.ai_model_prices)
                .where(t.ai_model_prices.c.effective_from <= now)
                .order_by(t.ai_model_prices.c.model_id, t.ai_model_prices.c.effective_from.desc())
            ):
                prices.setdefault(
                    row.model_id,
                    Price(
                        input_micros_per_million=row.input_micros_per_million,
                        output_micros_per_million=row.output_micros_per_million,
                        cached_input_micros_per_million=row.cached_input_micros_per_million,
                        cache_write_micros_per_million=row.cache_write_micros_per_million,
                        id=row.id,
                    ),
                )
            models = {
                row.id: ModelSpec(
                    id=row.id,
                    provider=row.provider,
                    provider_model=row.provider_model,
                    display_name=row.display_name,
                    capabilities=frozenset(
                        Capability(c) for c in row.capabilities if c in _KNOWN_CAPABILITIES
                    ),
                    context_window=row.context_window,
                    max_output_tokens=row.max_output_tokens,
                    is_active=row.is_active,
                    price=prices.get(row.id),
                    accepts_temperature=row.accepts_temperature,
                )
                for row in await conn.execute(select(t.ai_models))
            }
            targets: dict[UUID, list[ModelSpec]] = {}
            for row in await conn.execute(
                select(t.ai_alias_targets).order_by(
                    t.ai_alias_targets.c.alias_id, t.ai_alias_targets.c.priority
                )
            ):
                if row.model_id in models:
                    targets.setdefault(row.alias_id, []).append(models[row.model_id])
            aliases = {
                row.name: Alias(
                    name=row.name,
                    display_name=row.display_name,
                    description=row.description,
                    is_active=row.is_active,
                    is_default=row.is_default,
                    available_by_default=row.available_by_default,
                    targets=tuple(targets.get(row.id, ())),
                )
                for row in await conn.execute(select(t.ai_model_aliases))
            }
        return Registry(aliases=aliases, loaded_at=now)

    async def _load_policy(self, organization_id: UUID) -> OrganizationPolicy:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(t.ai_organization_policies).where(
                        t.ai_organization_policies.c.organization_id == organization_id
                    )
                )
            ).first()
            granted = (
                (
                    await conn.execute(
                        select(t.ai_model_aliases.c.name)
                        .select_from(
                            t.ai_organization_alias_grants.join(
                                t.ai_model_aliases,
                                t.ai_model_aliases.c.id
                                == t.ai_organization_alias_grants.c.alias_id,
                            )
                        )
                        .where(t.ai_organization_alias_grants.c.organization_id == organization_id)
                    )
                )
                .scalars()
                .all()
            )
        if row is None:
            return OrganizationPolicy(granted_aliases=frozenset(granted))
        return OrganizationPolicy(
            ai_enabled=row.ai_enabled,
            requests_per_minute=row.requests_per_minute,
            requests_per_day=row.requests_per_day,
            tokens_per_day=row.tokens_per_day,
            max_output_tokens=row.max_output_tokens,
            concurrent_requests=row.concurrent_requests,
            granted_aliases=frozenset(granted),
        )

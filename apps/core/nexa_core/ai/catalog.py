"""The seed AI catalog: which concrete models back each logical model, and their prices.

`sync_ai_catalog()` runs after every migration. It creates rows that are missing and never
changes rows that exist, so an operator's edits in the admin (a deactivated model, a new
price, a reordered fallback chain) survive deploys.

Model IDs and prices were taken from the providers' official model and pricing pages on
2026-10-08 (see `PRICE_SOURCE`). Prices change: check them before relying on cost figures,
and record a change as a new price row rather than editing the old one.

Every alias ends with the mock model so that a development machine without API keys still
works. Runtime only registers the mock provider in development and test, so in staging and
production those targets are skipped.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from django.db import transaction

from nexa_core.ai.models import (
    AIAliasTarget,
    AIEmbeddingModel,
    AIModel,
    AIModelAlias,
    AIModelPrice,
)

PRICES_EFFECTIVE_FROM = datetime(2026, 10, 8, tzinfo=UTC)
PRICE_SOURCE = "Provider pricing pages, 2026-10-08"

_FULL = ["chat", "streaming", "structured_output", "tool_calling", "vision", "reasoning"]
_MOCK_CAPABILITIES = ["chat", "streaming", "structured_output"]


@dataclass(frozen=True)
class SeedModel:
    provider: str
    provider_model: str
    display_name: str
    context_window: int
    max_output_tokens: int
    # USD per million tokens: input, output, cached input, cache write
    prices: tuple[str, str, str, str]
    capabilities: tuple[str, ...] = tuple(_FULL)
    accepts_temperature: bool = False


@dataclass(frozen=True)
class SeedAlias:
    name: str
    display_name: str
    description: str
    targets: tuple[str, ...]  # provider_model, in priority order
    is_default: bool = False
    is_active: bool = True
    available_by_default: bool = True


MODELS: tuple[SeedModel, ...] = (
    # Anthropic. Cache reads and 5-minute cache writes per the pricing page.
    SeedModel("anthropic", "claude-haiku-5-5", "Claude Haiku 5.5", 1_000_000, 128_000,
              ("0.10", "0.50", "0.01", "0.125")),
    SeedModel("anthropic", "claude-sonnet-5-5", "Claude Sonnet 5.5", 1_000_000, 128_000,
              ("2.00", "10.00", "0.10", "2.50")),
    SeedModel("anthropic", "claude-opus-5-5", "Claude Opus 5.5", 1_000_000, 128_000,
              ("4.00", "20.00", "0.20", "5.00")),
    SeedModel("anthropic", "claude-fable-5-1", "Claude Fable 5.1", 1_000_000, 128_000,
              ("10.00", "50.00", "0.25", "12.50")),
    # OpenAI. Maximum input is 922k tokens; the window counts input plus output.
    SeedModel("openai", "gpt-6-luna", "GPT-6 Luna", 1_050_000, 128_000,
              ("0.10", "0.50", "0.01", "0")),
    SeedModel("openai", "gpt-6.1-sol", "GPT-6.1 Sol", 1_050_000, 128_000,
              ("2.00", "10.00", "0.10", "0")),
    SeedModel("openai", "gpt-6-astra", "GPT-6 Astra", 1_050_000, 128_000,
              ("10.00", "50.00", "1.00", "0")),
    # Deterministic, free, development and tests only.
    SeedModel("mock", "mock-1", "Mock model", 200_000, 8_192, ("0", "0", "0", "0"),
              capabilities=tuple(_MOCK_CAPABILITIES), accepts_temperature=True),
)  # fmt: skip

ALIASES: tuple[SeedAlias, ...] = (
    SeedAlias(
        "nexa-fast",
        "NEXA Fast",
        "Quick, inexpensive answers: titles, rewrites, classification.",
        ("claude-haiku-5-5", "gpt-6-luna", "mock-1"),
    ),
    SeedAlias(
        "nexa-balanced",
        "NEXA Balanced",
        "The default: strong general answers at a moderate price.",
        ("claude-sonnet-5-5", "gpt-6.1-sol", "mock-1"),
        is_default=True,
    ),
    SeedAlias(
        "nexa-reasoning",
        "NEXA Reasoning",
        "Hard problems: planning, analysis, long reasoning. About five times the cost.",
        ("claude-fable-5-1", "gpt-6-astra", "mock-1"),
        available_by_default=False,
    ),
    SeedAlias(
        "nexa-vision",
        "NEXA Vision",
        "Image understanding. Image input arrives with the chat engine (Phase 05).",
        ("claude-opus-5-5", "gpt-6.1-sol"),
        is_active=False,
    ),
)


@dataclass(frozen=True)
class SeedEmbeddingModel:
    key: str
    display_name: str
    provider: str
    provider_model: str
    dimensions: int
    version: str = ""
    max_batch_size: int = 64
    max_input_tokens: int = 8_191
    relevance_floor: float | None = None
    is_default: bool = False
    notes: str = ""


# Phase 09 (docs/architecture/vector-search.md). No prices are seeded: none was verified for
# this release, and costs are never invented; an operator records the current price in the
# admin. The relevance floors are heuristics measured on the evaluation set (see the doc),
# not confidences.
EMBEDDING_MODELS: tuple[SeedEmbeddingModel, ...] = (
    SeedEmbeddingModel(
        "nexa-embed-small",
        "NEXA Embed Small",
        "openai",
        "text-embedding-3-small",
        1536,
        max_batch_size=128,
        relevance_floor=0.2,
        is_default=True,
        notes="Default. Needs AI_OPENAI_API_KEY; document text is sent to OpenAI.",
    ),
    SeedEmbeddingModel(
        "nexa-embed-mock",
        "Mock embeddings (development and tests)",
        "mock",
        "mock-embed-hash",
        256,
        version="hash-v2",
        max_batch_size=64,
        relevance_floor=0.2,
        notes="Deterministic feature hashing: lexical, not semantic. Refused outside "
        "development and test.",
    ),
)


def sync_embedding_models() -> None:
    """Create missing embedding models; never change existing rows (operators edit them)."""
    has_default = AIEmbeddingModel.objects.filter(is_default=True).exists()
    for seed in EMBEDDING_MODELS:
        AIEmbeddingModel.objects.get_or_create(
            key=seed.key,
            defaults={
                "display_name": seed.display_name,
                "provider": seed.provider,
                "provider_model": seed.provider_model,
                "dimensions": seed.dimensions,
                "version": seed.version,
                "max_batch_size": seed.max_batch_size,
                "max_input_tokens": seed.max_input_tokens,
                "relevance_floor": seed.relevance_floor,
                "is_default": seed.is_default and not has_default,
                "use_cases": ["documents", "queries"],
                "notes": seed.notes,
            },
        )


def _micros(usd: str) -> int:
    return int((Decimal(usd) * 1_000_000).to_integral_value())


@transaction.atomic
def sync_ai_catalog() -> None:
    models: dict[str, AIModel] = {}
    for seed in MODELS:
        model, created = AIModel.objects.get_or_create(
            provider=seed.provider,
            provider_model=seed.provider_model,
            defaults={
                "display_name": seed.display_name,
                "capabilities": list(seed.capabilities),
                "context_window": seed.context_window,
                "max_output_tokens": seed.max_output_tokens,
                "accepts_temperature": seed.accepts_temperature,
            },
        )
        if created or not model.prices.exists():
            input_, output, cached, write = (_micros(p) for p in seed.prices)
            AIModelPrice.objects.get_or_create(
                model=model,
                effective_from=PRICES_EFFECTIVE_FROM,
                defaults={
                    "input_micros_per_million": input_,
                    "output_micros_per_million": output,
                    "cached_input_micros_per_million": cached,
                    "cache_write_micros_per_million": write,
                    "source": PRICE_SOURCE,
                },
            )
        models[seed.provider_model] = model

    has_default = AIModelAlias.objects.filter(is_default=True).exists()
    for spec in ALIASES:
        alias, created = AIModelAlias.objects.get_or_create(
            name=spec.name,
            defaults={
                "display_name": spec.display_name,
                "description": spec.description,
                "is_active": spec.is_active,
                "is_default": spec.is_default and not has_default,
                "available_by_default": spec.available_by_default,
            },
        )
        if created:
            for priority, provider_model in enumerate(spec.targets, start=1):
                AIAliasTarget.objects.create(
                    alias=alias, model=models[provider_model], priority=priority
                )


def sync_after_migrate(**_: Any) -> None:
    from django.db import connection

    # Also fires when the app is migrated back to zero, when the tables no longer exist.
    tables = connection.introspection.table_names()
    if "ai_models" in tables:
        sync_ai_catalog()
    if "ai_embedding_models" in tables:
        with transaction.atomic():
            sync_embedding_models()

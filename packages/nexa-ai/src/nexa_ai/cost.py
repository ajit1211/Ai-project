"""Cost in micro-dollars, computed from the price version in force when the call was made.

Integer arithmetic only: a usage row's cost is reproducible and never drifts with float
rounding. Prices are stored per million tokens in micro-USD (1 USD = 1_000_000 micros), so
"$3.00 per million input tokens" is `input_micros_per_million = 3_000_000`.

Each component (uncached input, cache reads, cache writes, output) is rounded half-up to the
nearest micro-dollar separately, so a total can differ from rounding the exact sum by at most
two micro-dollars.
"""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from nexa_ai.providers.base import TokenCounts

MICROS_PER_USD = 1_000_000
TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True, slots=True)
class Price:
    input_micros_per_million: int
    output_micros_per_million: int
    cached_input_micros_per_million: int = 0
    cache_write_micros_per_million: int = 0
    id: UUID | None = None


def _component(tokens: int, micros_per_million: int) -> int:
    return (tokens * micros_per_million + TOKENS_PER_PRICE_UNIT // 2) // TOKENS_PER_PRICE_UNIT


def cost_micros(usage: TokenCounts, price: Price) -> int:
    """Cache reads and writes are priced separately; the rest of the input at the input rate."""
    cached = max(0, min(usage.cached_input_tokens, usage.input_tokens))
    written = max(0, min(usage.cache_write_input_tokens, usage.input_tokens - cached))
    uncached = usage.input_tokens - cached - written
    return (
        _component(uncached, price.input_micros_per_million)
        + _component(cached, price.cached_input_micros_per_million)
        + _component(written, price.cache_write_micros_per_million)
        + _component(usage.output_tokens, price.output_micros_per_million)
    )


def micros_to_usd(micros: int) -> str:
    """A six-decimal USD string such as "0.001234", for API responses."""
    return str((Decimal(micros) / MICROS_PER_USD).quantize(Decimal("0.000001")))


def usd_to_micros(amount: str | Decimal) -> int:
    """For seed data and admin forms: "3.00" -> 3_000_000."""
    return int((Decimal(amount) * MICROS_PER_USD).to_integral_value())

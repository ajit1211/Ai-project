"""Embedding models and results."""

from dataclasses import dataclass, field
from uuid import UUID

DISTANCES = ("cosine",)


@dataclass(frozen=True, slots=True)
class EmbeddingModel:
    """A logical embedding model (`key`) and the concrete model behind it.

    Two models' vectors are comparable only when their `space` is equal: same provider, same
    provider model and revision, same dimensions and distance. Changing any of those is a new
    model with a new space, never an edit of an existing one.
    """

    key: str
    provider: str
    provider_model: str
    dimensions: int
    version: str = ""
    distance: str = "cosine"
    max_batch_size: int = 64
    max_input_tokens: int = 8_191
    # USD micros per million input tokens; None when no reliable price is recorded.
    price_micros_per_million: int | None = None
    # Vector results below this cosine similarity are dropped. A heuristic floor that is
    # specific to the model's score distribution; None keeps every result. Not a confidence.
    relevance_floor: float | None = None
    is_default: bool = False
    id: UUID | None = None

    def __post_init__(self) -> None:
        if self.dimensions < 1:
            raise ValueError("dimensions must be positive")
        if self.distance not in DISTANCES:
            raise ValueError(f"unsupported distance {self.distance!r}")
        if self.max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")

    @property
    def space(self) -> str:
        version = self.version or "unversioned"
        return f"{self.provider}/{self.provider_model}@{version}:{self.dimensions}:{self.distance}"


@dataclass(frozen=True, slots=True)
class Embeddings:
    """Unit-length vectors, one per input text, in input order."""

    vectors: list[list[float]]
    model: EmbeddingModel
    input_tokens: int = 0
    # "provider" when every request reported its tokens, otherwise "estimated".
    usage_source: str = "provider"
    latency_ms: int = 0
    requests: int = 0
    retries: int = 0
    batch_sizes: list[int] = field(default_factory=list)

    @property
    def cost_micros(self) -> int | None:
        price = self.model.price_micros_per_million
        if price is None:
            return None
        return (self.input_tokens * price + 999_999) // 1_000_000

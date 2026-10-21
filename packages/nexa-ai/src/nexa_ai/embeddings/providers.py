"""Embedding provider adapters.

An adapter sends one batch to one concrete model and returns the raw vectors and the tokens
the provider reported, or raises a `nexa_ai.errors` error. Batching, retries, validation and
normalization belong to `Embedder`, so they behave the same for every provider.

- **OpenAI** (`/v1/embeddings` through the official SDK, SDK retries off). The
  `text-embedding-3` family accepts a `dimensions` parameter; it is sent so a model row's
  dimensions are what the provider returns.
- **Mock** (development and tests only; Runtime and Core refuse it elsewhere). Feature
  hashing of words and character trigrams: deterministic, and texts that share words get
  similar vectors. It is a lexical stand-in for tests, **not** a semantic model: it does not
  know that "car" and "automobile" are related, and must never back a real deployment.
"""

import hashlib
import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx2
import openai

from nexa_ai.errors import AIError, AIProviderError, AIRateLimited, AITimeout
from nexa_ai.tokens import estimate_text_tokens

_WORD = re.compile(r"[^\W_]+")
# English function words (snowball english.stop): the mock hashes content words, as a
# lexical feature extractor would, so unrelated texts do not look alike for sharing "the".
_STOP_WORD_TEXT = (
    "i me my myself we our ours ourselves you your yours yourself yourselves he him his "
    "himself she her hers herself it its itself they them their theirs themselves what "
    "which who whom this that these those am is are was were be been being have has had "
    "having do does did doing a an the and but if or because as until while of at by for "
    "with about against between into through during before after above below to from up "
    "down in out on off over under again further then once here there when where why how "
    "all any both each few more most other some such no nor not only own same so than too "
    "very s t can will just don should now"
)
_STOP_WORDS = frozenset(_STOP_WORD_TEXT.split())
MOCK_VERSION = "hash-v2"


@dataclass(frozen=True, slots=True)
class ProviderEmbeddings:
    vectors: list[list[float]]
    # None when the provider did not report usage.
    input_tokens: int | None


class EmbeddingProvider(Protocol):
    name: str

    async def embed(
        self,
        texts: Sequence[str],
        *,
        provider_model: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> ProviderEmbeddings: ...

    async def aclose(self) -> None: ...


class OpenAIEmbeddingProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        connect_timeout: float = 5.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._client = openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=0, http_client=http_client
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def embed(
        self,
        texts: Sequence[str],
        *,
        provider_model: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> ProviderEmbeddings:
        from nexa_ai.providers.openai import _map_error  # one mapping for every OpenAI call

        extra = {"dimensions": dimensions} if provider_model.startswith("text-embedding-3") else {}
        try:
            response = await self._client.embeddings.create(
                model=provider_model,
                input=list(texts),
                encoding_format="float",
                timeout=httpx2.Timeout(timeout_seconds, connect=self._connect_timeout),
                **extra,  # type: ignore[arg-type]
            )
        except Exception as exc:
            raise _map_error(exc, provider_model) from exc
        items = sorted(response.data, key=lambda item: item.index)
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
        return ProviderEmbeddings(
            vectors=[list(item.embedding) for item in items],
            input_tokens=int(tokens) if isinstance(tokens, int) else None,
        )


class MockEmbeddingProvider:
    """Deterministic feature-hashing vectors for development and tests (see module doc).

    `failures` scripts errors for the next calls, in order: "rate_limit", "provider",
    "timeout", "wrong_count", "wrong_dimensions", "not_finite".
    """

    name = "mock"

    def __init__(self, *, failures: Sequence[str] = ()) -> None:
        self._failures: deque[str] = deque(failures)
        self.calls: list[int] = []  # batch sizes, for tests

    def script_failures(self, *kinds: str) -> None:
        self._failures.extend(kinds)

    async def aclose(self) -> None:
        return None

    async def embed(
        self,
        texts: Sequence[str],
        *,
        provider_model: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> ProviderEmbeddings:
        self.calls.append(len(texts))
        name = self.name
        failure = self._failures.popleft() if self._failures else None
        if failure == "rate_limit":
            raise AIRateLimited(
                retry_after_seconds=0.0,
                provider_status=429,
                provider=name,
                provider_model=provider_model,
            )
        if failure == "provider":
            raise AIProviderError(provider_status=503, provider=name, provider_model=provider_model)
        if failure == "timeout":
            raise AITimeout(provider=name, provider_model=provider_model)
        vectors = [hash_embedding(text, dimensions) for text in texts]
        if failure == "wrong_count":
            vectors = vectors[:-1]
        elif failure == "wrong_dimensions":
            vectors = [vector[:-1] for vector in vectors]
        elif failure == "not_finite":
            vectors[0][0] = math.nan
        return ProviderEmbeddings(
            vectors=vectors, input_tokens=sum(estimate_text_tokens(text) for text in texts)
        )


def hash_embedding(text: str, dimensions: int) -> list[float]:
    """Signed feature hashing (version `MOCK_VERSION`) of lowercased content words (weight 1)
    and their character trigrams (weight 0.5), L2-normalized; a text made only of function
    words hashes all its words. Deterministic across processes and platforms."""
    vector = [0.0] * dimensions
    words = _WORD.findall(text.lower())
    words = [w for w in words if w not in _STOP_WORDS] or words
    for word in words:
        features = [(word, 1.0)]
        padded = f"#{word}#"
        features.extend((padded[i : i + 3], 0.5) for i in range(len(padded) - 2))
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % dimensions
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vector[index] += sign * weight
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def build_embedding_providers(
    *,
    openai_api_key: str | None,
    openai_base_url: str | None = None,
    mock_enabled: bool = False,
) -> Mapping[str, EmbeddingProvider]:
    """The adapters a process may use: OpenAI when its key is configured, the mock only
    where the caller allows it (development and test)."""
    providers: dict[str, EmbeddingProvider] = {}
    if openai_api_key:
        providers["openai"] = OpenAIEmbeddingProvider(
            api_key=openai_api_key, base_url=openai_base_url
        )
    if mock_enabled:
        providers["mock"] = MockEmbeddingProvider()
    return providers


__all__ = [
    "AIError",
    "EmbeddingProvider",
    "MockEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "ProviderEmbeddings",
    "build_embedding_providers",
    "hash_embedding",
]

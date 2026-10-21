"""The embedding capability: the mock's determinism, batching, retries, validation and
normalization in `Embedder`, and the OpenAI adapter against a mocked HTTP API."""

import json
import math
from typing import Any

import httpx2
import pytest

from nexa_ai.embeddings import (
    AIEmbeddingInvalid,
    Embedder,
    EmbeddingModel,
    MockEmbeddingProvider,
    OpenAIEmbeddingProvider,
    choose_model,
    hash_embedding,
)
from nexa_ai.errors import (
    AIAuthenticationError,
    AIContextTooLarge,
    AIInvalidRequest,
    AIModelUnavailable,
    AIProviderError,
    AIRateLimited,
    AITimeout,
)
from nexa_ai.testing import no_sleep

pytestmark = pytest.mark.anyio

MOCK = EmbeddingModel(
    key="nexa-embed-mock",
    provider="mock",
    provider_model="mock-embed",
    dimensions=64,
    version="hash-v2",
    max_batch_size=4,
)
SMALL = EmbeddingModel(
    key="nexa-embed-small",
    provider="openai",
    provider_model="text-embedding-3-small",
    dimensions=3,
    max_batch_size=8,
    price_micros_per_million=20_000,
)
DEFAULT = EmbeddingModel(
    key="nexa-embed-small",
    provider="openai",
    provider_model="text-embedding-3-small",
    dimensions=1536,
    is_default=True,
)
OTHER = EmbeddingModel(
    key="nexa-embed-large",
    provider="openai",
    provider_model="text-embedding-3-large",
    dimensions=3072,
)
REGISTRY = [DEFAULT, OTHER, MOCK]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def embedder(provider: Any, **options: Any) -> Embedder:
    return Embedder({provider.name: provider}, sleep=no_sleep, **options)


class TestMock:
    def test_deterministic_and_unit_length(self) -> None:
        first = hash_embedding("Refunds take thirty days", 64)
        assert first == hash_embedding("Refunds take thirty days", 64)
        assert math.isclose(sum(x * x for x in first), 1.0)

    def test_shared_words_mean_closer_vectors(self) -> None:
        query = hash_embedding("refund window", 128)
        near = hash_embedding("The refund window is thirty days", 128)
        far = hash_embedding("Book flights two weeks ahead", 128)
        assert cosine(query, near) > cosine(query, far)

    def test_space_identifies_compatible_vectors(self) -> None:
        assert MOCK.space == "mock/mock-embed@hash-v2:64:cosine"
        assert SMALL.space == "openai/text-embedding-3-small@unversioned:3:cosine"
        with pytest.raises(ValueError, match="dimensions"):
            EmbeddingModel(key="x", provider="mock", provider_model="m", dimensions=0)


class TestEmbedder:
    async def test_documents_are_batched_and_normalized(self) -> None:
        provider = MockEmbeddingProvider()
        texts = [f"text number {i}" for i in range(10)]
        result = await embedder(provider).embed_documents(texts, MOCK)
        assert provider.calls == [4, 4, 2]
        assert result.batch_sizes == [4, 4, 2]
        assert len(result.vectors) == 10
        assert all(math.isclose(sum(x * x for x in v), 1.0) for v in result.vectors)
        assert result.vectors[3] == hash_embedding("text number 3", 64)
        assert (result.requests, result.retries, result.usage_source) == (3, 0, "provider")
        assert result.input_tokens > 0

    async def test_a_token_budget_also_splits_batches(self) -> None:
        provider = MockEmbeddingProvider()
        texts = ["word " * 300] * 3  # about 500 estimated tokens each
        await embedder(provider, max_batch_tokens=1_000).embed_documents(texts, MOCK)
        assert provider.calls == [2, 1]

    async def test_query(self) -> None:
        result = await embedder(MockEmbeddingProvider()).embed_query("refund window", MOCK)
        assert result.vectors == [hash_embedding("refund window", 64)]

    async def test_rate_limits_and_provider_errors_are_retried(self) -> None:
        provider = MockEmbeddingProvider(failures=["rate_limit", "provider"])
        result = await embedder(provider, max_attempts=3).embed_documents(["a text"], MOCK)
        assert len(result.vectors) == 1
        assert (result.requests, result.retries) == (3, 2)

    async def test_retries_are_bounded(self) -> None:
        provider = MockEmbeddingProvider(failures=["provider"] * 5)
        with pytest.raises(AIProviderError):
            await embedder(provider, max_attempts=3).embed_documents(["a text"], MOCK)
        assert len(provider.calls) == 3

    async def test_a_timeout_is_retried_once(self) -> None:
        provider = MockEmbeddingProvider(failures=["timeout", "timeout"])
        with pytest.raises(AITimeout):
            await embedder(provider, max_attempts=5).embed_documents(["a text"], MOCK)
        assert len(provider.calls) == 2

    @pytest.mark.parametrize("failure", ["wrong_count", "wrong_dimensions", "not_finite"])
    async def test_invalid_output_is_refused_and_not_retried(self, failure: str) -> None:
        provider = MockEmbeddingProvider(failures=[failure])
        with pytest.raises(AIEmbeddingInvalid):
            await embedder(provider).embed_documents(["one text", "two texts"], MOCK)
        assert len(provider.calls) == 1

    async def test_invalid_input(self) -> None:
        tool = embedder(MockEmbeddingProvider())
        with pytest.raises(AIInvalidRequest):
            await tool.embed_documents([], MOCK)
        with pytest.raises(AIInvalidRequest):
            await tool.embed_documents(["fine", "   "], MOCK)
        long_model = EmbeddingModel(
            key="tiny", provider="mock", provider_model="m", dimensions=8, max_input_tokens=5
        )
        with pytest.raises(AIContextTooLarge):
            await tool.embed_query("far too many words for this tiny model", long_model)

    async def test_an_unconfigured_provider(self) -> None:
        tool = embedder(MockEmbeddingProvider())
        assert not tool.supports(SMALL)
        with pytest.raises(AIModelUnavailable):
            await tool.embed_query("text", SMALL)

    async def test_cost_only_with_a_price(self) -> None:
        result = await embedder(MockEmbeddingProvider()).embed_query("text", MOCK)
        assert result.cost_micros is None


# --- OpenAI adapter -----------------------------------------------------------------------------


Handler = Any


def openai_with(handler: Handler) -> OpenAIEmbeddingProvider:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAIEmbeddingProvider(api_key="sk-test-openai-key", http_client=client)


def embedding_response(vectors: list[list[float]], tokens: int = 7) -> dict[str, Any]:
    return {
        "object": "list",
        "model": "text-embedding-3-small",
        # Out of order on purpose: the adapter sorts by index.
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in reversed(list(enumerate(vectors)))
        ],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


class TestOpenAI:
    async def test_request_and_response(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path.endswith("/embeddings")
            assert request.headers["authorization"] == "Bearer sk-test-openai-key"
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json=embedding_response([[3.0, 0.0, 4.0], [0, 1, 0]]))

        result = await Embedder({"openai": openai_with(handler)}, sleep=no_sleep).embed_documents(
            ["first", "second"], SMALL
        )
        assert seen == [
            {
                "model": "text-embedding-3-small",
                "input": ["first", "second"],
                "encoding_format": "float",
                "dimensions": 3,
            }
        ]
        assert result.vectors == [[0.6, 0.0, 0.8], [0.0, 1.0, 0.0]]
        assert (result.input_tokens, result.usage_source) == (7, "provider")
        assert result.cost_micros == 1

    @pytest.mark.parametrize(
        ("status", "body", "error"),
        [
            (429, {"error": {"message": "slow down", "type": "rate_limit"}}, AIRateLimited),
            (
                401,
                {"error": {"message": "bad key", "type": "invalid_request_error"}},
                AIAuthenticationError,
            ),
            (
                400,
                {"error": {"message": "bad input", "type": "invalid_request_error"}},
                AIInvalidRequest,
            ),
            (503, {"error": {"message": "down", "type": "server_error"}}, AIProviderError),
        ],
    )
    async def test_errors_are_normalized(
        self, status: int, body: dict[str, Any], error: type[Exception]
    ) -> None:
        provider = openai_with(lambda request: httpx2.Response(status, json=body))
        with pytest.raises(error):
            await provider.embed(
                ["x"], provider_model="text-embedding-3-small", dimensions=3, timeout_seconds=5
            )

    async def test_wrong_dimensions_from_the_provider(self) -> None:
        provider = openai_with(
            lambda request: httpx2.Response(200, json=embedding_response([[1.0, 2.0]]))
        )
        with pytest.raises(AIEmbeddingInvalid):
            await Embedder({"openai": provider}, sleep=no_sleep).embed_query("x", SMALL)


class TestRegistry:
    """Core and Runtime must pick the same model from the same rows and settings."""

    def choose(
        self, configured: str = "", providers: tuple[str, ...] = (), mock: bool = False
    ) -> Any:
        return choose_model(
            REGISTRY, configured=configured, available_providers=providers, mock_allowed=mock
        )

    def test_the_configured_model_and_no_other(self) -> None:
        assert self.choose("nexa-embed-large", ("openai", "mock"), mock=True) is OTHER
        # Named but unknown: nothing, never a silent substitute.
        assert self.choose("nexa-embed-missing", ("openai", "mock"), mock=True) is None

    def test_the_default_when_its_provider_is_configured(self) -> None:
        assert self.choose(providers=("openai", "mock"), mock=True) is DEFAULT

    def test_the_mock_only_where_allowed(self) -> None:
        assert self.choose(providers=("mock",), mock=True) is MOCK
        # In production the default is reported (and is unavailable): no mock vectors.
        assert self.choose(providers=(), mock=False) is DEFAULT

    def test_no_models(self) -> None:
        assert (
            choose_model([], configured="", available_providers=("openai",), mock_allowed=True)
            is None
        )

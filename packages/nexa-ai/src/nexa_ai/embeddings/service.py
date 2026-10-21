"""`Embedder`: the one place that calls embedding providers (see the package docstring).

Errors are the gateway's (`nexa_ai.errors`): rate limits and provider failures are retried
with backoff, timeouts once more; invalid requests, authentication failures and invalid
output are not retried. There is no fallback to another model: its vectors would not be
comparable with the ones already stored.
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence

from nexa_ai.embeddings.providers import EmbeddingProvider, ProviderEmbeddings
from nexa_ai.embeddings.types import EmbeddingModel, Embeddings
from nexa_ai.errors import (
    AIContextTooLarge,
    AIError,
    AIInvalidRequest,
    AIModelUnavailable,
    AIOutputInvalid,
    AITimeout,
)
from nexa_ai.retry import RetryPolicy
from nexa_ai.tokens import estimate_text_tokens
from nexa_common.logs import get_logger

log = get_logger("nexa.ai.embeddings")

DEFAULT_MAX_BATCH_TOKENS = 100_000


class AIEmbeddingInvalid(AIOutputInvalid):
    """The provider answered, but not with one finite vector of the right size per text."""


class Embedder:
    def __init__(
        self,
        providers: Mapping[str, EmbeddingProvider],
        *,
        timeout: float = 30.0,
        max_attempts: int = 3,
        retry: RetryPolicy | None = None,
        max_batch_tokens: int = DEFAULT_MAX_BATCH_TOKENS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.providers = dict(providers)
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.retry = retry or RetryPolicy(max_retries=max(0, max_attempts - 1), max_delay=8.0)
        self.max_batch_tokens = max_batch_tokens
        self._sleep = sleep
        self._clock = clock

    def supports(self, model: EmbeddingModel) -> bool:
        """Whether this process can call the model's provider (its key is configured)."""
        return model.provider in self.providers

    async def embed_query(self, text: str, model: EmbeddingModel) -> Embeddings:
        return await self.embed_documents([text], model)

    async def embed_documents(
        self, texts: Sequence[str], model: EmbeddingModel, *, batch_size: int | None = None
    ) -> Embeddings:
        provider = self.providers.get(model.provider)
        if provider is None:
            raise AIModelUnavailable(
                f"The embedding provider '{model.provider}' is not configured.",
                provider=model.provider,
                provider_model=model.provider_model,
            )
        if not texts:
            raise AIInvalidRequest("There is no text to embed.")
        for text in texts:
            if not text or not text.strip():
                raise AIInvalidRequest("Cannot embed empty text.")
            if estimate_text_tokens(text) > model.max_input_tokens:
                raise AIContextTooLarge("A text is longer than the embedding model accepts.")
        started = self._clock()
        size = max(1, min(batch_size or model.max_batch_size, model.max_batch_size))
        vectors: list[list[float]] = []
        tokens, estimated, requests, retries = 0, False, 0, 0
        sizes: list[int] = []
        for batch in self._batches(texts, size):
            result, attempts = await self._call(provider, batch, model)
            requests += attempts
            retries += attempts - 1
            sizes.append(len(batch))
            vectors.extend(_validated(result.vectors, len(batch), model))
            if result.input_tokens is None:
                estimated = True
                tokens += sum(estimate_text_tokens(text) for text in batch)
            else:
                tokens += result.input_tokens
        return Embeddings(
            vectors=vectors,
            model=model,
            input_tokens=tokens,
            usage_source="estimated" if estimated else "provider",
            latency_ms=max(0, int((self._clock() - started) * 1000)),
            requests=requests,
            retries=retries,
            batch_sizes=sizes,
        )

    async def aclose(self) -> None:
        for provider in self.providers.values():
            try:
                await provider.aclose()
            except Exception as exc:
                log.warning("ai.embeddings.close_failed", error=type(exc).__name__)

    def _batches(self, texts: Sequence[str], size: int) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for text in texts:
            cost = estimate_text_tokens(text)
            if current and (len(current) >= size or current_tokens + cost > self.max_batch_tokens):
                batches.append(current)
                current, current_tokens = [], 0
            current.append(text)
            current_tokens += cost
        if current:
            batches.append(current)
        return batches

    async def _call(
        self, provider: EmbeddingProvider, batch: list[str], model: EmbeddingModel
    ) -> tuple[ProviderEmbeddings, int]:
        attempt = 0
        while True:
            attempt += 1
            try:
                async with asyncio.timeout(self.timeout + 1):
                    result = await provider.embed(
                        batch,
                        provider_model=model.provider_model,
                        dimensions=model.dimensions,
                        timeout_seconds=self.timeout,
                    )
                return result, attempt
            except TimeoutError as exc:
                error: AIError = AITimeout(
                    provider=model.provider, provider_model=model.provider_model
                )
                cause: BaseException = exc
            except AIError as exc:
                error, cause = exc, exc
            # Timeouts are worth one more try in a batch job; the policy decides the rest.
            delay = (
                0.0
                if isinstance(error, AITimeout) and attempt < min(2, self.max_attempts)
                else self.retry.delay(error, attempt - 1)
            )
            if delay is None or attempt >= self.max_attempts:
                log.warning(
                    "ai.embeddings.failed", attempts=attempt, batch=len(batch), **error.log_fields()
                )
                if error is cause:
                    raise error
                raise error from cause
            log.info(
                "ai.embeddings.retry",
                attempt=attempt,
                delay_s=round(delay, 2),
                **error.log_fields(),
            )
            await self._sleep(delay)


def _validated(vectors: object, expected: int, model: EmbeddingModel) -> list[list[float]]:
    """One finite, non-zero vector of the model's dimensions per text, made unit length."""
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise AIEmbeddingInvalid(
            "The embedding provider returned the wrong number of vectors.",
            provider=model.provider,
            provider_model=model.provider_model,
        )
    out: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != model.dimensions:
            raise AIEmbeddingInvalid(
                "The embedding provider returned vectors of the wrong size.",
                provider=model.provider,
                provider_model=model.provider_model,
            )
        values = [float(x) for x in vector]
        if not all(math.isfinite(x) for x in values):
            raise AIEmbeddingInvalid(
                "The embedding provider returned invalid numbers.",
                provider=model.provider,
                provider_model=model.provider_model,
            )
        norm = math.sqrt(sum(x * x for x in values))
        if norm == 0.0:
            raise AIEmbeddingInvalid(
                "The embedding provider returned an empty vector.",
                provider=model.provider,
                provider_model=model.provider_model,
            )
        out.append([x / norm for x in values])
    return out

"""Embeddings: turning text into vectors for semantic search (Phase 09).

A capability of its own beside the chat gateway, not a kind of chat request: embedding models
have their own identities (an embedding model is never a chat alias), and vectors from
different models live in different spaces, so there is no fallback from one model to another.

    Embedder.embed_documents(texts, model) ─► batches within the model's limits
        ─► provider adapter (OpenAI, or the mock in development and tests)
        ─► retries for rate limits and provider failures, a timeout per request
        ─► validation: one vector per text, the model's dimensions, finite, non-zero
        ─► unit length (cosine similarity is then a dot product)

docs/architecture/vector-search.md describes how Core and Runtime use it.
"""

from nexa_ai.embeddings.providers import (
    EmbeddingProvider,
    MockEmbeddingProvider,
    OpenAIEmbeddingProvider,
    ProviderEmbeddings,
    build_embedding_providers,
    hash_embedding,
)
from nexa_ai.embeddings.registry import choose_model
from nexa_ai.embeddings.service import AIEmbeddingInvalid, Embedder
from nexa_ai.embeddings.types import EmbeddingModel, Embeddings

__all__ = [
    "AIEmbeddingInvalid",
    "Embedder",
    "EmbeddingModel",
    "EmbeddingProvider",
    "Embeddings",
    "MockEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "ProviderEmbeddings",
    "build_embedding_providers",
    "choose_model",
    "hash_embedding",
]

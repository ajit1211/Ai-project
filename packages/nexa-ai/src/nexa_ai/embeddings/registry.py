"""Which embedding model a process uses.

Core embeds documents and Runtime embeds questions; both must use the same model, or the
question's vector would be compared with vectors from another space. Both load the active rows
of `ai_embedding_models` and apply this one rule with the same settings:

1. `EMBEDDING_MODEL` names a model: that model, and no other. If its provider is not configured
   here, semantic search is unavailable (never silently another model).
2. Otherwise the registry's default model, when its provider is configured.
3. Otherwise, only where the mock provider is allowed (development and test), the mock model,
   so a machine without API keys still exercises the whole pipeline.
4. Otherwise the default model, reported as unavailable; or None when there is none.
"""

from collections.abc import Collection, Sequence

from nexa_ai.embeddings.types import EmbeddingModel


def choose_model(
    models: Sequence[EmbeddingModel],
    *,
    configured: str,
    available_providers: Collection[str],
    mock_allowed: bool,
) -> EmbeddingModel | None:
    if configured:
        return next((m for m in models if m.key == configured), None)
    default = next((m for m in models if m.is_default), None)
    if default is not None and default.provider in available_providers:
        return default
    if mock_allowed:
        mock = next((m for m in models if m.provider == "mock"), None)
        if mock is not None:
            return mock
    return default

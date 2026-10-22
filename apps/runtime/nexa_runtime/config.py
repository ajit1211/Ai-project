"""Typed configuration for Runtime, read from the environment and the repository's .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from nexa_common.env import Environment

REPO_ROOT = Path(__file__).resolve().parents[3]

_MOCK_ENVIRONMENTS = ("development", "test")


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Required: the mock provider, its control phrases and the API docs are switched on by
    # `development` and `test`, so a deployment that forgot to say must not default to them.
    nexa_env: Environment
    log_level: str = "INFO"
    log_json: bool = True

    # Runtime connects as `nexa_runtime`, which may only write the tables Runtime owns (doc 11.3).
    runtime_database_url: str
    redis_queue_url: str = "redis://localhost:6379/0"
    # Where Core publishes its JWT verification keys; the only thing Runtime fetches from Core.
    core_jwks_url: str = "http://localhost:8000/.well-known/jwks.json"

    # --- AI Gateway (docs/architecture/ai-gateway.md) ------------------------------------
    # Provider credentials. A provider without a key is not registered, and the router skips
    # its models. Keys never leave this process: not in responses, logs or the database.
    ai_anthropic_api_key: SecretStr | None = None
    ai_anthropic_base_url: str | None = None
    ai_openai_api_key: SecretStr | None = None
    ai_openai_base_url: str | None = None
    # The deterministic mock provider. Default: on in development and test, off elsewhere;
    # turning it on in staging or production is refused at startup.
    ai_mock_provider: bool | None = None

    # Seconds (see nexa_ai.providers.base.Timeouts).
    ai_timeout_connect: float = 5.0
    ai_timeout_request: float = 120.0
    ai_timeout_first_token: float = 45.0
    ai_timeout_idle: float = 30.0
    ai_timeout_total: float = 300.0

    ai_default_max_output_tokens: int = 2048
    ai_max_output_tokens_cap: int = 8192

    # Platform default limits. Organization limits can be overridden per organization by
    # platform staff (ai_organization_policies); user limits follow the user across every
    # organization they belong to.
    ai_org_requests_per_minute: int = 60
    ai_org_requests_per_day: int = 2_000
    ai_org_tokens_per_day: int = 2_000_000
    ai_org_concurrent_requests: int = 20
    ai_user_requests_per_minute: int = 20
    ai_user_requests_per_day: int = 500
    ai_user_tokens_per_day: int = 500_000
    ai_user_concurrent_requests: int = 4
    # When Redis is down, each process enforces limit / this count on its own.
    ai_quota_process_count: int = 2

    # Model registry cache: refreshed every `ttl`; a stale copy is served for at most
    # `max_stale` while the database is unreachable.
    ai_registry_ttl_seconds: float = 30.0
    ai_registry_max_stale_seconds: float = 300.0

    # --- Chat engine (docs/architecture/chat-engine.md) --------------------------------
    # The recent-message window sent as context: never more messages or estimated tokens
    # than this, and always within the model's context window.
    chat_context_max_messages: int = 40
    chat_context_max_tokens: int = 24_000
    chat_max_output_tokens: int = 2048
    chat_title_max_chars: int = 80
    # New conversations per user per minute (message sends are limited by the AI quotas).
    chat_conversations_per_minute: int = 20

    # Memory engine (docs/architecture/memory-engine.md). Extraction and summaries are gateway
    # calls on `memory_model`, metered to the user like any other.
    memory_enabled: bool = True
    memory_model: str = "nexa-fast"
    # How many relevant memories, and how many standing preferences, one turn may recall.
    memory_retrieval_limit: int = 5
    memory_preference_limit: int = 5
    # Context budget shares, in estimated tokens.
    memory_context_max_tokens: int = 1_500
    memory_summary_max_tokens: int = 2_000
    # Summarize once this many messages lie outside the summary, keeping the newest in full.
    memory_summary_after_messages: int = 20
    memory_summary_keep_recent: int = 10
    memory_max_candidates_per_turn: int = 8
    memory_candidate_ttl_days: int = 30
    memory_conversation_ttl_days: int = 30

    # Document-grounded answers (docs/architecture/rag.md). Token figures are estimates
    # (three characters per token); a request may ask for fewer sources, never more.
    rag_enabled: bool = True
    rag_max_documents: int = 20
    rag_top_k: int = 6
    rag_max_top_k: int = 12
    rag_candidates: int = 40
    rag_context_max_tokens: int = 3_000
    rag_passage_max_chars: int = 2_400
    rag_min_matched_terms: int = 1
    rag_retrieval_timeout_ms: int = 5_000
    rag_max_question_chars: int = 4_000
    rag_queries_per_minute: int = 20
    rag_excerpt_chars: int = 400
    rag_source_detail_chars: int = 4_000

    # Semantic and hybrid retrieval (docs/architecture/vector-search.md). EMBEDDING_ENABLED and
    # EMBEDDING_MODEL must match Core's: questions and documents are embedded by the same model.
    embedding_enabled: bool = True
    embedding_model: str = ""
    # Used when a question does not choose: hybrid where semantic search runs, else lexical.
    rag_retrieval_mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    rag_vector_candidates: int = 40
    rag_rrf_k: int = 60
    rag_query_embedding_timeout_seconds: float = 10.0
    rag_hnsw_ef_search: int = 100
    rag_semantic_status_ttl_seconds: float = 30.0

    # AI tool calling (docs/architecture/tools.md). Every limit applies per reply ("turn").
    tools_enabled: bool = True
    tools_max_iterations: int = Field(default=4, ge=1, le=10)
    tools_max_calls_per_turn: int = Field(default=8, ge=1, le=32)
    tools_max_concurrency: int = Field(default=4, ge=1, le=8)
    tools_turn_timeout_seconds: float = Field(default=120.0, ge=5.0, le=600.0)
    tools_max_total_output_bytes: int = Field(default=48_000, ge=1_000, le=512_000)
    tools_calls_per_minute: int = Field(default=30, ge=1, le=600)
    tools_approval_ttl_seconds: int = Field(default=900, ge=60, le=86_400)

    @property
    def async_database_url(self) -> URL:
        return make_url(self.runtime_database_url).set(drivername="postgresql+asyncpg")

    @property
    def mock_provider_enabled(self) -> bool:
        if self.ai_mock_provider is None:
            return self.nexa_env in _MOCK_ENVIRONMENTS
        return self.ai_mock_provider

    @model_validator(mode="after")
    def _refuse_mock_in_deployed_environments(self) -> Self:
        if self.ai_mock_provider and self.nexa_env not in _MOCK_ENVIRONMENTS:
            raise ValueError("AI_MOCK_PROVIDER may only be enabled in development and test")
        return self


@lru_cache
def get_settings() -> RuntimeSettings:
    return RuntimeSettings()

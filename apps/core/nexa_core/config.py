"""Typed configuration for Core, read from the environment and the repository's .env (doc 27.14).

A missing or malformed value stops the process at startup instead of failing later at runtime.
"""

from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from nexa_common.env import Environment

REPO_ROOT = Path(__file__).resolve().parents[3]
# The placeholder from .env.example; refused outside development.
_PLACEHOLDER_SECRET = "replace-with-a-long-random-string"  # noqa: S105


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    nexa_env: Environment = "development"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str
    redis_queue_url: str = "redis://localhost:6379/0"
    redis_cache_url: str = "redis://localhost:6379/1"

    django_secret_key: SecretStr
    django_debug: bool = False
    django_allowed_hosts: str = "localhost,127.0.0.1"

    # Number of reverse proxies in front of Core whose X-Forwarded-For can be trusted.
    trusted_proxy_count: int = 0

    # Authentication (docs/architecture/authentication.md). JWT_PRIVATE_KEY (PEM) wins over
    # the file; development and test create the file on first use.
    jwt_private_key: SecretStr | None = None
    jwt_private_key_file: Path = REPO_ROOT / ".secrets" / "jwt_signing_key.pem"
    # Where the SPA lives: links in emails point here, and it is the only trusted origin.
    frontend_url: str = "http://localhost:5173"
    # Extra browser origins allowed to call the API. Empty by default: the SPA is served from
    # the same origin as the API, so no CORS is needed at all.
    cors_allowed_origins: str = ""

    # Email: files under .local/mail in development, SMTP (Amazon SES) when deployed.
    email_backend: str = "django.core.mail.backends.filebased.EmailBackend"
    email_file_path: Path = REPO_ROOT / ".local" / "mail"
    email_host: str = "localhost"
    email_port: int = 587
    email_host_user: str = ""
    email_host_password: SecretStr = SecretStr("")
    default_from_email: str = "NEXA <no-reply@nexa.local>"

    # Documents (docs/architecture/documents.md): private files under .local/documents in
    # development; an object store behind the same interface when deployed.
    documents_storage_backend: str = "local"
    documents_storage_root: Path = REPO_ROOT / ".local" / "documents"
    documents_max_upload_bytes: int = 50 * 1024 * 1024
    documents_max_text_chars: int = 5_000_000
    documents_max_pages: int = 500
    documents_max_csv_rows: int = 50_000
    documents_processing_timeout_seconds: int = 600
    documents_max_attempts: int = 5
    documents_uploads_per_hour: int = 60
    documents_org_storage_bytes: int = 5 * 1024 * 1024 * 1024
    documents_purge_after_days: int = 30

    # The search index behind document-grounded answers (docs/architecture/rag.md). Sizes
    # are characters; a token is estimated at three characters.
    rag_chunk_target_chars: int = 1_200
    rag_chunk_max_chars: int = 2_000
    rag_chunk_overlap_chars: int = 200
    rag_max_chunks_per_document: int = 20_000
    rag_index_timeout_seconds: int = 300
    rag_index_requests_per_hour: int = 30

    @property
    def allowed_hosts(self) -> list[str]:
        return _split(self.django_allowed_hosts)

    @property
    def cors_origins(self) -> list[str]:
        return _split(self.cors_allowed_origins)

    @model_validator(mode="after")
    def _refuse_unsafe_deployments(self) -> Self:
        if self.nexa_env in ("staging", "production"):
            if self.django_debug:
                raise ValueError("DJANGO_DEBUG must be false outside development")
            secret = self.django_secret_key.get_secret_value()
            if secret == _PLACEHOLDER_SECRET or len(secret) < 50:
                raise ValueError(
                    "DJANGO_SECRET_KEY must be a real secret of at least 50 characters"
                )
        return self


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache
def get_settings() -> CoreSettings:
    return CoreSettings()

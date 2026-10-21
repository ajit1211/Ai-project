"""Django settings for NEXA Core.

One module for every environment: values come from typed configuration in `nexa_core.config`,
never from per-environment settings files (doc 27.3).
"""

import logging
from pathlib import Path

import dj_database_url
import django_stubs_ext

from nexa_common.logs import configure_logging
from nexa_core.config import get_settings

# Lets admin and form classes take type parameters at runtime (e.g. ModelAdmin[User]).
django_stubs_ext.monkeypatch()

env = get_settings()
configure_logging(service="core", env=env.nexa_env, level=env.log_level, json_logs=env.log_json)
# RequestContextMiddleware logs every request; silence the dev server's duplicate access log.
logging.getLogger("django.server").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = env.django_secret_key.get_secret_value()
DEBUG = env.django_debug
ALLOWED_HOSTS = env.allowed_hosts
NEXA_TRUSTED_PROXY_COUNT = env.trusted_proxy_count
NEXA_ENV = env.nexa_env
NEXA_REDIS_QUEUE_URL = env.redis_queue_url
NEXA_FRONTEND_URL = env.frontend_url.rstrip("/")
NEXA_JWT_PRIVATE_KEY = env.jwt_private_key.get_secret_value() if env.jwt_private_key else None
NEXA_JWT_PRIVATE_KEY_FILE = env.jwt_private_key_file
# Documents (docs/architecture/documents.md)
NEXA_DOCUMENTS_STORAGE_BACKEND = env.documents_storage_backend
NEXA_DOCUMENTS_STORAGE_ROOT = env.documents_storage_root
NEXA_DOCUMENTS_MAX_UPLOAD_BYTES = env.documents_max_upload_bytes
NEXA_DOCUMENTS_MAX_TEXT_CHARS = env.documents_max_text_chars
NEXA_DOCUMENTS_MAX_PAGES = env.documents_max_pages
NEXA_DOCUMENTS_MAX_CSV_ROWS = env.documents_max_csv_rows
NEXA_DOCUMENTS_PROCESSING_TIMEOUT_SECONDS = env.documents_processing_timeout_seconds
NEXA_DOCUMENTS_MAX_ATTEMPTS = env.documents_max_attempts
NEXA_DOCUMENTS_UPLOADS_PER_HOUR = env.documents_uploads_per_hour
NEXA_DOCUMENTS_ORG_STORAGE_BYTES = env.documents_org_storage_bytes
NEXA_DOCUMENTS_PURGE_AFTER_DAYS = env.documents_purge_after_days
# The search index for document-grounded answers (docs/architecture/rag.md)
NEXA_RAG_CHUNK_TARGET_CHARS = env.rag_chunk_target_chars
NEXA_RAG_CHUNK_MAX_CHARS = env.rag_chunk_max_chars
NEXA_RAG_CHUNK_OVERLAP_CHARS = env.rag_chunk_overlap_chars
NEXA_RAG_MAX_CHUNKS_PER_DOCUMENT = env.rag_max_chunks_per_document
NEXA_RAG_INDEX_TIMEOUT_SECONDS = env.rag_index_timeout_seconds
NEXA_RAG_INDEX_REQUESTS_PER_HOUR = env.rag_index_requests_per_hour

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "corsheaders",
    "rest_framework",
    "nexa_core.identity",
    "nexa_core.organizations",
    "nexa_core.audit",
    "nexa_core.ai",
    "nexa_core.chat",
    "nexa_core.memory",
    "nexa_core.documents",
    "nexa_core.system",
]

MIDDLEWARE = [
    "nexa_core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "nexa_core.urls"
WSGI_APPLICATION = "nexa_core.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Database: Core connects as `nexa_core`, the role that owns the schema (doc 11.3).
DATABASES = {
    "default": dj_database_url.parse(env.database_url, conn_max_age=60, conn_health_checks=True),
}
DATABASES["default"]["OPTIONS"] = {"connect_timeout": 3}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Accounts (doc 19.5): custom user model from the first migration, Argon2id hashing,
# a minimum length and a common-password check, but no composition rules.
AUTH_USER_MODEL = "identity.User"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.redis_cache_url,
        "KEY_PREFIX": "nexa:core",
        "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR.parent / "staticfiles"

# API defaults are secure: JSON only, authentication required unless a view opts out.
# Authentication is a short-lived JWT in the Authorization header (docs/architecture/authentication.md).
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["nexa_core.access.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "nexa_core.exceptions.envelope_exception_handler",
    "UNAUTHENTICATED_USER": None,
    # JSON is the only renderer, so `?format=` selects nothing; switched off so it stays free
    # for filters (the document library filters on `format`).
    "URL_FORMAT_OVERRIDE": None,
}

# Celery (doc 21.3): at-least-once delivery, one task prefetched at a time, results kept in
# PostgreSQL rather than a result backend.
CELERY_BROKER_URL = env.redis_queue_url
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
CELERY_TASK_DEFAULT_QUEUE = "core.default"
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_SOFT_TIME_LIMIT = 50
CELERY_TASK_TIME_LIMIT = 60
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_TIMEZONE = "UTC"
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_TASK_ROUTES = {
    "core.identity.send_email": {"queue": "core.email"},
    # Parsing is CPU-bound and may take minutes: its own queue, so a burst of uploads
    # never delays an email (doc 21.2). Its tasks carry their own time limits.
    "core.documents.process": {"queue": "core.documents"},
    "core.documents.purge": {"queue": "core.documents"},
    "core.documents.index": {"queue": "core.documents"},
}
# Uploads above this size are streamed to a temporary file instead of memory.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024

# Logging is configured above by nexa_common; stop Django from applying its own defaults.
LOGGING_CONFIG = None

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Browser access (docs/architecture/security.md). The SPA shares the API's origin, so CORS
# stays closed unless extra origins are listed explicitly; there is never a wildcard.
CORS_ALLOWED_ORIGINS = env.cors_origins
CORS_ALLOW_CREDENTIALS = bool(env.cors_origins)
CSRF_TRUSTED_ORIGINS = [NEXA_FRONTEND_URL, *env.cors_origins]

# Email
EMAIL_BACKEND = env.email_backend
EMAIL_FILE_PATH = env.email_file_path
EMAIL_HOST = env.email_host
EMAIL_PORT = env.email_port
EMAIL_HOST_USER = env.email_host_user
EMAIL_HOST_PASSWORD = env.email_host_password.get_secret_value()
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = env.default_from_email
EMAIL_TIMEOUT = 10

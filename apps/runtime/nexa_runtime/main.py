"""FastAPI application for NEXA Runtime."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from nexa_ai.gateway import Gateway
from nexa_ai.providers.base import AIProvider
from nexa_common.logs import configure_logging
from nexa_runtime.ai.catalog import DatabaseCatalog
from nexa_runtime.ai.providers import build_providers, timeouts
from nexa_runtime.ai.quota import RedisQuota
from nexa_runtime.ai.usage import DatabaseUsageSink
from nexa_runtime.api import api_router
from nexa_runtime.auth.jwks import JWKSClient
from nexa_runtime.chat.cancellation import CancelRegistry
from nexa_runtime.chat.service import ChatService
from nexa_runtime.config import RuntimeSettings, get_settings
from nexa_runtime.errors import register_exception_handlers
from nexa_runtime.health import router as health_router
from nexa_runtime.middleware import RequestContextMiddleware
from nexa_runtime.ratelimit import Limiter


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    jwks_client: JWKSClient | None = None,
    providers: Mapping[str, AIProvider] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        service="runtime",
        env=settings.nexa_env,
        level=settings.log_level,
        json_logs=settings.log_json,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Clients are created once per process and shared by every request (doc 27.4).
        app.state.engine = create_async_engine(
            settings.async_database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"timeout": 3},
            # Parameters are message text: keep them out of error messages, hence out of logs.
            hide_parameters=True,
        )
        app.state.redis = Redis.from_url(
            settings.redis_queue_url, socket_connect_timeout=1, socket_timeout=1
        )
        # Keys are fetched lazily on the first authenticated request, so Runtime can start
        # (and answer health checks) while Core is still coming up.
        app.state.jwks = jwks_client or JWKSClient(settings.core_jwks_url)
        # The AI Gateway (docs/architecture/ai-gateway.md): provider clients, the catalog
        # cache, quotas in Redis and the usage ledger, wired once per process.
        app.state.catalog = DatabaseCatalog(
            app.state.engine,
            ttl=settings.ai_registry_ttl_seconds,
            max_stale=settings.ai_registry_max_stale_seconds,
        )
        app.state.quota = RedisQuota(app.state.redis, settings)
        app.state.gateway = Gateway(
            providers=providers if providers is not None else build_providers(settings),
            catalog=app.state.catalog,
            quota=app.state.quota,
            usage=DatabaseUsageSink(app.state.engine),
            timeouts=timeouts(settings),
            default_max_output_tokens=settings.ai_default_max_output_tokens,
            max_output_tokens_cap=settings.ai_max_output_tokens_cap,
        )
        # The chat engine (docs/architecture/chat-engine.md) on top of the gateway.
        app.state.cancel = CancelRegistry(app.state.redis)
        app.state.limiter = Limiter(app.state.redis)
        app.state.chat = ChatService(
            engine=app.state.engine,
            gateway=app.state.gateway,
            cancel=app.state.cancel,
            limiter=app.state.limiter,
            settings=settings,
        )
        try:
            yield
        finally:
            # Finish message and usage writes for cancelled calls before the connections go away.
            await app.state.chat.drain()
            await app.state.gateway.aclose()
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    is_dev = settings.nexa_env == "development"
    app = FastAPI(
        title="NEXA Runtime",
        version=version("nexa-runtime"),
        lifespan=lifespan,
        docs_url="/docs" if is_dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if is_dev else None,
    )
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()

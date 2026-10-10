"""Liveness and readiness endpoints, polled by the load balancer and the dev status page."""

import asyncio
from collections.abc import Awaitable
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

router = APIRouter(tags=["system"])

_CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    return {"service": "runtime", "status": "ok", "version": request.app.version}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    show_detail = state.settings.nexa_env == "development"
    database, redis = await asyncio.gather(
        _timed(_check_database(state.engine), show_detail=show_detail),
        _timed(_check_redis(state.redis), show_detail=show_detail),
    )
    checks = {"database": database, "redis": redis}
    ok = all(check["status"] == "ok" for check in checks.values())
    return JSONResponse(
        {"service": "runtime", "status": "ok" if ok else "unavailable", "checks": checks},
        status_code=200 if ok else 503,
    )


async def _timed(check: Awaitable[None], *, show_detail: bool) -> dict[str, Any]:
    """Run one dependency check with a timeout; a failure is reported, never raised."""
    started = perf_counter()
    try:
        await asyncio.wait_for(check, _CHECK_TIMEOUT_SECONDS)
        result: dict[str, Any] = {"status": "ok"}
    except Exception as exc:
        result = {"status": "error", "error": type(exc).__name__}
        if show_detail:
            result["detail"] = str(exc)[:200]
    result["latency_ms"] = round((perf_counter() - started) * 1000, 1)
    return result


async def _check_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def _check_redis(client: Redis) -> None:
    await client.ping()

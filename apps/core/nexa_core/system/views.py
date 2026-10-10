"""Health endpoints and Django-level error handlers.

`/healthz` answers "is the process alive?"; `/readyz` answers "can it serve traffic?" and is
what a load balancer polls. Celery is reported but not critical: the API can serve requests
while workers are down (doc 11.6).
"""

from collections.abc import Callable
from importlib.metadata import version
from time import perf_counter
from typing import Any

import redis
from django.conf import settings
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from nexa_common.context import current_request_id
from nexa_common.errors import code_for_status, error_body
from nexa_core.celery import app as celery_app

_CRITICAL_CHECKS = ("database", "redis")


@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"service": "core", "status": "ok", "version": version("nexa-core")})


@require_GET
def readyz(request: HttpRequest) -> JsonResponse:
    checks = {"database": _timed(_check_database), "redis": _timed(_check_redis)}
    if checks["redis"]["status"] == "ok":
        checks["celery"] = _timed(_check_celery)
    else:
        checks["celery"] = {"status": "skipped", "reason": "redis unavailable"}
    checks["celery"]["critical"] = False

    critical_ok = all(checks[name]["status"] == "ok" for name in _CRITICAL_CHECKS)
    all_ok = all(check["status"] == "ok" for check in checks.values())
    status = "ok" if all_ok else "degraded" if critical_ok else "unavailable"
    return JsonResponse(
        {"service": "core", "status": status, "checks": checks},
        status=200 if critical_ok else 503,
    )


def not_found(request: HttpRequest, exception: Exception) -> JsonResponse:
    return _error_response(404)


def server_error(request: HttpRequest) -> JsonResponse:
    return _error_response(500)


def _error_response(status: int) -> JsonResponse:
    body = error_body(code=code_for_status(status), request_id=current_request_id())
    return JsonResponse(body, status=status)


def _timed(check: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one dependency check; a failure is reported, never raised."""
    started = perf_counter()
    try:
        result = {"status": "ok", **check()}
    except Exception as exc:
        result = {"status": "error", "error": type(exc).__name__}
        if settings.DEBUG:
            result["detail"] = str(exc)[:200]
    result["latency_ms"] = round((perf_counter() - started) * 1000, 1)
    return result


def _check_database() -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return {}


def _check_redis() -> dict[str, Any]:
    client = redis.Redis.from_url(
        settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1
    )
    try:
        client.ping()
    finally:
        client.close()
    return {}


def _check_celery() -> dict[str, Any]:
    replies = celery_app.control.ping(timeout=1.0)
    if not replies:
        raise RuntimeError("no Celery worker replied")
    return {"workers": len(replies)}

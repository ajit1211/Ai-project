"""Request context and access logging for Core (doc 23.2)."""

import time
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from nexa_common.context import REQUEST_ID_HEADER, bind_request_context, resolve_request_id
from nexa_common.logs import get_logger
from nexa_core import request_meta

log = get_logger("nexa.core.http")

# Polled constantly by load balancers and the dev status page; logged at DEBUG only.
_QUIET_PATHS = frozenset({"/healthz", "/readyz"})


class RequestContextMiddleware:
    """Assigns a request ID, binds it to every log line, and logs one line per request."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        bind_request_context(request_id=request_id)
        request_meta.bind(request)
        started = time.perf_counter()

        response = self.get_response(request)

        response[REQUEST_ID_HEADER] = request_id
        emit = log.debug if request.path in _QUIET_PATHS else log.info
        emit(
            "http.request.completed",
            method=request.method,
            path=request.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return response

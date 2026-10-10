"""Request context and access logging for Runtime (doc 23.2).

A pure ASGI middleware rather than `BaseHTTPMiddleware`, so it never buffers the SSE
streams that arrive in Phase 05.
"""

import json
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nexa_common.context import REQUEST_ID_HEADER, bind_request_context, resolve_request_id
from nexa_common.errors import ErrorCode, error_body
from nexa_common.logs import get_logger

log = get_logger("nexa.runtime.http")

_QUIET_PATHS = frozenset({"/healthz", "/readyz"})

# Runtime takes JSON only (uploads go to Core and object storage). Bodies are counted
# as they arrive, so a chunked request without Content-Length is capped too, before
# FastAPI buffers and parses it.
MAX_BODY_BYTES = 4 * 1024 * 1024


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        # Each request runs in its own task, so this binding cannot leak into other requests.
        # It is deliberately not cleared on the way out: the 500 handler, which runs outside
        # this middleware, still needs the request ID.
        bind_request_context(request_id=request_id)
        started = time.perf_counter()
        status = 500

        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await _reject_too_large(send, request_id)
            log.info(
                "http.request.completed",
                method=scope["method"],
                path=scope["path"],
                status=413,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise HTTPException(status_code=413)
            return message

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, counting_receive, send_with_request_id)
        finally:
            emit = log.debug if scope["path"] in _QUIET_PATHS else log.info
            emit(
                "http.request.completed",
                method=scope["method"],
                path=scope["path"],
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )


async def _reject_too_large(send: Send, request_id: str) -> None:
    body = json.dumps(error_body(code=ErrorCode.PAYLOAD_TOO_LARGE, request_id=request_id)).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

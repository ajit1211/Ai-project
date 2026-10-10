"""Translate every error into the API error envelope, once, at the boundary (doc 27.7)."""

from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexa_common.context import REQUEST_ID_HEADER, current_request_id
from nexa_common.errors import (
    ErrorCode,
    FieldError,
    NexaError,
    code_for_status,
    error_body,
    error_body_from,
)
from nexa_common.logs import get_logger

log = get_logger("nexa.runtime.errors")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NexaError, _nexa_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled_error)


async def _nexa_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(NexaError, exc)
    body = error_body_from(error, request_id=current_request_id())
    return JSONResponse(body, status_code=error.http_status, headers=error.headers or None)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(StarletteHTTPException, exc)
    body = error_body(code=code_for_status(error.status_code), request_id=current_request_id())
    return JSONResponse(body, status_code=error.status_code, headers=dict(error.headers or {}))


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(RequestValidationError, exc)
    details = [
        FieldError(
            # loc is e.g. ("body", "email"); drop the location part.
            field=".".join(str(part) for part in item["loc"][1:]) or str(item["loc"][0]),
            code=str(item["type"]),
            message=str(item["msg"]),
        )
        for item in error.errors()
    ]
    body = error_body(
        code=ErrorCode.VALIDATION_FAILED, request_id=current_request_id(), details=details
    )
    return JSONResponse(body, status_code=422)


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    # Runs outside RequestContextMiddleware, so the request ID header is added here.
    log.exception("http.request.failed", path=request.url.path)
    request_id = current_request_id()
    body = error_body(code=ErrorCode.INTERNAL_ERROR, request_id=request_id)
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return JSONResponse(body, status_code=500, headers=headers)

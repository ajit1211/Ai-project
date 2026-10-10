"""Translate DRF and NEXA errors into the API error envelope (doc 27.7).

Unexpected exceptions are not handled here: they reach Django's 500 handler, which answers
with the same envelope (`nexa_core.system.views.server_error`).
"""

from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions as drf
from rest_framework.response import Response

from nexa_common.context import current_request_id
from nexa_common.errors import (
    ErrorCode,
    FieldError,
    NexaError,
    code_for_status,
    error_body,
    error_body_from,
)

_WWW_AUTHENTICATE = 'Bearer realm="nexa"'


def envelope_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    request_id = current_request_id()

    if isinstance(exc, NexaError):
        response = Response(error_body_from(exc, request_id=request_id), status=exc.http_status)
        for header, value in exc.headers.items():
            response[header] = value
        if exc.http_status == 401:
            response["WWW-Authenticate"] = _WWW_AUTHENTICATE
        return response

    if isinstance(exc, Http404):
        exc = drf.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = drf.PermissionDenied()
    if not isinstance(exc, drf.APIException):
        return None

    if isinstance(exc, drf.ValidationError):
        body = error_body(
            code=ErrorCode.VALIDATION_FAILED,
            request_id=request_id,
            details=_field_errors(exc.detail),
        )
        return Response(body, status=422)

    # DRF reports a missing credential as 403 when no authenticator offers a challenge;
    # a missing credential is always 401 in NEXA.
    status = (
        401 if isinstance(exc, drf.NotAuthenticated | drf.AuthenticationFailed) else exc.status_code
    )
    response = Response(
        error_body(code=code_for_status(status), request_id=request_id), status=status
    )
    if auth_header := getattr(exc, "auth_header", None):
        response["WWW-Authenticate"] = auth_header
    if (wait := getattr(exc, "wait", None)) is not None:
        response["Retry-After"] = str(int(wait))
    return response


def _field_errors(detail: Any, field: str = "") -> list[FieldError]:
    """Flatten DRF's nested error detail into `[{field, code, message}]`."""
    if isinstance(detail, dict):
        errors: list[FieldError] = []
        for key, value in detail.items():
            errors += _field_errors(value, f"{field}.{key}" if field else str(key))
        return errors
    if isinstance(detail, list):
        errors = []
        for index, item in enumerate(detail):
            nested = isinstance(item, dict | list)
            errors += _field_errors(item, f"{field}[{index}]" if nested else field)
        return errors
    return [
        FieldError(
            field=field or "non_field_errors",
            code=str(getattr(detail, "code", "invalid")),
            message=str(detail),
        )
    ]

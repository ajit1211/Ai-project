"""Per-request context that every log line carries (doc 23.2)."""

import re

import structlog

from nexa_common.ids import new_request_id

REQUEST_ID_HEADER = "X-Request-ID"

# Only IDs that are safe to echo into headers and logs are accepted from callers.
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def resolve_request_id(incoming: str | None) -> str:
    """Keep a caller-supplied request ID when it is safe, otherwise mint a new one."""
    if incoming and _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return new_request_id()


def bind_request_context(**values: object) -> None:
    """Start a fresh logging context for this request or task."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**values)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()


def current_request_id() -> str | None:
    """The request ID bound for the current request or task, if any."""
    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None

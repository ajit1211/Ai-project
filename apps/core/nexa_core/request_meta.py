"""Who is calling: client IP and user agent for the current request, used by audit and limits.

Set once by `RequestContextMiddleware`; read anywhere without passing the request around.
"""

from contextvars import ContextVar
from dataclasses import dataclass

from django.conf import settings
from django.http import HttpRequest


@dataclass(frozen=True)
class RequestMeta:
    ip_address: str | None
    user_agent: str


_current: ContextVar[RequestMeta | None] = ContextVar("nexa_request_meta", default=None)


def client_ip(request: HttpRequest) -> str | None:
    """The caller's IP. `X-Forwarded-For` is trusted only behind the configured proxy count,
    since anyone can send that header."""
    proxies: int = settings.NEXA_TRUSTED_PROXY_COUNT
    if proxies > 0:
        forwarded = [part.strip() for part in request.headers.get("X-Forwarded-For", "").split(",")]
        if len(forwarded) >= proxies and forwarded[-proxies]:
            return forwarded[-proxies]
    address = request.META.get("REMOTE_ADDR")
    return str(address) if address else None


def bind(request: HttpRequest) -> None:
    _current.set(
        RequestMeta(
            ip_address=client_ip(request),
            user_agent=request.headers.get("User-Agent", "")[:512],
        )
    )


def current() -> RequestMeta:
    return _current.get() or RequestMeta(ip_address=None, user_agent="")

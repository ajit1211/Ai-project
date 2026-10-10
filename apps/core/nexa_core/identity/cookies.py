"""The refresh-token cookie used by browsers (docs/architecture/authentication.md).

HttpOnly: page scripts cannot read it, so an XSS bug cannot steal the long-lived credential.
Secure: never sent over plain HTTP (browsers treat http://localhost as secure).
SameSite=Strict + path /api/v1/auth: sent only to the auth endpoints, only from NEXA itself.

Because a cookie is sent automatically, the endpoints that read it also require a custom
header that cross-site forms cannot set, and check the Origin header when present.
"""

from datetime import datetime

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from nexa_common.errors import ErrorCode, NexaError

REFRESH_COOKIE = "nexa_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"
CSRF_HEADER = "X-NEXA-CSRF"


def set_refresh_cookie(response: HttpResponse, token: str, expires_at: datetime) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        expires=expires_at,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="Strict",
    )


def clear_refresh_cookie(response: HttpResponse) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, samesite="Strict")


def read_refresh_cookie(request: HttpRequest) -> str | None:
    """The cookie's value, after checking the request really comes from the NEXA web app."""
    token = request.COOKIES.get(REFRESH_COOKIE)
    if not token:
        return None
    origin = request.headers.get("Origin")
    if request.headers.get(CSRF_HEADER) != "1" or (
        origin is not None and origin not in settings.CSRF_TRUSTED_ORIGINS
    ):
        raise NexaError("Cross-site request refused.", code=ErrorCode.PERMISSION_DENIED, status=403)
    return token

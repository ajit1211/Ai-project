"""Redis-backed rate limits for authentication endpoints (ADR-025).

Fixed windows, counted per subject (an IP address or an account). Subjects are hashed before
they reach Redis, so keys never contain raw emails or IPs.

Every limit expires with its window: nothing here can lock a legitimate user out permanently.
If Redis is unavailable, limits fail open and log a warning; refusing every login because a
cache is down would turn a Redis outage into a full outage. A short circuit breaker stops each
request from paying a connection timeout while Redis is down.
"""

import hashlib
import time
from dataclasses import dataclass
from functools import lru_cache

import redis
from django.conf import settings
from redis.backoff import NoBackoff
from redis.retry import Retry

from nexa_common.errors import RateLimited
from nexa_common.logs import get_logger

log = get_logger("nexa.core.ratelimit")


@dataclass(frozen=True)
class Limit:
    name: str
    max_hits: int
    window_seconds: int


# Login: every attempt counts per IP; only failures count per account, and a successful
# login or password reset clears the account counter.
LOGIN_PER_IP = Limit("login_ip", 30, 5 * 60)
LOGIN_FAILURES_PER_ACCOUNT = Limit("login_failures_account", 10, 15 * 60)
REGISTER_PER_IP = Limit("register_ip", 5, 60 * 60)
PASSWORD_RESET_PER_IP = Limit("password_reset_ip", 10, 60 * 60)
PASSWORD_RESET_PER_ACCOUNT = Limit("password_reset_account", 3, 60 * 60)
VERIFICATION_RESEND_PER_IP = Limit("verification_resend_ip", 10, 60 * 60)
VERIFICATION_RESEND_PER_ACCOUNT = Limit("verification_resend_account", 3, 60 * 60)
TOKEN_REDEEM_PER_IP = Limit("token_redeem_ip", 30, 60 * 60)
INVITATIONS_PER_ORGANIZATION = Limit("invitations_org", 50, 60 * 60)


_BREAKER_SECONDS = 5.0
_unavailable_until = 0.0


@lru_cache
def _client() -> redis.Redis:
    return redis.Redis.from_url(
        settings.NEXA_REDIS_QUEUE_URL,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        retry=Retry(NoBackoff(), retries=0),
    )


def _available() -> bool:
    return time.monotonic() >= _unavailable_until


def _trip(limit: Limit) -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + _BREAKER_SECONDS
    log.warning("ratelimit.unavailable", limit=limit.name)


def _key(limit: Limit, subject: str) -> str:
    digest = hashlib.sha256(subject.encode()).hexdigest()[:32]
    window = int(time.time() // limit.window_seconds)
    return f"nexa:rl:{limit.name}:{digest}:{window}"


def retry_after(limit: Limit) -> int:
    return limit.window_seconds - int(time.time() % limit.window_seconds)


def hit(limit: Limit, subject: str) -> int | None:
    """Count one event. Returns the count in this window, or None if Redis is unavailable."""
    if not _available():
        return None
    key = _key(limit, subject)
    try:
        pipe = _client().pipeline()
        pipe.incr(key)
        pipe.expire(key, limit.window_seconds + 1)
        count, _ = pipe.execute()
    except redis.RedisError:
        _trip(limit)
        return None
    return int(count)


def count(limit: Limit, subject: str) -> int:
    if not _available():
        return 0
    try:
        value = _client().get(_key(limit, subject))
    except redis.RedisError:
        _trip(limit)
        return 0
    return int(value) if isinstance(value, bytes | str | int) else 0


def consume(limit: Limit, subject: str) -> None:
    """Count one event and raise RateLimited if this pushes the subject over the limit.

    Only the first refusal in a window is audited, so an attack cannot flood the audit log.
    """
    current = hit(limit, subject)
    if current is None or current <= limit.max_hits:
        return
    if current == limit.max_hits + 1:
        from nexa_core.audit.actions import AuditAction
        from nexa_core.audit.service import record

        record(AuditAction.RATE_LIMIT_EXCEEDED, metadata={"limit": limit.name})
    raise RateLimited(retry_after(limit))


def ensure_below(limit: Limit, subject: str) -> None:
    """Raise RateLimited if the subject is already at the limit, without counting."""
    if count(limit, subject) >= limit.max_hits:
        raise RateLimited(retry_after(limit))


def reset(limit: Limit, subject: str) -> None:
    if not _available():
        return
    try:
        _client().delete(_key(limit, subject))
    except redis.RedisError:
        _trip(limit)

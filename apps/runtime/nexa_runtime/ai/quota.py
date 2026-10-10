"""AI quotas: requests, tokens and concurrency, per organization and per user.

    org   requests/minute, requests/day, tokens/day, concurrent requests
    user  requests/minute, requests/day, tokens/day, concurrent requests

Organization limits come from the platform defaults, overridden per organization by platform
staff. User limits are platform defaults keyed by the user alone, so they follow a person
across every organization they belong to: creating more organizations does not buy more AI.

`reserve` runs before any provider call. One Lua script checks every counter and, only if
all of them have room, adds the request and the call's worst-case tokens to each: concurrent
requests can never jointly overshoot a limit. `settle` replaces the reserved tokens with the
real count and releases the concurrency slot, whatever the outcome of the call.

Windows are UTC: a minute is the clock minute, a day ends at 00:00 UTC.

If Redis is unreachable, quotas do not fail open, because an AI call costs money in a way a
login attempt does not. Each process enforces the same limits divided by the expected number
of processes, in memory, and logs that it is degraded. A short breaker keeps requests from
paying a Redis timeout each while it is down.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from redis.asyncio import Redis

from nexa_ai.context import AIRequestContext
from nexa_ai.errors import AIQuotaExceeded
from nexa_ai.registry import OrganizationPolicy
from nexa_common.logs import get_logger
from nexa_runtime.config import RuntimeSettings

log = get_logger("nexa.runtime.ai.quota")

_BREAKER_SECONDS = 5.0
_CONCURRENCY_RETRY_AFTER = 2

# ARGV per key: limit, amount, ttl. A negative ttl means "set the expiry only when the key
# has none": in-flight counters must not have their expiry pushed out by every new request,
# or a slot that was never released (a crash, a lost settle) would never expire.
_RESERVE = """
local n = #KEYS
for i = 1, n do
  local limit = tonumber(ARGV[(i - 1) * 3 + 1])
  local amount = tonumber(ARGV[(i - 1) * 3 + 2])
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  if current + amount > limit then
    return {i}
  end
end
for i = 1, n do
  local ttl = tonumber(ARGV[(i - 1) * 3 + 3])
  redis.call('INCRBY', KEYS[i], tonumber(ARGV[(i - 1) * 3 + 2]))
  if ttl > 0 then
    redis.call('EXPIRE', KEYS[i], ttl)
  elseif redis.call('TTL', KEYS[i]) < 0 then
    redis.call('EXPIRE', KEYS[i], -ttl)
  end
end
return {0}
"""

# KEYS: token counters, then concurrency counters. ARGV[1]: token delta, ARGV[2]: how many
# of KEYS are token counters. Keys that expired meanwhile (a day rolled over) are skipped.
_SETTLE = """
local token_keys = tonumber(ARGV[2])
for i = 1, #KEYS do
  if redis.call('EXISTS', KEYS[i]) == 1 then
    if i <= token_keys then
      redis.call('INCRBY', KEYS[i], tonumber(ARGV[1]))
    else
      local value = redis.call('DECR', KEYS[i])
      if value < 0 then redis.call('INCRBY', KEYS[i], -value) end
    end
  end
end
return 0
"""


@dataclass(frozen=True, slots=True)
class Counter:
    key: str
    limit: int
    amount: int
    ttl: int
    message: str
    kind: Literal["window", "tokens", "concurrency"]
    retry_after: int  # seconds until the window ends (or a short wait for a free slot)


@dataclass(frozen=True, slots=True)
class QuotaTicket:
    backend: Literal["redis", "memory"]
    token_keys: tuple[str, ...]
    concurrency_keys: tuple[str, ...]
    reserved_tokens: int


@dataclass(frozen=True, slots=True)
class Limits:
    org_requests_per_minute: int
    org_requests_per_day: int
    org_tokens_per_day: int
    org_concurrent: int
    user_requests_per_minute: int
    user_requests_per_day: int
    user_tokens_per_day: int
    user_concurrent: int

    @classmethod
    def resolve(cls, settings: RuntimeSettings, policy: OrganizationPolicy) -> "Limits":
        def pick(override: int | None, default: int) -> int:
            return default if override is None else override  # 0 is a real limit

        return cls(
            org_requests_per_minute=pick(
                policy.requests_per_minute, settings.ai_org_requests_per_minute
            ),
            org_requests_per_day=pick(policy.requests_per_day, settings.ai_org_requests_per_day),
            org_tokens_per_day=pick(policy.tokens_per_day, settings.ai_org_tokens_per_day),
            org_concurrent=pick(policy.concurrent_requests, settings.ai_org_concurrent_requests),
            user_requests_per_minute=settings.ai_user_requests_per_minute,
            user_requests_per_day=settings.ai_user_requests_per_day,
            user_tokens_per_day=settings.ai_user_tokens_per_day,
            user_concurrent=settings.ai_user_concurrent_requests,
        )


def counters(
    ctx: AIRequestContext, limits: Limits, *, tokens: int, now: datetime, stream_ttl: int
) -> list[Counter]:
    minute = int(now.timestamp() // 60)
    day = now.strftime("%Y%m%d")
    to_next_minute = max(1, 60 - now.second)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    to_midnight = max(1, int((midnight - now).total_seconds()))
    org = f"org:{ctx.organization_id}"
    user = f"user:{ctx.user_id}"
    return [
        Counter(f"nexa:ai:rpm:{org}:{minute}", limits.org_requests_per_minute, 1, 120,
                "Your organization has reached its AI request limit for this minute.", "window",
                to_next_minute),
        Counter(f"nexa:ai:rpm:{user}:{minute}", limits.user_requests_per_minute, 1, 120,
                "You have reached your AI request limit for this minute.", "window",
                to_next_minute),
        Counter(f"nexa:ai:rpd:{org}:{day}", limits.org_requests_per_day, 1, 2 * 86_400,
                "Your organization has reached its AI request limit for today.", "window",
                to_midnight),
        Counter(f"nexa:ai:rpd:{user}:{day}", limits.user_requests_per_day, 1, 2 * 86_400,
                "You have reached your AI request limit for today.", "window", to_midnight),
        Counter(f"nexa:ai:tpd:{org}:{day}", limits.org_tokens_per_day, tokens, 2 * 86_400,
                "Your organization has used its AI token allowance for today.", "tokens",
                to_midnight),
        Counter(f"nexa:ai:tpd:{user}:{day}", limits.user_tokens_per_day, tokens, 2 * 86_400,
                "You have used your AI token allowance for today.", "tokens", to_midnight),
        Counter(f"nexa:ai:inflight:{org}", limits.org_concurrent, 1, -stream_ttl,
                "Your organization has too many AI requests in progress.", "concurrency",
                _CONCURRENCY_RETRY_AFTER),
        Counter(f"nexa:ai:inflight:{user}", limits.user_concurrent, 1, -stream_ttl,
                "You have too many AI requests in progress. Wait for one to finish.",
                "concurrency", _CONCURRENCY_RETRY_AFTER),
    ]  # fmt: skip


class _MemoryCounters:
    """The degraded-mode store: one process's share of every limit."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._values: dict[str, tuple[int, float]] = {}
        self._clock = clock

    def _get(self, key: str) -> int:
        entry = self._values.get(key)
        if entry is None or entry[1] <= self._clock():
            self._values.pop(key, None)
            return 0
        return entry[0]

    def reserve(self, items: list[Counter], share: Callable[[int], int]) -> Counter | None:
        for item in items:
            if self._get(item.key) + item.amount > share(item.limit):
                return item
        now = self._clock()
        for item in items:
            existing = self._values.get(item.key)
            if item.ttl < 0 and existing is not None and existing[1] > now:
                expires = existing[1]  # in-flight slots keep their first expiry
            else:
                expires = now + abs(item.ttl)
            self._values[item.key] = (self._get(item.key) + item.amount, expires)
        if len(self._values) > 50_000:
            for key in [k for k, (_, exp) in self._values.items() if exp <= now]:
                del self._values[key]
        return None

    def settle(self, ticket: QuotaTicket, delta: int) -> None:
        for key in ticket.token_keys:
            if self._get(key):
                value, expires = self._values[key]
                self._values[key] = (max(0, value + delta), expires)
        for key in ticket.concurrency_keys:
            if self._get(key):
                value, expires = self._values[key]
                self._values[key] = (max(0, value - 1), expires)


class RedisQuota:
    def __init__(
        self,
        redis: Redis,
        settings: RuntimeSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._redis = redis
        self._settings = settings
        self._clock = clock
        self._now = now
        self._memory = _MemoryCounters(clock)
        self._down_until = 0.0
        self._reserve_script: Any = redis.register_script(_RESERVE)
        self._settle_script: Any = redis.register_script(_SETTLE)

    @property
    def degraded(self) -> bool:
        return self._clock() < self._down_until

    async def reserve(
        self, ctx: AIRequestContext, policy: OrganizationPolicy, *, tokens: int
    ) -> QuotaTicket:
        limits = Limits.resolve(self._settings, policy)
        stream_ttl = int(self._settings.ai_timeout_total) + 60
        items = counters(ctx, limits, tokens=tokens, now=self._now(), stream_ttl=stream_ttl)
        token_keys = tuple(c.key for c in items if c.kind == "tokens")
        concurrency_keys = tuple(c.key for c in items if c.kind == "concurrency")

        if not self.degraded:
            try:
                args: list[int] = []
                for item in items:
                    args += [item.limit, item.amount, item.ttl]
                result = await self._reserve_script(keys=[c.key for c in items], args=args)
            except Exception as exc:
                self._trip(exc)
            else:
                index = int(result[0])
                if index:
                    self._refuse(items[index - 1])
                return QuotaTicket("redis", token_keys, concurrency_keys, tokens)

        breached = self._memory.reserve(items, self._share)
        if breached is not None:
            self._refuse(breached)
        return QuotaTicket("memory", token_keys, concurrency_keys, tokens)

    async def settle(self, ticket: object, *, tokens_used: int) -> None:
        if not isinstance(ticket, QuotaTicket):
            return
        delta = tokens_used - ticket.reserved_tokens
        if ticket.backend == "memory":
            self._memory.settle(ticket, delta)
            return
        try:
            await self._settle_script(
                keys=[*ticket.token_keys, *ticket.concurrency_keys],
                args=[delta, len(ticket.token_keys)],
            )
        except Exception as exc:
            # The day counters keep the reservation (an over-count, never an under-count);
            # the concurrency slot expires with its TTL.
            self._trip(exc)

    def _share(self, limit: int) -> int:
        return max(1, math.ceil(limit / max(1, self._settings.ai_quota_process_count)))

    def _trip(self, exc: Exception) -> None:
        if not self.degraded:
            log.warning("ai.quota.degraded", error=type(exc).__name__)
        self._down_until = self._clock() + _BREAKER_SECONDS

    def _refuse(self, counter: Counter) -> None:
        retry_after = counter.retry_after
        limit_name = "_".join(counter.key.split(":")[2:4])  # e.g. "tpd_org"
        log.warning("ai.quota.exceeded", limit=limit_name, kind=counter.kind)
        raise AIQuotaExceeded(counter.message, retry_after_seconds=retry_after)

"""Fixed-window rate limits for Runtime's own endpoints (AI calls have their own quotas).

Counters live in Redis, keyed by limit name, subject and window. When Redis is unreachable,
each process counts in memory instead, so a limit still holds per process; a short breaker
keeps requests from each paying a connection timeout.
"""

import time
from collections.abc import Callable

from redis.asyncio import Redis

from nexa_common.errors import RateLimited
from nexa_common.logs import get_logger

log = get_logger("nexa.runtime.ratelimit")

_BREAKER_SECONDS = 5.0


class Limiter:
    def __init__(self, redis: Redis | None, *, clock: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._clock = clock
        self._memory: dict[str, tuple[int, float]] = {}
        self._down_until = 0.0

    async def hit(self, name: str, subject: str, *, limit: int, window_seconds: int) -> None:
        """Count one event; raise RateLimited once `limit` is exceeded in the window."""
        now = self._clock()
        window = int(now // window_seconds)
        retry_after = max(1, int((window + 1) * window_seconds - now))
        key = f"nexa:rl:runtime:{name}:{subject}:{window}"
        count = await self._incr(key, window_seconds, now)
        if count > limit:
            log.warning("ratelimit.exceeded", limit=name)
            raise RateLimited(retry_after)

    async def _incr(self, key: str, window_seconds: int, now: float) -> int:
        if self._redis is not None and time.monotonic() >= self._down_until:
            try:
                pipe = self._redis.pipeline()
                pipe.incr(key)
                pipe.expire(key, window_seconds + 1)
                count, _ = await pipe.execute()
                return int(count)
            except Exception as exc:
                if time.monotonic() >= self._down_until:
                    log.warning("ratelimit.redis_unavailable", error=type(exc).__name__)
                self._down_until = time.monotonic() + _BREAKER_SECONDS
        value, expires = self._memory.get(key, (0, now + window_seconds))
        if expires <= now:
            value, expires = 0, now + window_seconds
        self._memory[key] = (value + 1, expires)
        if len(self._memory) > 50_000:
            self._memory = {k: v for k, v in self._memory.items() if v[1] > now}
        return value + 1

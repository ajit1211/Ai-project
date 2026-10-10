"""Stop requests for running generations.

A generation runs inside the HTTP request that started it, in one Runtime process. A stop
can come from that request's client going away, or from `POST /cancel/` sent to any process
(another tab, another device). The request is recorded twice: an in-process event the
generation loop checks four times a second, and a short-lived Redis key that a small
watcher task polls about once a second on behalf of the generation. Without Redis, a stop
sent to a different process is not seen; the generation still ends when its own client
disconnects.

Stopping never makes the provider stop instantly: NEXA closes the provider's stream, and
the provider bills whatever it generated before it noticed.
"""

import asyncio
import time
from uuid import UUID

from redis.asyncio import Redis

from nexa_common.logs import get_logger

log = get_logger("nexa.runtime.chat.cancel")

_KEY_TTL_SECONDS = 15 * 60
_POLL_SECONDS = 1.0
_BREAKER_SECONDS = 5.0


def _key(message_id: UUID) -> str:
    return f"nexa:chat:cancel:{message_id}"


class CancelRegistry:
    def __init__(self, redis: Redis | None) -> None:
        self._redis = redis
        self._local: dict[UUID, asyncio.Event] = {}
        self._watchers: dict[UUID, asyncio.Task[None]] = {}
        self._down_until = 0.0

    def start(self, message_id: UUID) -> asyncio.Event:
        """Register a running generation; the returned event is set when it should stop."""
        event = asyncio.Event()
        self._local[message_id] = event
        if self._redis is not None:
            self._watchers[message_id] = asyncio.get_running_loop().create_task(
                self._watch(message_id, event)
            )
        return event

    def finish(self, message_id: UUID) -> None:
        self._local.pop(message_id, None)
        watcher = self._watchers.pop(message_id, None)
        if watcher is not None:
            watcher.cancel()

    async def request(self, message_id: UUID) -> None:
        """Ask every process to stop generating `message_id`."""
        local = self._local.get(message_id)
        if local is not None:
            local.set()
        if self._redis is None or time.monotonic() < self._down_until:
            return
        try:
            await self._redis.set(_key(message_id), "1", ex=_KEY_TTL_SECONDS)
        except Exception as exc:
            self._trip(exc)

    async def _watch(self, message_id: UUID, event: asyncio.Event) -> None:
        assert self._redis is not None  # noqa: S101 - only started with Redis configured
        while not event.is_set():
            await asyncio.sleep(_POLL_SECONDS)
            if time.monotonic() < self._down_until:
                continue
            try:
                if await self._redis.exists(_key(message_id)):
                    event.set()
                    return
            except Exception as exc:
                self._trip(exc)

    def _trip(self, exc: Exception) -> None:
        if time.monotonic() >= self._down_until:
            log.warning("chat.cancel.redis_unavailable", error=type(exc).__name__)
        self._down_until = time.monotonic() + _BREAKER_SECONDS

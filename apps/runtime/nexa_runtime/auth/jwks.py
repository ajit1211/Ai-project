"""Core's public signing keys, fetched from `/.well-known/jwks.json` and cached.

Keys are refreshed every 10 minutes, and at once (rate-limited) when a token names a key
this process has not seen, which is how a key rotation is picked up without a restart.
"""

import asyncio
import time

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from nexa_common.errors import ServiceUnavailable
from nexa_common.jwt import keys_from_jwks
from nexa_common.logs import get_logger

log = get_logger("nexa.runtime.jwks")

REFRESH_SECONDS = 600
MIN_REFETCH_SECONDS = 30


class JWKSClient:
    def __init__(
        self, url: str, *, timeout: float = 3.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._transport = transport
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def keys(self) -> dict[str, Ed25519PublicKey]:
        return self._keys

    async def refresh(self, *, force: bool = False) -> None:
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            if not force and self._keys and age < REFRESH_SECONDS:
                return
            if force and age < MIN_REFETCH_SECONDS and self._keys:
                return  # an unknown kid is not a reason to hammer Core
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    response = await client.get(self._url)
                    response.raise_for_status()
                keys = keys_from_jwks(response.json())
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("jwks.fetch_failed", url=self._url, error=type(exc).__name__)
                if not self._keys:
                    raise ServiceUnavailable("Authentication is temporarily unavailable.") from exc
                return
            self._keys = keys
            self._fetched_at = time.monotonic()
            log.info("jwks.refreshed", key_count=len(keys))

    async def keys_for(self, kid: str | None) -> dict[str, Ed25519PublicKey]:
        """The current keys, refetched first if `kid` is unknown."""
        await self.refresh()
        if kid is not None and kid not in self._keys:
            await self.refresh(force=True)
        return self._keys

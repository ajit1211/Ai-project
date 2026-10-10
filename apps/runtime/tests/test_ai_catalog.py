"""The registry cache backs off from a failing database instead of queueing on it."""

from typing import Any

import pytest

from nexa_ai.errors import AIProviderError
from nexa_ai.registry import Registry
from nexa_runtime.ai.catalog import DatabaseCatalog

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class DownEngine:
    def __init__(self) -> None:
        self.attempts = 0

    def connect(self) -> Any:
        self.attempts += 1
        raise OSError("database down")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_stale_registry_is_served_without_hammering_the_database() -> None:
    clock, engine = Clock(), DownEngine()
    catalog = DatabaseCatalog(engine, ttl=30, max_stale=300, clock=clock)  # type: ignore[arg-type]
    snapshot = Registry()
    catalog._registry, catalog._loaded_at = snapshot, clock.now

    clock.now += 31  # stale
    for _ in range(20):
        assert await catalog.registry() is snapshot
    assert engine.attempts == 1  # one try, then a back-off window

    clock.now += 6  # back-off over: one more try
    assert await catalog.registry() is snapshot
    assert engine.attempts == 2

    clock.now += 300  # too old to serve
    with pytest.raises(AIProviderError):
        await catalog.registry()


async def test_policy_lookups_back_off_too() -> None:
    from uuid import uuid4

    clock, engine = Clock(), DownEngine()
    catalog = DatabaseCatalog(engine, ttl=30, max_stale=300, clock=clock)  # type: ignore[arg-type]
    for _ in range(10):
        with pytest.raises(AIProviderError):
            await catalog.policy(uuid4())
    assert engine.attempts == 1

"""Fixtures for the gateway: a static catalog of mock models and a gateway that never sleeps."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from nexa_ai.context import AIRequestContext
from nexa_ai.gateway import Gateway, MemoryUsageSink
from nexa_ai.providers.base import Timeouts
from nexa_ai.providers.mock import MockProvider
from nexa_ai.retry import RetryPolicy
from nexa_ai.testing import StaticCatalog, make_registry, no_sleep

GatewayFactory = Callable[..., Gateway]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def ctx() -> AIRequestContext:
    return AIRequestContext(
        request_id="req_test",
        organization_id=uuid4(),
        user_id=uuid4(),
        permissions=frozenset({"ai.chat"}),
    )


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider(sleep=no_sleep)


@pytest.fixture
def usage() -> MemoryUsageSink:
    return MemoryUsageSink()


@pytest.fixture
def make_gateway(provider: MockProvider, usage: MemoryUsageSink) -> GatewayFactory:
    def factory(**overrides: Any) -> Gateway:
        options: dict[str, Any] = {
            "providers": {"mock": provider},
            "catalog": StaticCatalog(make_registry()),
            "usage": usage,
            "retry": RetryPolicy(max_retries=2, base_delay=0.0, max_delay=0.0),
            "timeouts": Timeouts(),
            "sleep": no_sleep,
        }
        options.update(overrides)
        return Gateway(**options)

    return factory

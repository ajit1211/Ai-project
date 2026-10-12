"""AI quotas against an in-memory Redis (with Lua), and the degraded mode without one."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import fakeredis
import pytest
from redis.asyncio import Redis

from nexa_ai.context import AIRequestContext
from nexa_ai.errors import AIQuotaExceeded
from nexa_ai.registry import OrganizationPolicy
from nexa_runtime.ai.quota import RedisQuota
from nexa_runtime.config import RuntimeSettings

pytestmark = pytest.mark.anyio

SETTINGS = RuntimeSettings(
    nexa_env="test",
    runtime_database_url="postgresql://nobody:nothing@127.0.0.1:1/none",
    ai_org_requests_per_minute=3,
    ai_org_requests_per_day=100,
    ai_org_tokens_per_day=1_000,
    ai_org_concurrent_requests=2,
    ai_user_requests_per_minute=2,
    ai_user_requests_per_day=50,
    ai_user_tokens_per_day=800,
    ai_user_concurrent_requests=1,
    ai_quota_process_count=2,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.wall = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.now

    def utc(self) -> datetime:
        return self.wall


def ctx(organization_id: object = None, user_id: object = None) -> AIRequestContext:
    return AIRequestContext(
        request_id=None,
        organization_id=organization_id or uuid4(),  # type: ignore[arg-type]
        user_id=user_id or uuid4(),  # type: ignore[arg-type]
        permissions=frozenset({"ai.chat"}),
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def quota(clock: Clock) -> RedisQuota:
    redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
    return RedisQuota(redis, SETTINGS, clock=clock.monotonic, now=clock.utc)


class TestRedisQuota:
    async def test_reserve_then_settle(self, quota: RedisQuota) -> None:
        org, user = uuid4(), uuid4()
        policy = OrganizationPolicy()
        ticket = await quota.reserve(ctx(org, user), policy, tokens=600)
        assert ticket.backend == "redis"
        # 600 of the user's 800 are held: a second 600 does not fit until settled.
        with pytest.raises(AIQuotaExceeded) as caught:
            await quota.reserve(ctx(org, uuid4()), policy, tokens=600)  # org: 600 + 600 > 1000
        assert "organization" in caught.value.message
        await quota.settle(ticket, tokens_used=100)
        # Now 100 are charged; 600 more fit the org (700) and a fresh user (600 <= 800).
        await quota.reserve(ctx(org, uuid4()), policy, tokens=600)

    async def test_user_limits_follow_the_user_across_organizations(
        self, quota: RedisQuota
    ) -> None:
        user = uuid4()
        policy = OrganizationPolicy()
        first = await quota.reserve(ctx(uuid4(), user), policy, tokens=500)
        await quota.settle(first, tokens_used=500)
        with pytest.raises(AIQuotaExceeded, match="your AI token allowance"):
            await quota.reserve(ctx(uuid4(), user), policy, tokens=500)  # 500 + 500 > 800

    async def test_requests_per_minute_and_retry_after(
        self, quota: RedisQuota, clock: Clock
    ) -> None:
        org = uuid4()
        policy = OrganizationPolicy()
        for _ in range(3):
            ticket = await quota.reserve(ctx(org), policy, tokens=1)
            await quota.settle(ticket, tokens_used=1)
        with pytest.raises(AIQuotaExceeded) as caught:
            await quota.reserve(ctx(org), policy, tokens=1)
        assert "this minute" in caught.value.message
        assert 1 <= int(caught.value.headers["Retry-After"]) <= 120
        clock.wall = clock.wall.replace(minute=1)  # next window
        await quota.reserve(ctx(org), policy, tokens=1)

    async def test_concurrency_is_released_on_settle(self, quota: RedisQuota) -> None:
        user = uuid4()
        policy = OrganizationPolicy()
        ticket = await quota.reserve(ctx(uuid4(), user), policy, tokens=1)
        with pytest.raises(AIQuotaExceeded, match="in progress"):
            await quota.reserve(ctx(uuid4(), user), policy, tokens=1)
        await quota.settle(ticket, tokens_used=1)
        await quota.reserve(ctx(uuid4(), user), policy, tokens=1)

    async def test_background_calls_spend_tokens_but_never_request_slots(
        self, quota: RedisQuota
    ) -> None:
        org, user = uuid4(), uuid4()
        policy = OrganizationPolicy()
        background = replace(ctx(org, user), source_type="memory")
        # Far more background calls than the per-minute (2) and in-flight (1) limits allow,
        # none of them settled: they hold no request or concurrency slot.
        for _ in range(5):
            await quota.reserve(background, policy, tokens=10)
        # The user's own request still gets through...
        own = await quota.reserve(ctx(org, user), policy, tokens=10)
        await quota.settle(own, tokens_used=10)
        # ...while the token allowance (800 for the user) binds background work too.
        with pytest.raises(AIQuotaExceeded, match="token allowance"):
            await quota.reserve(replace(background, source_type="summary"), policy, tokens=800)

    async def test_organization_policy_overrides_defaults(self, quota: RedisQuota) -> None:
        org = uuid4()
        generous = OrganizationPolicy(tokens_per_day=5_000)
        for _ in range(2):
            ticket = await quota.reserve(ctx(org), generous, tokens=700)
            await quota.settle(ticket, tokens_used=700)
        # 1,400 used: over the 1,000 default, within the override.
        await quota.reserve(ctx(org), generous, tokens=700)

    async def test_nothing_is_counted_when_one_limit_refuses(self, quota: RedisQuota) -> None:
        org, user = uuid4(), uuid4()
        policy = OrganizationPolicy()
        with pytest.raises(AIQuotaExceeded):
            await quota.reserve(ctx(org, user), policy, tokens=5_000)  # over every token limit
        # The request counters were not touched: three requests still fit this minute.
        for _ in range(3):
            ticket = await quota.reserve(ctx(org, user if _ == 0 else uuid4()), policy, tokens=1)
            await quota.settle(ticket, tokens_used=1)


class TestDegradedMode:
    async def test_limits_are_shared_per_process_when_redis_is_down(self, clock: Clock) -> None:
        unreachable = RedisQuota(
            Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.1, socket_timeout=0.1),
            SETTINGS,
            clock=clock.monotonic,
            now=clock.utc,
        )
        org = uuid4()
        policy = OrganizationPolicy()
        # Org tokens/day 1,000 split across 2 processes: 500 each.
        ticket = await unreachable.reserve(ctx(org), policy, tokens=400)
        assert ticket.backend == "memory"
        assert unreachable.degraded
        with pytest.raises(AIQuotaExceeded):
            await unreachable.reserve(ctx(org), policy, tokens=400)
        await unreachable.settle(ticket, tokens_used=50)
        await unreachable.reserve(ctx(org), policy, tokens=400)  # 50 + 400 fits 500

    async def test_breaker_resets_after_a_few_seconds(self, clock: Clock) -> None:
        unreachable = RedisQuota(
            Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.1, socket_timeout=0.1),
            SETTINGS,
            clock=clock.monotonic,
            now=clock.utc,
        )
        await unreachable.reserve(ctx(), OrganizationPolicy(), tokens=1)
        assert unreachable.degraded
        clock.now += 10
        assert not unreachable.degraded


class TestReviewFixes:
    async def test_retry_after_is_the_time_to_the_window_end(
        self, quota: RedisQuota, clock: Clock
    ) -> None:
        org = uuid4()
        policy = OrganizationPolicy(requests_per_day=1)
        ticket = await quota.reserve(ctx(org), policy, tokens=1)
        await quota.settle(ticket, tokens_used=1)
        with pytest.raises(AIQuotaExceeded) as caught:
            await quota.reserve(ctx(org), policy, tokens=1)
        # The clock says 12:00 UTC: twelve hours to midnight, not the key's two-day TTL.
        assert caught.value.headers["Retry-After"] == str(12 * 3600)

        clock.wall = clock.wall.replace(second=45)
        busy = uuid4()
        for _ in range(3):
            done = await quota.reserve(ctx(busy), OrganizationPolicy(), tokens=1)
            await quota.settle(done, tokens_used=1)
        with pytest.raises(AIQuotaExceeded) as caught:
            await quota.reserve(ctx(busy), OrganizationPolicy(), tokens=1)
        assert caught.value.headers["Retry-After"] == "15"

    async def test_in_flight_slots_keep_their_first_expiry(self, quota: RedisQuota) -> None:
        org = uuid4()
        redis = quota._redis
        first = await quota.reserve(ctx(org), OrganizationPolicy(), tokens=1)
        key = f"nexa:ai:inflight:org:{org}"
        await redis.expire(key, 30)  # as if the slot had been held for a while
        second = await quota.reserve(ctx(org), OrganizationPolicy(), tokens=1)
        assert await redis.ttl(key) <= 30  # not pushed back out by the new request
        await quota.settle(first, tokens_used=1)
        await quota.settle(second, tokens_used=1)

    async def test_zero_is_a_real_limit(self, quota: RedisQuota) -> None:
        with pytest.raises(AIQuotaExceeded):
            await quota.reserve(ctx(), OrganizationPolicy(requests_per_minute=0), tokens=1)

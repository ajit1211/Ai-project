"""Appends gateway usage records to Core's `ai_usage_events` ledger."""

from dataclasses import asdict

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_ai.gateway import UsageRecord
from nexa_runtime.ai import tables as t


class DatabaseUsageSink:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, usage: UsageRecord) -> None:
        values = asdict(usage)
        for column in ("request_id", "source_type", "error_code"):
            values[column] = values[column] or ""
        async with self._engine.begin() as conn:
            await conn.execute(insert(t.ai_usage_events).values(**values))

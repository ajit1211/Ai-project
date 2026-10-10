"""Server-Sent Events encoding for gateway streams (docs/architecture/ai-gateway.md#streaming).

    event: message.delta
    id: 3
    data: {"text":"Hello"}

Keep-alives are SSE comments (`: ping`), which every client ignores and every proxy counts
as traffic. Only NEXA's event shapes are written; nothing provider-specific reaches the client.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from nexa_ai.gateway import KeepAlive
from nexa_ai.types import UsageEvent

SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    # Tell Nginx (and compatible proxies) not to buffer the stream.
    "X-Accel-Buffering": "no",
}
PING = b": ping\n\n"


def event_payload(event: BaseModel) -> dict[str, Any]:
    if isinstance(event, UsageEvent):
        return event.usage.model_dump()
    return event.model_dump(exclude={"type"}, mode="json")


def encode_event(event: BaseModel, event_id: int) -> bytes:
    """Any event model with a `type` field; the chat engine reuses this for its own events."""
    name = getattr(event, "type", "message")
    data = json.dumps(event_payload(event), separators=(",", ":"), ensure_ascii=False)
    return f"event: {name}\nid: {event_id}\ndata: {data}\n\n".encode()


async def sse_bytes(events: AsyncIterator[BaseModel | KeepAlive]) -> AsyncIterator[bytes]:
    """Closing this iterator closes `events` too (an async `for` does not), so the gateway
    records a disconnected stream at once rather than whenever it is garbage-collected."""
    event_id = 0
    try:
        async for event in events:
            if isinstance(event, KeepAlive):
                yield PING
                continue
            event_id += 1
            yield encode_event(event, event_id)
    finally:
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()

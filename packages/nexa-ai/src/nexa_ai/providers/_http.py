"""Small helpers shared by the HTTP-based adapters."""

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx2

from nexa_ai.providers.base import ProviderCall


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """`retry-after-ms` (OpenAI, Anthropic), then `retry-after` in seconds or as an HTTP date."""
    milliseconds = headers.get("retry-after-ms")
    if milliseconds:
        try:
            return max(0.0, float(milliseconds) / 1000)
        except ValueError:
            pass
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def request_timeout(call: ProviderCall) -> httpx2.Timeout:
    """A non-streaming call: the gateway bounds it end to end; the SDK bounds each phase."""
    return httpx2.Timeout(call.timeouts.request, connect=call.timeouts.connect)


def stream_timeout(call: ProviderCall) -> httpx2.Timeout:
    """A streaming call: reads may legitimately wait up to the first-token timeout. The gateway
    enforces the precise first-token, idle and total limits; this is only a backstop."""
    read = max(call.timeouts.first_token, call.timeouts.idle) + 5.0
    return httpx2.Timeout(call.timeouts.request, connect=call.timeouts.connect, read=read)

"""`current_time`: the server's clock, in UTC and optionally in one IANA time zone.

The clock is injected (`make_clock_tool(now=...)`), so tests are deterministic; nothing is
ever made up. An unknown time zone is an error, not a silent fallback to UTC.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nexa_runtime.tools.registry import ToolContext, ToolDefinition, ToolFailure, ToolOutput

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def make_clock_tool(now: Clock = _utc_now) -> ToolDefinition:
    async def current_time(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
        moment = now().astimezone(UTC).replace(microsecond=0)
        data: dict[str, Any] = {
            "utc": moment.isoformat().replace("+00:00", "Z"),
            "timezone": "UTC",
            "unix_seconds": int(moment.timestamp()),
            "weekday": moment.strftime("%A"),
        }
        zone = arguments.get("timezone")
        if zone:
            try:
                local = moment.astimezone(ZoneInfo(zone))
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ToolFailure("unknown_timezone", "That time zone is not known.") from exc
            data["local"] = {
                "time": local.isoformat(),
                "timezone": zone,
                "weekday": local.strftime("%A"),
            }
        return ToolOutput(data=data)

    return ToolDefinition(
        id="current_time",
        name="Current time",
        description=(
            "The current date and time from the server's clock, in UTC, and optionally in an "
            "IANA time zone such as Europe/Berlin or Asia/Kolkata."
        ),
        version="1",
        category="time",
        input_schema={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "An IANA time zone name; leave out for UTC only.",
                }
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "utc": {"type": "string"},
                "timezone": {"const": "UTC"},
                "unix_seconds": {"type": "integer"},
                "weekday": {"type": "string"},
                "local": {
                    "type": "object",
                    "properties": {
                        "time": {"type": "string"},
                        "timezone": {"type": "string"},
                        "weekday": {"type": "string"},
                    },
                    "required": ["time", "timezone", "weekday"],
                },
            },
            "required": ["utc", "timezone", "unix_seconds", "weekday"],
        },
        handler=current_time,
        risk="low",
        timeout_seconds=1.0,
        max_input_bytes=256,
        max_output_bytes=1_024,
        describe=lambda arguments: (
            "Checking the time"
            + (f" in {arguments['timezone']}" if arguments.get("timezone") else "")
        ),
    )

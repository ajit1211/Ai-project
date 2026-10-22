"""Model arguments in, tool output out: both untrusted, both checked here
(docs/architecture/tools.md#validation).

Arguments: a JSON object (never the model's raw text), within the tool's size limit, no deeper
than `MAX_DEPTH`, valid against the tool's schema (unknown properties refused by the schema's
`additionalProperties: false`), with the schema's top-level defaults filled in. Errors name the
field and the rule, never the offending value.

Output: a JSON object valid against the tool's output schema, within its size limit. Lists are
shortened from the end to fit, and the result says it was truncated; anything that still does
not fit is a failure, never silently cut.
"""

import hashlib
import json
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry

from nexa_ai.types import ToolCallPart
from nexa_runtime.tools.registry import ToolDefinition

MAX_DEPTH = 8
_OFFLINE: Registry[Any] = Registry()
REDACTED = "[redacted]"


class ArgumentsRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OutputRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_arguments(definition: ToolDefinition, call: ToolCallPart) -> dict[str, Any]:
    """The call's arguments, validated and normalized, or ArgumentsRejected."""
    if call.raw_arguments is not None:
        raise ArgumentsRejected("invalid_arguments", "The arguments are not a JSON object.")
    arguments = call.arguments
    if len(canonical(arguments).encode()) > definition.max_input_bytes:
        raise ArgumentsRejected("arguments_too_large", "The arguments are too large.")
    if _depth(arguments) > MAX_DEPTH:
        raise ArgumentsRejected("invalid_arguments", "The arguments are nested too deeply.")
    normalized = _with_defaults(definition.input_schema, arguments)
    errors = sorted(_validator(definition.input_schema).iter_errors(normalized), key=_order)
    if errors:
        raise ArgumentsRejected(
            "invalid_arguments", "Invalid arguments: " + "; ".join(_describe(e) for e in errors[:5])
        )
    return normalized


def validate_output(definition: ToolDefinition, data: Any) -> tuple[dict[str, Any], bool]:
    """(data, truncated): valid against the output schema and within the size limit."""
    if not isinstance(data, dict):
        raise OutputRejected("invalid_output", "The tool returned something unexpected.")
    if _validator(definition.output_schema).is_valid(data) is False:
        raise OutputRejected("invalid_output", "The tool returned something unexpected.")
    bounded, truncated = _bounded(data, definition.max_output_bytes)
    if truncated and not _validator(definition.output_schema).is_valid(bounded):
        raise OutputRejected("output_too_large", "The tool's result is too large to return.")
    return bounded, truncated


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def arguments_hash(definition: ToolDefinition, arguments: dict[str, Any]) -> str:
    """Identifies one exact action: an approval is bound to it, and any change of the tool,
    its version or an argument makes a different hash."""
    payload = canonical(
        {"tool": definition.id, "version": definition.version, "arguments": arguments}
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def redacted(definition: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
    """What is stored and shown of the arguments: secrets-to-be are replaced."""
    return {
        key: REDACTED if key in definition.redact else value for key, value in arguments.items()
    }


def _with_defaults(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    filled = dict(arguments)
    for name, subschema in (schema.get("properties") or {}).items():
        if name not in filled and isinstance(subschema, dict) and "default" in subschema:
            filled[name] = deepcopy(subschema["default"])
    return filled


def _bounded(data: dict[str, Any], limit: int) -> tuple[dict[str, Any], bool]:
    """Shorten the longest top-level list until the data fits; (data, whether it was cut)."""
    if len(canonical(data).encode()) <= limit:
        return data, False
    trimmed = deepcopy(data)
    lists = [key for key, value in trimmed.items() if isinstance(value, list) and value]
    while lists and len(canonical(trimmed).encode()) > limit:
        longest = max(lists, key=lambda key: len(canonical(trimmed[key])))
        trimmed[longest].pop()
        if not trimmed[longest]:
            lists.remove(longest)
    if len(canonical(trimmed).encode()) > limit:
        raise OutputRejected("output_too_large", "The tool's result is too large to return.")
    return trimmed, True


def _validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=_OFFLINE)


def _order(error: ValidationError) -> list[str]:
    return [str(part) for part in error.path]


def _describe(error: ValidationError) -> str:
    """Where and which rule, never the value (the model may have put user text there)."""
    where = ".".join(str(part) for part in error.path) or "arguments"
    if error.validator == "required":
        missing = str(error.message).split("'")[1] if "'" in str(error.message) else "a field"
        return f"{missing} is required"
    if error.validator == "additionalProperties":
        return f"{where}: unknown property"
    return f"{where}: fails '{error.validator}'"


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_DEPTH:
        return level
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level + 1)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level + 1)
    return level

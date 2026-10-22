"""The tool contract and the registry of trusted tools (docs/architecture/tools.md#registry).

A tool is a `ToolDefinition`: a stable id, a version, a description for the model, JSON Schemas
for its arguments and its output, the permissions it needs, its risk, whether it changes
anything, its limits, and a handler. Handlers are application code, registered at start-up; a
database row can switch a tool off, never add one or change what one does.

Registration refuses anything ambiguous or unsafe: a duplicate id, an invalid or open-ended
argument schema (`additionalProperties` must be false), limits out of range, a mutating tool
that would be retried automatically, or a high-risk tool that skips approval.
"""

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from nexa_ai.errors import AIInvalidRequest
from nexa_ai.structured import check_schema
from nexa_ai.types import MAX_TOOL_DESCRIPTION_CHARS, ToolSpec
from nexa_common.access import Principal

TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
VERSION = re.compile(r"^[0-9]{1,4}(\.[0-9]{1,4}){0,2}$")
MAX_TIMEOUT_SECONDS = 60.0
MAX_IO_BYTES = 256 * 1024

Risk = Literal["low", "medium", "high"]
AuditPolicy = Literal["all", "failures"]


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A document passage a tool result came from: references only, re-authorized on read."""

    document_id: UUID
    chunk_id: UUID | None = None

    def record(self) -> dict[str, str | None]:
        return {
            "document_id": str(self.document_id),
            "chunk_id": str(self.chunk_id) if self.chunk_id else None,
        }


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Who a call runs for. Built by the executor from the authenticated principal, never from
    anything the model or the client supplied."""

    principal: Principal
    conversation_id: UUID
    message_id: UUID
    invocation_id: UUID
    request_id: str | None = None

    @property
    def organization_id(self) -> UUID:
        if self.principal.organization_id is None:  # pragma: no cover - principals have one
            raise ValueError("A tool runs inside an organization.")
        return self.principal.organization_id


@dataclass(frozen=True, slots=True)
class ToolOutput:
    data: dict[str, Any]
    sources: tuple[SourceRef, ...] = ()


class ToolFailure(Exception):
    """A handler's safe, expected failure: `code` and `message` reach the model and the user,
    so they must never contain secrets, stack traces or another tenant's data."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code[:48]
        self.message = message[:300]
        self.retryable = retryable


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolOutput]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    id: str
    name: str
    description: str
    version: str
    category: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: Handler
    # Checked when the tool is offered and again when a call runs (on top of ai.tool.execute).
    permissions: frozenset[str] = frozenset()
    # Where the tool may be used; Phase 10 offers tools in conversations only.
    scopes: frozenset[str] = frozenset({"conversation"})
    risk: Risk = "low"
    read_only: bool = True
    requires_approval: bool = False
    timeout_seconds: float = 10.0
    # Automatic re-runs after a retryable failure or a timeout; read-only tools only.
    max_retries: int = 0
    max_input_bytes: int = 4_096
    max_output_bytes: int = 16_384
    # May run at the same time as the other calls of the same model step.
    concurrency_safe: bool = True
    enabled: bool = True
    audit: AuditPolicy = "failures"
    # Argument names whose values are not stored in invocation records.
    redact: frozenset[str] = frozenset()
    # One line for the activity timeline and approvals: "Calculating 2 + 2".
    describe: Callable[[dict[str, Any]], str] | None = None
    # What an approval dialog shows: the target and the consequences. Required with approval.
    approval_details: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def spec(self) -> ToolSpec:
        """The definition the model sees: id, description and argument schema; nothing else."""
        return ToolSpec(name=self.id, description=self.description, input_schema=self.input_schema)

    def summary(self, arguments: Mapping[str, Any]) -> str:
        if self.describe is None:
            return self.name
        try:
            return self.describe(dict(arguments))[:200]
        except Exception:
            return self.name

    def allowed_for(self, principal: Principal) -> bool:
        return all(principal.has(permission) for permission in self.permissions)


@dataclass
class ToolRegistry:
    """Tool id -> definition. Built once at start-up; read-only afterwards."""

    _tools: dict[str, ToolDefinition] = field(default_factory=dict)

    def register(self, definition: ToolDefinition) -> None:
        _check(definition)
        if definition.id in self._tools:
            raise ValueError(f"Tool '{definition.id}' is already registered.")
        self._tools[definition.id] = definition

    def get(self, tool_id: str) -> ToolDefinition | None:
        return self._tools.get(tool_id)

    def all(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda d: d.id)

    def __contains__(self, tool_id: object) -> bool:
        return tool_id in self._tools


def registry_of(definitions: Iterable[ToolDefinition]) -> ToolRegistry:
    registry = ToolRegistry()
    for definition in definitions:
        registry.register(definition)
    return registry


def _check(d: ToolDefinition) -> None:
    def refuse(reason: str) -> None:
        raise ValueError(f"Tool '{d.id}': {reason}")

    if not TOOL_ID.match(d.id):
        refuse("the id must be 2-64 lowercase letters, digits or underscores.")
    if not VERSION.match(d.version):
        refuse("the version must look like 1, 1.2 or 1.2.3.")
    if not d.name.strip() or not 1 <= len(d.description) <= MAX_TOOL_DESCRIPTION_CHARS:
        refuse("a name and a description of at most 1024 characters are required.")
    for label, schema in (("input", d.input_schema), ("output", d.output_schema)):
        try:
            check_schema(schema)
        except AIInvalidRequest as exc:
            refuse(f"invalid {label} schema: {exc.message}")
    if d.input_schema.get("additionalProperties") is not False:
        refuse("the input schema must set additionalProperties to false.")
    if not 0 < d.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        refuse(f"the timeout must be between 0 and {MAX_TIMEOUT_SECONDS:g} seconds.")
    if not (0 < d.max_input_bytes <= MAX_IO_BYTES and 0 < d.max_output_bytes <= MAX_IO_BYTES):
        refuse("input and output limits must be between 1 byte and 256 KiB.")
    if not 0 <= d.max_retries <= 3:
        refuse("at most three automatic retries.")
    if not d.read_only and d.max_retries:
        refuse("a tool that changes data is never retried automatically.")
    if not d.read_only and d.concurrency_safe:
        refuse("a tool that changes data runs alone, in order.")
    if d.risk == "high" and not d.requires_approval:
        refuse("a high-risk tool needs approval.")
    if d.requires_approval and d.approval_details is None:
        refuse("a tool that needs approval must describe what is being approved.")
    if d.requires_approval and d.redact:
        # The user approves exactly what runs, and an approved call runs from its record.
        refuse("a tool that needs approval cannot redact its arguments.")
    if not inspect.iscoroutinefunction(d.handler):
        refuse("the handler must be an async function.")
    if not d.scopes:
        refuse("at least one scope is required.")

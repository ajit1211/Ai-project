"""`save_memory`: save something to the user's private memories, after they approve it.

The one built-in tool that changes data, so it is high risk: it never runs without the user's
approval of the exact text, runs alone, and is never retried automatically. It goes through
the memory engine's own `create` (the same permission, secret refusal and normalization as
saving a memory by hand), which is idempotent: the same statement saved twice is one memory.
Only the user's private scope is offered; team and organization memories stay manual.
"""

from typing import Any

from nexa_runtime.memory.schemas import CreateMemory
from nexa_runtime.memory.service import MemoryEngine, MemoryRefused
from nexa_runtime.tools.registry import ToolContext, ToolDefinition, ToolFailure, ToolOutput

MAX_CONTENT_CHARS = 500
KINDS = ["fact", "preference", "decision"]


def make_memory_tool(*, memory: MemoryEngine) -> ToolDefinition:
    async def save_memory(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
        body = CreateMemory(scope="user", kind=arguments["kind"], content=arguments["content"])
        try:
            saved = await memory.create(context.principal, body)
        except MemoryRefused as exc:
            raise ToolFailure("refused", exc.message) from exc
        return ToolOutput(
            data={
                "memory_id": str(saved.id),
                "content": saved.content,
                "kind": saved.kind,
                "status": saved.status,
            }
        )

    def details(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "Save a memory",
            "target": "Your private memories",
            "content": arguments.get("content", ""),
            "kind": arguments.get("kind", "fact"),
            "consequences": (
                "NEXA will use this in your future conversations. Only you can see it, and "
                "you can delete it at any time on the Memories page."
            ),
        }

    return ToolDefinition(
        id="save_memory",
        name="Save to memory",
        description=(
            "Save a short statement to the user's private memories so it can be used in later "
            "conversations. Only when the user clearly asks to remember something. The user "
            "must approve the exact text before it is saved."
        ),
        version="1",
        category="memory",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "minLength": 1, "maxLength": MAX_CONTENT_CHARS},
                "kind": {"type": "string", "enum": KINDS, "default": "fact"},
            },
            "required": ["content"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "content": {"type": "string"},
                "kind": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["memory_id", "content", "kind", "status"],
            "additionalProperties": False,
        },
        handler=save_memory,
        permissions=frozenset({"memory.create"}),
        risk="high",
        read_only=False,
        requires_approval=True,
        concurrency_safe=False,
        timeout_seconds=10.0,
        max_retries=0,
        max_input_bytes=2_048,
        max_output_bytes=2_048,
        audit="all",
        describe=lambda arguments: "Saving to your memories",
        approval_details=details,
    )

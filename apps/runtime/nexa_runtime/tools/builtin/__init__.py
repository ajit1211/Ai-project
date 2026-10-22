"""NEXA's built-in tools (docs/architecture/tools.md#built-in-tools).

| Tool | Risk | Changes data | Approval |
| --- | --- | --- | --- |
| `calculator` | low | no | no |
| `current_time` | low | no | no |
| `document_metadata` | low | no | no |
| `workspace_info` | low | no | no |
| `search_documents` | medium | no | no |
| `save_memory` | high | yes | yes |
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from nexa_runtime.memory.service import MemoryEngine
from nexa_runtime.rag.service import RagEngine
from nexa_runtime.tools.builtin.calculator import CALCULATOR
from nexa_runtime.tools.builtin.clock import Clock, make_clock_tool
from nexa_runtime.tools.builtin.documents import make_document_tools
from nexa_runtime.tools.builtin.memory import make_memory_tool
from nexa_runtime.tools.builtin.workspace import make_workspace_tool
from nexa_runtime.tools.registry import ToolDefinition, ToolRegistry, registry_of


def builtin_tools(
    *,
    engine: AsyncEngine,
    rag: RagEngine | None,
    memory: MemoryEngine | None,
    clock: Clock | None = None,
) -> list[ToolDefinition]:
    tools = [
        CALCULATOR,
        make_clock_tool(clock) if clock is not None else make_clock_tool(),
        *make_document_tools(engine=engine, rag=rag),
        make_workspace_tool(engine=engine),
    ]
    if memory is not None:
        tools.append(make_memory_tool(memory=memory))
    return tools


def builtin_registry(
    *,
    engine: AsyncEngine,
    rag: RagEngine | None,
    memory: MemoryEngine | None,
    clock: Clock | None = None,
) -> ToolRegistry:
    return registry_of(builtin_tools(engine=engine, rag=rag, memory=memory, clock=clock))

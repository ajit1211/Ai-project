"""The built-in AI tools as Core needs to know them (docs/architecture/agents.md#configuration).

Tools are code in Runtime (`nexa_runtime.tools`), which alone can run them. Core validates
agent configurations without calling Runtime, so the facts it needs about each built-in tool
live here: its id, name, risk, whether it changes data or needs approval, and the permissions
it requires. A Runtime test fails if the registry and this catalog ever disagree.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolInfo:
    id: str
    name: str
    description: str
    risk: str
    read_only: bool
    requires_approval: bool
    permissions: frozenset[str]


BUILTIN_TOOLS: tuple[ToolInfo, ...] = (
    ToolInfo(
        "calculator",
        "Calculator",
        "Evaluates arithmetic expressions with its own parser.",
        "low",
        True,
        False,
        frozenset(),
    ),
    ToolInfo(
        "current_time",
        "Current time",
        "The server clock in UTC, optionally in an IANA time zone.",
        "low",
        True,
        False,
        frozenset(),
    ),
    ToolInfo(
        "document_metadata",
        "Document details",
        "Lists the user's documents and their details, never their text.",
        "low",
        True,
        False,
        frozenset({"documents.view"}),
    ),
    ToolInfo(
        "search_documents",
        "Document search",
        "Searches the user's documents and returns excerpts with references.",
        "medium",
        True,
        False,
        frozenset({"documents.view"}),
    ),
    ToolInfo(
        "workspace_info",
        "Workspace",
        "The organization, the user's role and their teams.",
        "low",
        True,
        False,
        frozenset({"organization.view"}),
    ),
    ToolInfo(
        "save_memory",
        "Save to memory",
        "Saves a statement to the user's private memories, after their approval.",
        "high",
        False,
        True,
        frozenset({"memory.create"}),
    ),
)

TOOLS_BY_ID: dict[str, ToolInfo] = {tool.id: tool for tool in BUILTIN_TOOLS}
EXECUTE_PERMISSION = "ai.tool.execute"

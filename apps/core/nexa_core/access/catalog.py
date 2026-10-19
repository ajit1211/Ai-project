"""The single source of truth for permissions and system roles.

Permissions are `<domain>.<action>` strings. Views and services never check role names;
they check permissions, and roles are only bundles of permissions (ADR-022).

Permissions for features that arrive in later phases (documents, knowledge, agents,
workflows, AI) are declared now so roles can be designed once; nothing grants access to those
features until they exist. `<domain>.manage_all` lets org administrators act on every
resource in a domain, regardless of who owns it (but never outside their organization).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionSpec:
    codename: str
    description: str

    @property
    def category(self) -> str:
        return self.codename.split(".", 1)[0]


PERMISSIONS: tuple[PermissionSpec, ...] = (
    # Organization
    PermissionSpec("organization.view", "View the organization and its settings"),
    PermissionSpec("organization.update", "Rename the organization and change its settings"),
    PermissionSpec("organization.delete", "Delete the organization"),
    # Members (the spec's users.* within an organization)
    PermissionSpec("members.view", "List members and their roles"),
    PermissionSpec("members.invite", "Invite people and manage pending invitations"),
    PermissionSpec("members.update", "Change members' roles and suspend or reactivate them"),
    PermissionSpec("members.remove", "Remove members from the organization"),
    # Teams
    PermissionSpec("team.view", "View teams and their members"),
    PermissionSpec("team.create", "Create teams"),
    PermissionSpec("team.update", "Rename and describe teams"),
    PermissionSpec("team.delete", "Delete teams"),
    PermissionSpec("team.manage_members", "Add and remove team members (teams you lead)"),
    PermissionSpec("team.manage_all", "Manage every team, including teams you do not lead"),
    # Roles and audit
    PermissionSpec("roles.view", "View roles and their permissions"),
    PermissionSpec("roles.manage", "Create, change and delete custom roles"),
    PermissionSpec("audit.view", "View the organization's audit log and security events"),
    # Later phases: documents and knowledge (07-09)
    PermissionSpec("documents.view", "View documents"),
    PermissionSpec("documents.upload", "Upload documents"),
    PermissionSpec("documents.update", "Rename, categorize and share your documents"),
    PermissionSpec("documents.delete", "Delete documents"),
    PermissionSpec("documents.manage_all", "Manage every document in the organization"),
    PermissionSpec("knowledge.view", "View knowledge bases"),
    PermissionSpec("knowledge.create", "Create knowledge bases"),
    PermissionSpec("knowledge.update", "Change knowledge bases"),
    PermissionSpec("knowledge.delete", "Delete knowledge bases"),
    PermissionSpec("knowledge.manage_all", "Manage every knowledge base in the organization"),
    # Later phases: agents and workflows (11-16)
    PermissionSpec("agents.view", "View agents"),
    PermissionSpec("agents.create", "Create agents"),
    PermissionSpec("agents.update", "Change agents"),
    PermissionSpec("agents.delete", "Delete agents"),
    PermissionSpec("agents.execute", "Run agents"),
    PermissionSpec("agents.manage_all", "Manage every agent in the organization"),
    PermissionSpec("workflows.view", "View workflows"),
    PermissionSpec("workflows.create", "Create workflows"),
    PermissionSpec("workflows.update", "Change workflows"),
    PermissionSpec("workflows.execute", "Run workflows"),
    PermissionSpec("workflows.manage_all", "Manage every workflow in the organization"),
    PermissionSpec("analytics.view", "View usage and cost analytics"),
    # Conversations (05): private to their owner; nobody else in the organization sees them.
    PermissionSpec("conversation.view", "View your conversations"),
    PermissionSpec("conversation.create", "Start conversations"),
    PermissionSpec("conversation.update", "Rename and archive your conversations"),
    PermissionSpec("conversation.delete", "Delete your conversations"),
    # Memory (06): private memories are the user's alone; team and organization memories are
    # shared. `memory.manage_all` reaches shared memories only, never anyone's private ones.
    PermissionSpec("memory.view", "View your memories and the ones shared with you"),
    PermissionSpec("memory.create", "Save memories"),
    PermissionSpec("memory.update", "Edit your memories"),
    PermissionSpec("memory.delete", "Delete your memories"),
    PermissionSpec("memory.manage_all", "Manage team and organization memories saved by others"),
    # AI capabilities (04+). Agents can never exceed these (doc 14.5).
    PermissionSpec("ai.chat", "Chat with AI models"),
    PermissionSpec("ai.agent.execute", "Let AI agents act on your behalf"),
    PermissionSpec("ai.tool.execute", "Let AI use tools on your behalf"),
    PermissionSpec("ai.web_search", "Let AI search the web"),
    PermissionSpec("ai.code_execute", "Let AI run code in the sandbox"),
    PermissionSpec("ai.database_query", "Let AI query connected databases"),
)

ALL_PERMISSIONS: frozenset[str] = frozenset(p.codename for p in PERMISSIONS)

_VIEWER = frozenset(
    {
        "organization.view",
        "members.view",
        "team.view",
        "roles.view",
        "documents.view",
        "knowledge.view",
        "agents.view",
        "workflows.view",
        "conversation.view",
        "conversation.create",
        "conversation.update",
        "conversation.delete",
        "memory.view",
        "memory.create",
        "memory.update",
        "memory.delete",
        "ai.chat",
    }
)
_MEMBER = _VIEWER | {
    "team.manage_members",
    "documents.upload",
    "documents.update",
    "documents.delete",
    "knowledge.create",
    "knowledge.update",
    "agents.create",
    "agents.update",
    "agents.execute",
    "workflows.create",
    "workflows.update",
    "workflows.execute",
    "ai.agent.execute",
    "ai.tool.execute",
    "ai.web_search",
}
_MANAGER = _MEMBER | {
    "members.invite",
    "team.create",
    "team.update",
    "team.delete",
    "team.manage_all",
    "documents.delete",
    "knowledge.delete",
    "agents.delete",
    "analytics.view",
    "memory.manage_all",
    "ai.code_execute",
}
_ADMIN = ALL_PERMISSIONS - {"organization.delete"}
_OWNER = ALL_PERMISSIONS


@dataclass(frozen=True)
class RoleTemplate:
    key: str
    name: str
    description: str
    permissions: frozenset[str]


OWNER = "owner"
ADMIN = "admin"
MANAGER = "manager"
MEMBER = "member"
VIEWER = "viewer"

ROLE_TEMPLATES: tuple[RoleTemplate, ...] = (
    RoleTemplate(OWNER, "Owner", "Full control, including deleting the organization", _OWNER),
    RoleTemplate(ADMIN, "Admin", "Manages members, roles, teams and settings", _ADMIN),
    RoleTemplate(MANAGER, "Manager", "Invites people and manages teams and content", _MANAGER),
    RoleTemplate(MEMBER, "Member", "Uses NEXA and creates content", frozenset(_MEMBER)),
    RoleTemplate(VIEWER, "Viewer", "Read-only access, plus AI chat", _VIEWER),
)
ROLE_TEMPLATES_BY_KEY = {template.key: template for template in ROLE_TEMPLATES}

for _template in ROLE_TEMPLATES:
    _unknown = _template.permissions - ALL_PERMISSIONS
    if _unknown:
        raise ValueError(f"Role template {_template.key!r} uses undeclared permissions {_unknown}")

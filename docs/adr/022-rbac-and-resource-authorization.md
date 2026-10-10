# ADR-022: Roles are permission bundles; resources add a second check

Status: accepted

## Context

Role checks such as `if user.is_admin` spread through a codebase and cannot express "you
may edit knowledge bases, but only your own" or "you lead this team, not that one". NEXA
will have documents, knowledge bases, agents, conversations and workflows, each with
owners, team visibility and sharing.

## Decision

- Permissions are `<domain>.<action>` strings declared in one catalog. Roles bundle
  permissions per organization; five system roles are created for every organization and
  custom roles can be added. Code checks permissions, never role names.
- Authorization has three layers: tenant (404 if the resource is elsewhere), role ceiling
  (the permission must be held), and resource access (owner, grant, team or organization
  visibility, or the administrator's `manage_all`). The ceiling cannot be raised by ownership.
- Escalation rules: nobody grants what they do not hold, only Owners touch Owners, nobody
  edits their own membership, the last Owner is protected.
- Permissions for future features, including AI capabilities (`ai.web_search`,
  `ai.code_execute`, `ai.database_query`), are declared now so each role's intent is fixed
  once and agents can be constrained with the same vocabulary.

## Alternatives

- Django's built-in groups and permissions: global, not per organization, and model-level
  only.
- A policy engine (Casbin, OPA): more than the current rules need, and a second language to
  test. The pure-function policy can be swapped for one later without changing callers.
- Resource ACLs only, no roles: every resource would need explicit grants for every member.

## Consequences

- Each future resource maps its rows to a `ResourceRef`; no new policy code per type.
- Role changes take effect on the next request because permissions are loaded per request.

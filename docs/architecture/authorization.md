# Authorization

Every request passes three checks in order: is the resource in the caller's organization,
does the caller's role allow this kind of action, and does the caller have enough access to
this particular resource. The policy is one pure module, `nexa_common.access`, used
unchanged by Core and Runtime, and later by AI agents.

## Permissions

A permission is a string `<domain>.<action>`, declared once in
`apps/core/nexa_core/access/catalog.py` and mirrored into the `permissions` table after
every migration. Views and services check permissions; they never check role names.

| Domain | Permissions |
| --- | --- |
| organization | view, update, delete |
| members | view, invite, update, remove |
| team | view, create, update, delete, manage_members, manage_all |
| roles | view, manage |
| audit | view |
| documents (Phase 07) | view, upload, delete, manage_all |
| knowledge (Phase 08) | view, create, update, delete, manage_all |
| agents (Phase 11) | view, create, update, delete, execute, manage_all |
| workflows (Phase 16) | view, create, update, execute, manage_all |
| analytics | view |
| ai (Phase 04+) | chat, agent.execute, tool.execute, web_search, code_execute, database_query |

Permissions for later phases are declared now so roles are designed once. Nothing grants
access to a feature that does not exist yet. `<domain>.manage_all` is the administrator's
override for resource-level access (see below); it never crosses an organization boundary.

## Roles

A role is a named bundle of permissions inside one organization. Every organization is
created with the five system roles below (fixed, re-synced from the catalog after each
migration); administrators can add custom roles.

| Role | Can | Cannot |
| --- | --- | --- |
| Owner | Everything, including deleting the organization and granting Owner | — |
| Admin | Manage members, roles, teams, settings, audit, all content | Delete the organization; touch an Owner; grant Owner |
| Manager | Invite people, manage every team, delete content, see analytics, let AI run code | Change members' roles, manage roles, see the audit log |
| Member | Use NEXA: create content and agents, run them, manage teams they lead, let AI search the web | Invite, delete others' content, see analytics |
| Viewer | Read shared content and chat with AI | Create anything |

The exact permission sets are in `catalog.py`; the table above is the intent.

## The three layers

```
request ──► tenant ──► role ceiling ──► resource access ──► allowed
             │             │                  │
            404           403            403 (or 404 if invisible)
```

1. **Tenant.** The resource's `organization_id` must equal the principal's active
   organization. Otherwise the resource does not exist as far as the caller can tell: 404,
   never 403, so nothing leaks about other tenants. See [multi-tenancy.md](multi-tenancy.md).
2. **Role ceiling.** The principal's permissions must include `<resource>.<action>`.
   Ownership, grants and team membership can never raise access above this ceiling: a
   Viewer who owns a knowledge base still cannot delete it.
3. **Resource access.** The principal's access to this one resource must be at least what
   the action needs: `view` and `execute` need VIEW, `update` and `upload` need EDIT,
   `delete`, `share` and `manage_members` need MANAGE. Access comes from the highest of:
   - `<resource>.manage_all` in the role → MANAGE (administrators, inside their organization)
   - being the owner → MANAGE
   - an explicit grant to the user or one of their teams → that grant's level
   - the resource's visibility: `organization` → VIEW for every member; `team` → VIEW for
     that team's members; `private` → nothing

`authorize()` turns these into responses: a resource the caller cannot see at all is 404; one
they can see but not act on is 403.

### Example: teams

Teams are the first resource that uses all three layers. A Member who leads "Research" holds
`team.manage_members` (layer 2) and MANAGE access to Research through a lead grant
(layer 3), so they can add people to Research but not to Sales. A Manager holds
`team.manage_all` and can manage every team. A Viewer holds only `team.view`.

### Future resources

A knowledge base will carry `owner_id`, `visibility` and `team_id`, plus a grants table.
Its service builds a `ResourceRef` from those columns and calls `authorize(principal,
"update", ref)`. No new policy code is needed per resource type: only the mapping from rows
to `ResourceRef`.

## Preventing escalation

`apps/core/nexa_core/access/rules.py` applies four rules to every member, role and
invitation change, in addition to the permission check:

1. You can only hand out what you hold: a role or invitation may carry only permissions the
   actor already has. A Manager cannot invite an Admin; an Admin cannot create a role with
   `organization.delete`.
2. Only an Owner can grant, change or remove another Owner.
3. Nobody changes their own membership through these endpoints.
4. An organization never loses its last active Owner (demotion, suspension, removal and
   leaving are all refused when that would happen).

System roles cannot be edited; a custom role cannot be edited by someone with fewer
permissions than the role itself.

## Where the code lives

| Piece | Location |
| --- | --- |
| Policy (pure functions, shared) | `packages/nexa-common/src/nexa_common/access.py` |
| Permission catalog and role templates | `apps/core/nexa_core/access/catalog.py` |
| Building a `Principal` from the database (Core) | `apps/core/nexa_core/access/principal.py` |
| DRF integration (`OrganizationScopedView`, `required_permissions`) | `apps/core/nexa_core/access/drf.py` |
| Escalation rules | `apps/core/nexa_core/access/rules.py` |
| Building a `Principal` from the database (Runtime) | `apps/runtime/nexa_runtime/auth/dependencies.py` |

A view declares what it needs and nothing else:

```python
class TeamListView(OrganizationScopedView):
    required_permissions = {"GET": "team.view", "POST": "team.create"}
```

The base class verifies membership (404 otherwise) and the permission before the handler
runs; services then apply resource access and the escalation rules.

## AI agents act as their user, never above them

An agent's principal is the user's principal with permissions intersected with the agent's
own allowance (`Principal.for_agent`). A user without `ai.database_query` cannot gain it by
asking an agent; an agent configured without it cannot use it even for a user who has it.
Tools (Phase 10) will call `require(principal, "ai.web_search")` and `authorize(principal,
"view", resource)` through the same functions as everything else, so there is no second
authorization path for agents to bypass.

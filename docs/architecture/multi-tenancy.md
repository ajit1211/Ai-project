# Multi-tenancy

An organization is the tenant. Every piece of data belongs to exactly one organization,
every request acts in exactly one organization, and the database refuses rows that would
mix two.

## One model for everyone

Every account gets a personal workspace (an organization of kind `personal`) at signup, and
can belong to any number of team organizations. There is no "user-owned" data: a document
in a personal workspace and a document in a company organization are the same thing with a
different tenant. This keeps one authorization path, one set of queries and one UI
(ADR-006, ADR-021).

Personal workspaces cannot invite members or be deleted; they go with the account.

## The request flow

```
access token (org claim)
        │
        ▼
OrganizationMembership lookup: user + organization, status active, organization not deleted
        │                                  │
      found                            not found ──► 404
        │
        ▼
Principal(organization_id, role, permissions, teams)
        │
        ▼
every query filtered by organization_id (and resource checks, see authorization.md)
```

The organization ID in a URL (`/organizations/{id}/...`) or in the token's `org` claim is
only a *request*. `load_organization_context` (Core) and `load_principal` (Runtime) turn it
into a verified membership or a 404. Nothing downstream ever sees an organization the caller
is not an active member of.

## Choosing the active organization

A session remembers its active organization. Login picks the personal workspace (or the
oldest membership); `POST /api/v1/auth/switch-organization/` changes it after verifying
membership and returns an access token scoped to the new organization. The SPA's switcher
calls this and keeps the organization ID in memory with the token.

Because the active organization lives in the token, a request can never carry a mismatched
organization: the client cannot send org A's token with org B's ID, since URL-scoped views
check membership of the URL's organization independently.

## Enforcement in the database

Filtering in code is necessary but not sufficient; the schema also refuses inconsistent rows:

| Constraint | Prevents |
| --- | --- |
| `organization_memberships (organization_id, role_id) → roles (organization_id, id)` | A membership in org A with a role from org B |
| `invitations (organization_id, role_id) → roles (organization_id, id)` | An invitation granting another organization's role |
| `team_memberships (organization_id, team_id) → teams (organization_id, id)` | A team membership pointing at another organization's team |
| `team_memberships (organization_id, membership_id) → organization_memberships (organization_id, id)` | Adding a member of org B to a team in org A |
| unique `(organization, user)` on memberships | Duplicate memberships |
| unique `(organization, lower(name))` on roles and teams | Duplicate names within a tenant (names may repeat across tenants) |
| one pending invitation per `(organization, email)` | Duplicate open invitations |

These are composite foreign keys added in migration `organizations/0002`, which Django
cannot express in models. They are `NOT DEFERRABLE`, so a violation fails at once.

Row-level security (`org_id = current_setting('app.org_id')`) is planned for Phase 09, when
knowledge tables arrive, as a backstop beneath the application filters.

## What the caller can never do

- See that another organization exists: foreign IDs and random IDs both answer 404 with an
  identical body.
- Use a role, team or member from another organization, even by ID: lookups are scoped, and
  the database constraints catch anything the code misses.
- Keep access after being suspended or removed: membership is re-read on every request.
- Act in an organization after it is deleted: soft-deleted organizations are excluded from
  every membership lookup at once; their rows are purged later.

## Soft delete

| Entity | Decision | Why |
| --- | --- | --- |
| Organization | Soft (`deleted_at`), purged after a retention window | An accidental delete of a whole company's workspace must be recoverable for a while; hiding is immediate either way. |
| User | Deactivation flag (`is_active=false`, `deactivated_at`), hard deletion by a later job | Audit entries and memberships refer to the user; deactivation keeps history coherent, and the deletion pipeline (Phase 17) removes personal data on request. |
| Team | Hard | Cheap to recreate, nothing else hangs off it yet. |
| Invitation | Status transitions (`accepted`, `declined`, `revoked`, `expired`), never deleted | The status *is* the history. |
| Membership | Hard | Removal is an audited event; the audit log, not the row, is the record. |

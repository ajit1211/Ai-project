# ADR-021: Organizations are the tenant, enforced in the database

Status: accepted (extends ADR-006, personal organization per user)

## Context

Data from one customer must never be visible to another. Users belong to several
organizations. Filtering in application code is necessary but a single missed filter is a
breach, so the design needs a second, independent line of defence.

## Decision

- Every tenant-owned table carries `organization_id`. There is no user-owned data outside an
  organization: each account gets a personal workspace at signup (ADR-006).
- The active organization travels in the access token and is verified against an active
  membership on every request; URL-scoped endpoints verify membership of the URL's
  organization independently. A non-member gets 404, identical to a non-existent ID.
- Composite foreign keys `(organization_id, <child>_id) → (organization_id, id)` on
  memberships, invitations and team memberships make PostgreSQL refuse rows that mix
  organizations, whatever the code does. Uniqueness is per organization (`roles`, `teams`).
- Deleting an organization is a soft delete that hides it from every membership lookup at
  once; rows are purged later.
- Row-level security is added in Phase 09 as a further backstop for data tables.

## Alternatives

- Schema per tenant: strong isolation, but migrations and connection pooling multiply by the
  number of tenants, and users in several organizations need cross-schema queries.
- Database per tenant: the same, more so.
- Code-only filtering with a `TenantManager`: kept as the first line, but not trusted alone.

## Consequences

- Tests for every organization-scoped endpoint include a cross-tenant case expecting 404.
- Migrations that add tenant tables must add `organization_id` and, where rows reference
  other tenant rows, the composite key.

# ADR-024: An append-only audit log, with security events as a filtered view

Status: accepted

## Context

Administrators must be able to answer "who did what, when, from where", security monitoring
needs failed logins and permission changes as a stream, and later phases add AI actions
(agent runs, tool calls, document access). The record must survive the deletion of the
users and organizations it describes and must not be editable.

## Decision

- `audit_logs` is append-only: the model refuses updates and deletes, and the application
  role will lose UPDATE/DELETE grants on it. `organization_id` and `actor_id` are plain UUID
  columns, not foreign keys, so rows outlive their subjects.
- Every entry records actor type and ID, organization, action, category, severity, resource,
  IP, user agent, request ID and JSON metadata. Metadata is passed through a redactor that
  drops anything that looks like a credential.
- Actions are an enum in one module; a new event is one line. Security-relevant actions are
  tagged `category=security` with a severity and also written to the log stream at WARNING,
  so investigations see one timeline while monitoring gets a filtered stream.
- Organization admins (`audit.view`) read their organization's log through a cursor-paginated
  API; nobody reads another organization's.

## Alternatives

- Separate audit and security tables: two timelines to correlate during an incident.
- Logs only (no table): not queryable per organization, not exposable to customers, subject
  to log retention.
- Database triggers capturing every row change: complete but noisy, and without actor
  context.

## Consequences

- Retention and partitioning (monthly, Phase 18) handle volume; the table is never pruned by
  the application.
- Future AI events reuse the same model and API unchanged.

# ADR-023: One authorization policy, shared by Core, Runtime and agents

Status: accepted

## Context

Two services and, later, AI agents and tools all decide whether an action is allowed. Two
implementations of the same rules drift, and an agent path with its own checks is exactly
how an agent becomes a privilege-escalation mechanism.

## Decision

- The policy is a pure module, `nexa_common.access` (`Principal`, `ResourceRef`, `can`,
  `authorize`, `require`), with no framework or database imports. Core and Runtime load
  data and call it; they never re-implement it.
- Core views declare their needs (`OrganizationScopedView` with `required_permissions`);
  membership and permission checks run before any handler. Services apply resource-level
  checks and escalation rules.
- Runtime builds the same `Principal` through FastAPI dependencies (`CurrentUser`,
  `CurrentPrincipal`) from the verified token and the shared database.
- An agent acts with `Principal.for_agent`, whose permissions are the intersection of the
  user's and the agent's. Tools will call the same `require` and `authorize`.

## Alternatives

- Authorization inside each view: fast to write, impossible to audit, and the agent path
  would inevitably differ.
- An authorization service called over the network: a dependency on every request for rules
  that fit in a few hundred lines.

## Consequences

- Policy tests are plain unit tests with no database.
- Adding a permission is a catalog change plus a call site; adding a resource type is a
  `ResourceRef` mapping.

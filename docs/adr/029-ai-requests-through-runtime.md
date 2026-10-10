# ADR-029: AI requests are served by Runtime; Core owns the catalog, policies and ledger

Status: accepted

## Context

The Phase 04 brief asks for a clear Django / FastAPI boundary. Model calls are long-lived,
I/O-bound and streamed, which is what Runtime exists for; configuration, permissions and
records of what happened are Core's domain. The two services must not call each other
(Phase 03).

## Decision

- Runtime serves `GET /ai/models/`, `POST /ai/chat/` and `POST /ai/chat/stream/`. It
  verifies Core's tokens locally (JWKS), loads membership and permissions from the shared
  database, runs the gateway, and inserts usage rows.
- Core owns the schema and migrations for the catalog, organization AI policies and the
  usage ledger, the Django admin for them, and `GET /ai/usage/`, because reporting over the
  ledger belongs with the analytics permissions and the rollups of Phase 05.
- The database enforces the boundary: `nexa_runtime` keeps read access to Core's tables and
  gets `INSERT` on `ai_usage_events` only; a trigger makes the ledger append-only for every
  role.
- The edge routes `/api/v1/ai/usage` to Core and the rest of `/api/v1/ai/` to Runtime. This
  supersedes Phase 01's `/models` on Core: only Runtime knows which providers are
  configured in a given process, so only it can say which models are usable.

## Alternatives

- Serve everything AI from Django with async views: Django's async support is partial, the
  ORM is synchronous, and every token stream would hold a Django worker.
- Proxy from Django to FastAPI: Django in the middle of every stream, which Phase 01
  explicitly removed.
- Have Runtime POST usage to Core: a service-to-service call on the hot path, and a new
  authentication scheme between the services.

## Consequences

- No circular dependency: Runtime imports `nexa_ai` and `nexa_common`; Core imports
  `nexa_common`. Neither imports the other.
- The Vite proxy and the production edge need the two-line path rule for `/ai/usage`.
- Phase 05 conversations follow the same split: messages in Runtime, analytics in Core.

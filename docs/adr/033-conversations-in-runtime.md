# ADR-033: Conversations are served by Runtime; Core owns their schema and permissions

Status: accepted

## Context

The Phase 05 brief describes the chat path as React → Django REST API → conversation
services → FastAPI AI Gateway, with Django owning conversation management and FastAPI owning
AI execution, and asks for the communication between them to be documented. The decisions
already made point the other way: Phase 01 (section 18.2) routes `/conversations` and
`/messages` to Runtime and removed Django from the token-stream path; Phase 03 decided the
services never call each other; ADR-029 said Phase 05 conversations would follow the
"execution in Runtime, records in Core" split. The Vite proxy and the production edge already
route `/conversations` to Runtime.

## Decision

- Runtime serves every `/api/v1/conversations/*` endpoint, runs the generation lifecycle and
  writes the `conversations` and `messages` tables (a migration grants `nexa_runtime` INSERT,
  UPDATE and DELETE on exactly those two tables).
- Core owns the schema, migrations, constraints and admin of both tables, and the
  `conversation.*` permissions in its catalog.
- The services still never call each other. Authentication is Core's JWT verified by
  Runtime with Core's public keys; authorization reads Core's tables; errors and request IDs
  use the shared `nexa_common` contracts.

## Alternatives

- Django serves conversations and relays generation from FastAPI (the brief's sketch). Every
  token would pass through a synchronous Django worker for the length of the reply, Django
  would need an async stack or a thread per stream, and Django would need a new
  service-to-service credential to call Runtime. Rejected for the same reasons Phase 01 gave.
- Django serves conversation CRUD, Runtime serves only send, regenerate and cancel. Two
  services would answer under one resource path, split by HTTP method at the edge, and both
  would write the same tables. Rejected: more routing and ownership complexity for no user
  benefit.
- Runtime owns the schema too. Rejected: one migration history (Phase 01 ADR-002).

## Consequences

- One request carries a chat turn end to end with no internal hop; Django's availability
  does not affect streaming, only sign-in and refresh.
- Runtime's database role can now change two Core-defined tables; the grant is explicit and
  limited to them.
- If Django ever needs to act on conversations (exports, retention jobs), it reads the same
  tables through its ORM; the models exist for that and for the admin.

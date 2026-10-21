# Architecture Decision Records

ADR-001 to ADR-018 were made in Phase 01 and are recorded in section 31 of
[the architecture document](../architecture/NEXA-architecture.md). Later decisions get one
file each here, numbered on from there. Each phase brief asks for its own "ADR-001 to
ADR-00N"; they are filed under their NEXA numbers:

| ADR | Decision | Brief |
| --- | --- | --- |
| [019](019-custom-user-model.md) | Custom Django user model identified by email, UUIDv7 keys, profile in a separate table | 03 / ADR-001 |
| [020](020-jwt-session-architecture.md) | Short EdDSA access tokens in memory, rotating refresh tokens in an HttpOnly cookie, session checked per request | 03 / ADR-002 |
| [021](021-organization-multi-tenancy.md) | Organizations as the tenant, personal workspace for everyone, tenancy enforced in the database | 03 / ADR-003 |
| [022](022-rbac-and-resource-authorization.md) | Roles as permission bundles plus resource-level access | 03 / ADR-004 |
| [023](023-centralized-authorization.md) | One policy module shared by Core, Runtime and agents | 03 / ADR-005 |
| [024](024-audit-logging.md) | Append-only audit log with security events as a filtered view | 03 / ADR-006 |
| [025](025-redis-rate-limiting.md) | Redis fixed-window rate limits that fail open | 03 / ADR-007 |
| [026](026-ai-gateway.md) | Every model call goes through one framework-free AI Gateway | 04 / ADR-008 |
| [027](027-provider-adapters.md) | Provider adapters behind one contract, official SDKs with retries off, mock provider in development only | 04 / ADR-009 |
| [028](028-logical-model-names.md) | Logical model names resolved by a database catalog with priced, ordered targets | 04 / ADR-010 |
| [029](029-ai-requests-through-runtime.md) | AI requests served by Runtime; Core owns catalog, policies, ledger and the usage endpoint | 04 / ADR-011 |
| [030](030-streaming-over-sse.md) | Token streaming over SSE with a fixed commit point | 04 / ADR-012 |
| [031](031-usage-ledger-and-quotas.md) | Append-only usage ledger, integer cost, reserve-then-settle quotas instead of money budgets | 04 / ADR-013 |
| [032](032-fallback-retry-and-timeouts.md) | Fallback, retry and timeout strategy under one deadline | 04 / ADR-014 |
| [033](033-conversations-in-runtime.md) | Conversations served by Runtime; Core owns their schema and permissions; no service-to-service calls | 05 |
| [034](034-memory-engine.md) | Structured memory in PostgreSQL, team scope as project memory, in-process extraction | 06 |
| [035](035-document-uploads-through-core.md) | Document uploads through Core, private storage behind an interface, processing on a dedicated queue | 07 |
| [036](036-rag-in-chat-lexical-baseline.md) | Document-grounded answers inside the chat engine, lexical retrieval first, validated inline citations | 08 |
| [037](037-pgvector-hybrid-retrieval.md) | Embeddings in PostgreSQL with pgvector, hybrid retrieval by reciprocal rank fusion, keyword search as the floor | 09 |

Format: context, decision, alternatives, consequences. Status is *accepted* unless noted.

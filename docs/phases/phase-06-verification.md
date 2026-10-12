# Phase 06 verification report

Conversation memory: NEXA keeps selected statements about a user and their work, recalls the
relevant ones into each reply, summarizes long conversations, and gives the user full control.
Design: [memory-engine.md](../architecture/memory-engine.md),
[memory-security.md](../architecture/memory-security.md); API: [memories.md](../api/memories.md);
decision: [ADR-034](../adr/034-memory-engine.md).

Every number below comes from a run on 2026-10-12 against the code being committed.

## Step 1: what existed

Phases 01 to 05 were in place and green before any change: 407 Python tests, 90 web tests,
and the auth, AI and chat end-to-end scripts. Reused unchanged: the Phase 05 conversation and
message tables and chat engine, the Phase 04 gateway (structured output, metering, quotas),
Phase 03 authentication, permissions, teams and the shared access policy. Missing
dependency: a **project** entity. NEXA has none, so project memory is team memory (ADR-034);
no new service, no vector database.

## Implemented

| Brief section | Status | Where |
| --- | --- | --- |
| 3 Memory layers | ✓ | Working memory (per-turn context), episodic (`kind = episode`, one per conversation), semantic (preferences, facts, decisions), project (team scope) |
| 4 Scopes and isolation | ✓ | `conversation`, `user`, `team`, `organization`; owner always set; visibility as a SQL predicate from the token's organization and the database's team membership |
| 5 Data model | ✓ | `memories`, `memory_settings`, `conversation_summaries`, `memory_extractions` (Core migrations `memory/0001`, `0002`): scope checks, one active memory per subject, ranges, tenant-consistent composite keys, GIN full-text index, provenance (conversation, message, source type, confidence), expiry, supersession; states candidate, active, superseded, expired, rejected; delete removes the row |
| 6 Extraction | ✓ | Gateway structured output (`feature = memory`), schema validation twice, secret / sensitive / personal-data refusal, grounding, confidence, dedup by content hash, conflicts by subject, approval or automatic storage |
| 7 Privacy and consent | ✓ | Secrets never stored; sensitive data never automatically; settings for automatic extraction and saving; view, edit, delete, clear |
| 8 APIs | ✓ | `GET/POST /memories/`, `GET/PATCH/DELETE /memories/{id}/`, approve, reject, search, settings, clear |
| 9 Retrieval | ✓ | Standing preferences plus full-text relevance ranked by importance, confidence, recency and scope; limits; exclusions; path to embeddings documented |
| 10 Context assembly | ✓ | Protected instructions, then memories as data, then the summary, then recent messages, in one budget with defined priorities |
| 11 Summarization | ✓ | Triggered by message count, incremental from the previous version, versioned, idempotent, originals kept |
| 12 Updates and conflicts | ✓ | Supersede by subject with history kept; conflicts with the user's own words or another kind of statement wait for the user |
| 13 Lifecycle | ✓ | Create, recall, update, approve, reject, supersede, expire (read time and `memory.purge`), delete; background work idempotent through database claims |
| 14 Observability | ✓ | `memory.recalled`, `memory.extraction.*`, `memory.summary.*`, `memory.cleared`, extended `chat.context.built`, `memory_extractions` counts, usage rows for `memory` and `summary`; no text logged |
| 15 Frontend | ✓ | `/memories`: list, search, filters (scope, kind, status), scope and source labels, origin link, dates, confidence and status, edit, delete with confirmation, approve and reject, add by hand, automatic-memory settings, clear with confirmation |
| 16 Chat integration | ✓ | Recall before the gateway call; extraction and summaries after a completed reply in a tracked task that can neither delay nor fail it |
| 17 Security | ✓ | See memory-security.md and the tests below |
| 19 Performance | ✓ | Indexes per access path, bounded retrieval, one extraction per message, summaries only every 20 messages, no caches of private data |
| 22 Out of scope | ✓ | No RAG, vector store, agents, workflows |

## Tested

| Brief item | Tests (all run for this report) |
| --- | --- |
| Data model: valid creation, invalid scope, duplicates, expiration, superseding | `test_memory_models.py` (11), `test_memory_retention.py`, API tests for one memory per statement and supersede by key |
| Extraction: valid output, malformed output, unsupported claims, sensitive data, duplicates | `test_memory_unit.py` (judging, malformed items, grounding, secrets, personal identifiers), API: secrets, health data and emails never extracted, extraction once per message |
| Retrieval: relevance, scope, expired, superseded, tenant isolation, ranking | Search ranking, expired / superseded / rejected never recalled, colleague, administrator and other-organization isolation, team scope, recall into the context with the irrelevant memory left out |
| Context: budget, summary plus recent messages, protected instructions, boundaries | `test_memory_unit.py` (order, priorities, truncation, summary boundary, never opening with the model), API: instructions byte for byte unchanged, prompt-injection memory confined to the data block |
| API: CRUD, permissions, pagination, search, approval, deletion | `TestMemories` (14) and `MemoriesPage.test.tsx` (8), `memories.test.ts` (2) |
| Integration: chat survives extraction failure, memory affects context, summaries keep messages | `TestChatIntegration` (10), including the quota regression below |

### Results

| Suite | Count | Result |
| --- | --- | --- |
| Python: `packages/nexa-common` | 17 | pass |
| Python: `packages/nexa-ai` | 167 | pass |
| Python: `apps/core` (12 new for memory) | 137 | pass |
| Python: `apps/runtime` (62 new for memory, 1 for quotas) | 160 | pass |
| Web (Vitest; 10 new) | 100 | pass |
| End-to-end, memory (`scripts/e2e_memory.py`, new) | 52 checks | pass |
| End-to-end, chat / AI gateway / sign-in | 41 / 31 / 23 checks | pass |
| ruff, ruff format, mypy --strict (176 files), ESLint, tsc, Vite build, `makemigrations --check` | — | clean |

The end-to-end scripts ran through the Vite proxy against `pnpm dev` with the project-local
PostgreSQL, no Redis and no provider keys; the mock provider answers the memory engine's
structured requests from phrase shapes, and every policy decision is the engine's own.

## Found and fixed during the phase

| Finding | Fix |
| --- | --- |
| The live memory run hit "AI request limit" on the sixth message: extraction calls counted against the user's requests-per-minute and in-flight limits, halving how fast a user could chat | Background calls (`source_type` memory or summary, set by Runtime) count tokens only; regression tests at the quota and the chat level, both confirmed to fail without the fix |
| Storing a newer memory for a subject inserted it before retiring the old one, against the one-active-per-subject rule | The old memory is retired first, then the new one stored, then linked |
| Django's single-column foreign keys apply `ON DELETE` in Python only; Runtime deletes with SQL | Two database-level `SET NULL` keys for the source message and the successor |
| A blank decision rendered as an empty bullet in summaries | Blank items are dropped |
| The memories page had two fields named "Kind" | The filters are named "Filter by scope / kind / status" |

## Known limitations

| Limitation | Effect |
| --- | --- |
| Retrieval is lexical (full-text, English stemming) | Paraphrases are missed ("Postgres" vs "database"); semantic retrieval is the planned step |
| Grounding compares word stems | Reworded claims can pass as grounded; inferences still always need approval |
| An extraction interrupted by a process crash is not retried | That message's memories are not proposed |
| `memory.purge` is not scheduled by default | Expiry still holds at read time; tidy-up needs a schedule in operations |
| Project memory is team memory | Until NEXA has projects |
| The memories page creates private memories only | Team and organization memories through the API |
| One extra small-model call per completed reply, and one per summary | Metered, visible in usage, never counted against request limits |
| Mock-based extraction in development | Real extraction quality depends on the configured model |
| No browser-level tests; the page was not checked in a real browser | Component tests cover behaviour, not layout |

## Deferred

| Item | Phase |
| --- | --- |
| Embeddings and reranking for recall | 08 |
| Document knowledge and RAG | 07 to 09 |
| A project entity with its own membership | later |
| Agent memory (per agent, tainted-run confirmation) | 11 |
| Scheduled retention and deletion jobs in operations | 18 |

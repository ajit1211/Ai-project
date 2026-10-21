# Phase 08 verification report

Retrieval-augmented generation: authorized users ask questions about their documents in the
chat and get answers built from retrieved passages, with citations that point at those
passages. Design: [rag.md](../architecture/rag.md); API: [conversations.md](../api/conversations.md#document-grounded-answers)
and [documents.md](../api/documents.md); decision: [ADR-036](../adr/036-rag-in-chat-lexical-baseline.md).

Every number below comes from runs on 2026-10-19 to 2026-10-21 against the code being
committed.

## Step 1: what existed

Phases 01 to 07 were inspected in code, not from their reports. Reused: the chat engine
(Runtime writes conversations and messages; streaming, cancellation, retries, usage), the AI
Gateway and its mock provider (no separate LLM SDK, no second registry), the memory engine's
context builder and token estimate (three characters per token; no tokenizer exists), the
shared access policy and Runtime's direct read access to Core tables, PostgreSQL full-text
search (as in Phase 06), Celery on the Phase 07 `core.documents` queue, and the Phase 07
extracted text with its PDF page spans. Earlier-phase work this phase needed: the Phase 07
library UI, tests and documentation were finished first (see the Phase 07 report), and two
Phase 07 bugs were fixed. Missing dependencies: none; no vector store, no new service.

## Implemented

| Brief section | Status | Where |
| --- | --- | --- |
| 4 Chunking | ✓ | `documents/chunking.py`: sections, pages, block and sentence boundaries, size target and maximum, overlap where a block is split, tables and CSV rows with the header repeated, dedup, limits, exact offsets, deterministic; characters with estimated tokens |
| 5 Data model | ✓ | `document_chunks` (stable UUIDv5 ids, scope, position, text, section, pages, rows, offsets, counts, hash, strategy, weighted generated `tsvector`, GIN) and `document_indexes` (status, generation, strategy, source hash, counts, attempts, errors, timings); `messages.grounding`, `message_sources`; composite tenant keys; Runtime grant on `message_sources` |
| 5 Lifecycle | ✓ | `pending → indexing → indexed`, `failed`, `stale`; generation tokens make duplicates and superseded runs no-ops; one-transaction swap keeps the previous complete index on failure; stalled runs taken over and failed by the purge |
| 6 Indexing and retrieval | ✓ | Indexing after extraction and on request; FTS `english`; authorization inside the query; filters by documents, team or all readable; ranking by term coverage plus cover density; limits; near-duplicate removal; statement timeout |
| 7 Query pipeline | ✓ | Validate, authorize scope, retrieve, rank, budgeted evidence, rules prompt, AI Gateway (feature `rag`), citation validation, persistence with usage |
| 7 Prompt and no-evidence | ✓ | Untrusted-data rules, citation rules, abstention, general knowledge marked, contradictions surfaced; no model call when nothing is retrieved |
| 8 Citations | ✓ | Server-assigned labels, validation against supplied passages, invalid labels recorded, references stored (never copied text), re-authorized on every read, navigable to page or offset |
| 9 Chat integration | ✓ | `grounding` on the existing send endpoint; `sources` stream event; streaming, cancellation, regenerate (re-authorized), history; ordinary chat unchanged; memory extraction skipped for grounded turns |
| 10 API | ✓ | Chat send with `grounding`; `GET /conversations/{id}/messages/{mid}/sources/`; `POST /documents/{id}/index/`, `GET /documents/{id}/index-status/`; stable error codes `DOCUMENT_UNAVAILABLE`, `DOCUMENT_NOT_INDEXED`, `RAG_INVALID_SCOPE`, `RAG_RETRIEVAL_FAILED`; bounds on question, documents, sources and evidence |
| 11 React | ✓ | Search-index status, "Searchable" mark and re-index in the library and detail pages; in the chat: document picker (searchable only, "all documents"), selection chips, grounded badge, search progress, citation buttons for verified labels only, source cards with excerpts and links, uncited / no-evidence / unverified notices, error mapping |
| 12 Security | ✓ | See the table in rag.md and the tests below |
| 13 Reliability | ✓ | Idempotent indexing, timeouts, bounded evidence, cancellation, failure-safe state, indexes for every query, request ids, structured logs, usage and latency per message; no cache added |
| 14 Evaluation | ✓ | `tests/data/rag_eval.json` and `test_rag_eval.py` |
| 17 Out of scope | ✓ | No embeddings, vector search, hybrid retrieval, rerankers, agents or workflows; `Retriever` protocol ready for Phase 09 |

**API decision:** grounded questions extend the chat endpoint; there is no competing
`/rag/query/` API, and the assistant message is the persisted query record (ADR-036).

## Tested

| Brief item | Tests |
| --- | --- |
| Chunks and metadata, pages and sections, size and overlap, empty and malformed text | `test_document_chunking.py` (21), including the hand-off from real PDF and DOCX extraction |
| Idempotent indexing, re-indexing, failed and retried runs | `test_document_indexing.py` (18): duplicate, superseded, stable ids on re-index, failure keeps the previous chunks, deleted during indexing, live and dead runs, stale strategy, the search column, the index API and its authorization, rate limit, `searchable` filter, deletion |
| Retrieval, filters, limits and ordering, insufficient evidence | `test_rag_unit.py` (17) and `test_rag_chat.py` (25): terms, near-duplicates, budget, scopes (documents, team, all), no evidence, no searchable words |
| Gateway integration with mocks, structured answer validation, fabricated citations | Mock provider grounded answers; `[[cite:S99]]` recorded as invalid and never a source; `[[no_cite]]` marked uncited |
| Citation authorization, cross-user and cross-tenant isolation | Unreadable, foreign and guessed ids refused with nothing stored; library scope never returns hidden or foreign passages; sources become unavailable when a document is unshared or deleted; regenerate re-authorizes; SQL visibility equals the shared policy over a role and sharing matrix |
| Prompt injection in documents | Passages confined to the source block, block-breaking text escaped, NEXA's instructions byte-for-byte unchanged |
| RAG chat, ordinary chat, streaming, cancellation, errors | Stream and JSON paths, `sources` event order, listing, sources endpoint, history markers stripped, memory not extracted, retrieval failure, provider failure, switched off, rate limited, stop during a grounded reply |
| Frontend | 18 new tests: picker (searchable only, all documents), `grounding` sent only with a selection, `sources` event, citation buttons for known labels only, literal excerpts, unavailable sources, notices, error mapping |
| Regression of Phases 01 to 07 | Whole Python and web suites below |

### Results

| Suite | Count | Result |
| --- | --- | --- |
| Python: `packages/nexa-common` | 17 | pass |
| Python: `packages/nexa-ai` | 167 | pass |
| Python: `apps/core` (103 new in Phases 07 and 08) | 240 | pass |
| Python: `apps/runtime` (45 new) | 205 | pass |
| Python total | 629 | pass |
| Web: Vitest | 189 in 18 files (89 new in Phases 07 and 08) | pass |
| `scripts/e2e_rag.py` against the running stack (mock provider) | 31 checks | 31 pass |
| `scripts/e2e_documents.py` | 75 checks | 75 pass |
| ruff, ruff format, mypy (strict, 212 files), ESLint, `tsc --noEmit`, `vite build` | | clean |
| Migrations `documents/0003`, `chat/0003` applied; `makemigrations --check` | | no changes |

### Evaluation (synthetic set, lexical baseline, mock provider)

| Measure | Result |
| --- | --- |
| Recall@6 (expected passage supplied), 12 questions | 1.00 |
| Mean reciprocal rank | 1.00 |
| Unanswerable questions with no evidence | 3 of 3 |
| Near-duplicate pairs among supplied passages | 0 |
| Passages from another organization | 0 |
| Answers citing an expected document / invalid citations | 12 of 12 / 0 |
| Weak-evidence question (shares "leave" with the handbook) | 2 passages supplied; only the model can abstain |

These figures are regression guards, not accuracy claims: the questions use the documents'
own words, the set is tiny, and the mock provider is not a language model.

## Found and fixed during the phase

- Phase 07's `?format=` filter and purge count (see the Phase 07 report).
- Near-duplicate removal collapsed two test documents with nearly the same text; the test
  data was wrong, the behaviour is intended.

## Known limitations

- Lexical retrieval: no synonyms or paraphrases; a single shared common word can surface an
  unrelated passage (the model is told to say when the evidence does not answer).
- English stemming only; character-based sizes; estimated tokens; no reranker.
- Citations rely on the model following the `[S1]` convention; non-compliant answers are
  flagged `uncited`, not repaired.
- Stale indexes after a chunking change are rebuilt on request, not automatically.
- The web UI was tested in jsdom and the API end to end, not in a real browser; the team scope
  has no picker option yet (the API supports it).

## Phase 09 readiness

Ready. Vector search can be added as a second `Retriever` (embedding column or table per
chunk) and combined with the lexical one; chunking, chunk ids, the visibility predicate,
evidence, citations, sources and the chat integration stay as they are. No blocker found.

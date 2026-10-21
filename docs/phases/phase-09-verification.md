# Phase 09 verification report

Vector search, embeddings and hybrid retrieval: document passages are embedded in the
background and stored with pgvector; grounded questions are answered from keyword search,
vector search, or both fused, inside the same authorization; wherever the vector path is
missing or fails, answers fall back to keyword search and say so. Design:
[vector-search.md](../architecture/vector-search.md); API: [documents.md](../api/documents.md)
and [conversations.md](../api/conversations.md#retrieval-options); decision:
[ADR-037](../adr/037-pgvector-hybrid-retrieval.md).

Every number below comes from runs on 2026-10-21 against the code being committed.

## Step 1: what existed

Phases 01 to 08 were inspected in code. Reused as they are: Phase 08's chunks (stable ids,
content hashes, generations), its `Retriever` protocol and `Passage` type, the visibility
predicate written as SQL (Phase 08 tests it against the shared policy), evidence budgeting,
citations, sources and the chat integration; the AI Gateway's error types, retry policy and
usage ledger; the OpenAI SDK already used by the chat adapter; Celery on `core.documents` with
the broker-down inline fallback; the generation-token pattern of indexing.

**PostgreSQL and pgvector compatibility**:

| Environment | PostgreSQL | pgvector | Consequence |
| --- | --- | --- | --- |
| Docker Compose, CI | `pgvector/pgvector:pg17` | bundled | Full semantic search; `init.sql` creates the extension |
| Local Windows development (`scripts/dev-db.ps1`, port 5433) | 18.6 | **not installed**, no compiler to build it | Keyword search only, reported as `pgvector_missing` |
| Verification server for this report (scratch, port 5434) | 16.2 (from the `pgserver` wheel) | 0.6.2 | Every pgvector path below was run here |

No new service or dependency was needed (no vector database; the OpenAI SDK was already a
dependency).

## Implemented

| Brief area | Status | Where |
| --- | --- | --- |
| Embedding provider abstraction | ✓ | `nexa_ai.embeddings`: `EmbeddingProvider` protocol (separate from chat), OpenAI adapter (`text-embedding-3-*` with `dimensions`), deterministic mock (`hash-v2`, development and test only); `Embedder` with batching by count and tokens, retries with backoff, timeouts, validation (count, dimensions, finite, non-zero) and L2 normalization |
| Model registry | ✓ | `ai_embedding_models` (`AIEmbeddingModel`): key, provider, model, dimensions, version, distance, batch and input limits, price, relevance floor, one default; space fields immutable; seeded `nexa-embed-small` (default) and `nexa-embed-mock`; `choose_model` shared by Core and Runtime |
| Vector storage | ✓ | `chunk_embeddings` (pgvector, created only where the extension exists), `document_embeddings` (state per document and model); composite tenant foreign keys; `setup_vector_search` command; documented path without pgvector |
| Embedding pipeline | ✓ | `documents/embedding.py` + `core.documents.embed` task: after indexing, generation-guarded, idempotent upserts, vectors of unchanged chunks reused, daily organization budget, per-batch usage events, stale-vector pruning, stalled-run recovery, safe on deletion; `embed_documents` command |
| Query embedding and semantic retrieval | ✓ | `rag/vector.py`: `SemanticCatalog` (availability), `VectorRetriever` (`pgvector-cosine/v1`): authorization predicate in the SQL, active model and current generation only, relevance floor, savepoint |
| Hybrid retrieval | ✓ | `rag/fusion.py`: reciprocal rank fusion (k = 60), deduplication, deterministic ties; modes `auto`/`lexical`/`semantic`/`hybrid`; fallbacks recorded |
| Reranking | interface only | `Reranker` protocol, `NoReranker`, `rerank_safely` (timeout, reorder-only); **no reranker enabled** — none configured, no fake one |
| RAG integration | ✓ | `RagEngine.retrieve` picks methods per request; grounding records `retrieval_requested`, `retrieval_mode`, `fallback`; regeneration keeps the requested mode; citations, sources and no-evidence answers unchanged |
| API | ✓ | `index.semantic` on documents; `POST /documents/{id}/index/` `{"reembed": true}`; `GET /documents/search-status/` (admins); `grounding.retrieval_mode`; `GET /ai/retrieval/` |
| Frontend | see [Frontend](#frontend) | Semantic state in the library and detail pages, re-embed, retrieval-mode control, fallback notice |
| Security | ✓ | Tenant and visibility predicate in every vector query; no vectors or similarities in any API; dimensions only in the admin diagnostic; no text, questions or vectors in logs; mock refused in production |
| Observability and cost | ✓ | `documents.embedding.*` and `rag.retrieval.completed` (mode, fallback, candidates per method, query embedding ms); usage events (feature `embedding`) for documents and questions; reuse, budget, rate limits, `EMBEDDING_ENABLED` |
| Evaluation | ✓ | `tests/data/rag_eval_v2.json`, `test_rag_eval_modes.py` |
| Indexes | ✓ | Exact search by default with a b-tree scope index; HNSW per model on request (`vector_index`) |

## Tested

| Brief item | Tests |
| --- | --- |
| Provider, batching, retries, timeouts, validation | `packages/nexa-ai/tests/test_embeddings.py` (25): mock determinism and similarity, batching by count and tokens, rate-limit and provider retries, timeout retried once, wrong count / dimensions / non-finite vectors rejected and not retried, normalization, OpenAI adapter against a mocked HTTP API (request shape, `dimensions`, order by index, error mapping), the model-selection rule |
| Pipeline: idempotent, versioned, reuse, deletion, failures | `apps/core/tests/test_document_embedding.py` (20): unit vectors and usage rows, re-index reuses every vector, changed text re-embedded and old vectors pruned, force, duplicate and superseded runs, partial failure keeps finished batches, refused request not retried, invalid vectors store nothing, deletion (before and during a run), a second model never mixed in, the daily budget, stalled runs, availability reasons, the active-model rule, immutable spaces, one default, API payload, re-embed, admin-only search status |
| Semantic and hybrid retrieval, isolation, fallbacks, API | `apps/runtime/tests/test_rag_vector.py` (16): typos found by vectors and not by keywords, similarity order, no evidence below the floor, only ready/current documents of the active model, another model's vectors never compared, other organization / private / deleted documents excluded in semantic and hybrid modes, forged ids refused before any search, fusion without repeats, deterministic order, question-embedding failure and vector-query failure falling back to keywords, `auto` without semantic search, `/ai/retrieval/`, grounded hybrid answer with citations and usage, regeneration keeps the mode, deletion |
| Fusion, reranking guard, mode choice | `apps/runtime/tests/test_rag_fusion.py` (21, no database) |
| Evaluation | `apps/runtime/tests/test_rag_eval_modes.py` (4 + 1 opt-in live): the three modes compared, hybrid answers' citations, a changed document found by its new text only in every mode, latency on a generated library |
| Phase 08 behaviour | `test_rag_chat.py`, `test_rag_eval.py` pinned to `lexical` (the Phase 08 baseline) and still passing; whole suites below |

pgvector-dependent tests skip with a reason where the extension is missing; they ran on the
pgvector server for this report and run in CI.

### Results

| Suite | Database | Count | Result |
| --- | --- | --- | --- |
| Python, all (`uv run pytest`) | PostgreSQL 16 + pgvector 0.6.2 | 705 | 704 pass, 1 skipped (live OpenAI evaluation, opt-in) |
| `test_rag_fusion.py` (added after that run; no database) | — | 21 | pass |
| Python, all | local PostgreSQL 18, no pgvector | 705 | 669 pass, 36 skipped (35 need pgvector, 1 live evaluation) |
| Web: Vitest | — | 223 in 18 files (34 new) | pass |
| `scripts/e2e_vector.py` against the running local stack (no pgvector: fallback path) | local | 20 checks | 20 pass |
| `scripts/e2e_rag.py`, `scripts/e2e_documents.py` (Phase 08 and 07, re-run) | local | 31 + 75 checks | all pass |
| ruff, ruff format, mypy (strict, 232 files) | | | clean |
| ESLint, `tsc --noEmit`, `vite build` | | | clean |
| Migrations `ai/0003`, `documents/0004`, `documents/0005` applied on both databases; `makemigrations --check` | | | no changes |

Python test counts by package: `nexa-common` 17, `nexa-ai` 192 (25 new), `apps/core` 267 (20
new), `apps/runtime` 250 (42 new).

### Evaluation (synthetic set, mock embeddings)

| Mode | recall@6 | precision@6 | MRR | nDCG@6 | Out-of-scope passages | Unanswerable with no evidence | Question embeddings |
| --- | --- | --- | --- | --- | --- | --- | --- |
| lexical | 0.87 | 0.51 | 0.87 | 0.87 | 0 | 4/4 | 0 |
| semantic | 0.67 | 0.58 | 0.67 | 0.67 | 0 | 4/4 | 20 calls, 260 tokens |
| hybrid | 0.87 | 0.51 | 0.83 | 0.84 | 0 | 4/4 | 20 calls, 260 tokens |

Hybrid grounded answers over all 19 questions: 0 invalid citations; the 4 unanswerable
questions got `no_evidence` without a model call. A document whose text changed was found
only by its new text in all three modes after re-indexing. Retrieval latency on 150 generated
documents (262 chunk vectors, exact search, laptop, including the question embedding and the
test client): p50/p95 39/50 ms lexical, 88/96 ms semantic, 134/151 ms hybrid.

**Reading these numbers.** The mock embeds hashed words and character trigrams; it is not a
semantic model. On this set it is *worse* than full-text search, and hybrid keeps lexical
recall with a slightly lower MRR (one distractor). The evaluation shows that storage,
authorization, fusion, fallbacks and the no-evidence rule work; it does **not** show that
semantic retrieval improves answers. That needs the opt-in live run with a real provider,
which was **not run** for this report (no provider key here). No accuracy improvement is
claimed.

## Frontend

- **Documents**: each document shows its semantic state (Semantic search ready, Embedding
  queued, Embedding…, Updating embeddings, Embedding failed with the server's message, Not
  embedded). Where semantic search does not run, the library shows one muted note instead of a
  badge per row, and the document page says so in one line. Documents are polled while
  embedding, through the existing index poller. The document page has **Re-embed** (or
  **Retry embedding** after a retryable failure) next to Re-index, for users who may re-index,
  disabled while embedding or before the keyword index exists.
- **Chat**: once documents are selected, a **Search by** control offers Automatic plus only the
  modes `GET /ai/retrieval/` lists; it is hidden where keyword search is the only way, or when
  the options cannot be loaded. Automatic sends no `retrieval_mode`.
- **Replies**: after a fallback, a muted note says "Searched by keywords: semantic search was
  unavailable for this answer."; otherwise a short line says how the passages were found.
  Older replies show neither.
- Never shown: vectors, dimensions, similarity values, model keys, reason codes.
- Tested in jsdom (34 new tests) and through the API end to end; not in a real browser.

## Found and fixed during the phase

- The Phase 08 tests assumed keyword retrieval and failed once hybrid became the default;
  they are pinned to `lexical`, which is the baseline they measure.
- The first mock (`hash-v1`, stop words included, floor 0.15) gave unanswerable questions
  passages through shared function words ("what", "the"); `hash-v2` drops English stop words
  and the floor is 0.2 (measured on the Phase 08 set: relevant passages ≥ 0.23, unanswerable
  questions ≤ 0.14).
- Django's test teardown could not truncate `documents` because the unmanaged
  `chunk_embeddings` table references it; the test flush now cascades.
- One question first filed as unanswerable ("capital of Mongolia") shares a word with the
  travel policy ("capital cities"); it is a weak-evidence case and is reported, not asserted,
  as in Phase 08.
- DDL cannot take bind parameters: the HNSW index's model filter is an inlined, validated UUID.

## Known limitations

- Semantic quality with a real provider is unmeasured here; relevance floors are heuristics
  chosen on the synthetic set.
- The OpenAI adapter was tested against a mocked HTTP API, not the live service.
- The local Windows database has no pgvector, so `pnpm dev` runs keyword search only; the
  pgvector paths were verified on PostgreSQL 16 + pgvector 0.6.2, CI uses pg17 with pgvector.
- No reranker; one active embedding model at a time; switching models means re-embedding.
- Exact vector search by default; HNSW is opt-in and limited to 2 000 dimensions.
- Mock token counts are estimates.

## Phase 10 readiness

Ready, no blocker. Phase 10 (tool calling) can expose document search as a tool by calling
`RagEngine.retrieve` with the user's principal; authorization, modes and fallbacks come with
it. Before relying on semantic search anywhere: install pgvector on that database
([installing pgvector](../architecture/vector-search.md#installing-pgvector)), set
`AI_OPENAI_API_KEY` in Core and Runtime, run `embed_documents`, and run the live evaluation to
check the relevance floor on real embeddings.

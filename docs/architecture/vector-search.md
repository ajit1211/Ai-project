# Vector search, embeddings and hybrid retrieval (Phase 09)

Phase 08 answers questions from documents with keyword (full-text) retrieval
([rag.md](rag.md)). Phase 09 adds a second retriever that compares meaning, as vectors, and
combines the two. Chunking, authorization, evidence, citations, sources and the chat
integration are unchanged: a vector search returns the same `Passage` objects as the keyword
search, and everything after retrieval works as before.

Decision record: [ADR-037](../adr/037-pgvector-hybrid-retrieval.md).

## Overview

```text
Core (indexing worker)                          Runtime (chat request)
  document text -> chunks (Phase 08)              question
        |                                            |
        v                                            v
  embed task: changed chunks -> provider      embed the question (same model)
        |                                            |
        v                                            v
  chunk_embeddings (pgvector)  <---- SQL ---- vector search  +  keyword search
                                                     \            /
                                                  reciprocal rank fusion
                                                          |
                                               evidence, citations (Phase 08)
```

- **Core** owns the embedding model registry, the per-document embedding state and the
  vectors; the embed task runs on the `core.documents` queue after a document is indexed.
- **Runtime** embeds the question with the same model, runs the vector query (authorization
  inside the SQL, as for keyword search), fuses the two lists and reports what ran.
- Both services choose the active model with one shared rule (`nexa_ai.embeddings.choose_model`)
  from the same rows and settings, so a question is never compared with vectors from another
  model.

## Installing pgvector

Vectors are stored with the [pgvector](https://github.com/pgvector/pgvector) extension.
Docker Compose and CI use the `pgvector/pgvector:pg17` image, which has it; on those, nothing
needs installing.

On a PostgreSQL you manage yourself:

1. Install the extension's files on the database server (packages: `postgresql-17-pgvector` on
   Debian/Ubuntu via the PGDG repository, `pgvector` in Homebrew; on Windows, build it with
   Visual Studio's `nmake` as described in the pgvector README, or use a distribution that
   bundles it). pgvector 0.5 or later is needed; HNSW indexes need 0.5+.
2. Create the extension, as a superuser (releases marked trusted can also be created by the
   database owner: `SELECT version, trusted FROM pg_available_extension_versions WHERE name =
   'vector'`; 0.6.2, used for verification here, is not):
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
   `scripts/db/init.sql` does this for new Compose databases, in `nexa` and in `template1`
   (so test databases created from it have it too).
3. Create the vector table:
   ```text
   uv run python apps/core/manage.py setup_vector_search                    # table only
   uv run python apps/core/manage.py setup_vector_search --create-extension # also the extension
   ```
   The migration `documents/0005_vector_store` creates the table when the extension is
   already installed at migrate time; `setup_vector_search` does it later, and is safe to run
   again. It prints whether semantic search will run and, if not, why.
4. Embed existing documents: `uv run python apps/core/manage.py embed_documents` (see
   [Indexing](#indexing)).

**Without pgvector** everything else works: migrations succeed (the vector table is simply not
created), documents are indexed for keyword search, document payloads show
`"semantic": {"status": "unavailable", "reason": "pgvector_missing"}`, and chat answers use
keyword retrieval. Nothing pretends otherwise: an explicit request for semantic or hybrid
retrieval is answered by keywords and records `fallback: "semantic_pgvector_missing"`.

## Embedding models

Embedding models are rows of `ai_embedding_models` (Core model `AIEmbeddingModel`),
separate from chat models:

| Key | Provider / model | Dimensions | Notes |
| --- | --- | --- | --- |
| `nexa-embed-small` | OpenAI `text-embedding-3-small` | 1536 | Default. No price recorded (set `input_micros_per_million` from your contract to get costs) |
| `nexa-embed-mock` | mock `mock-embed-hash`, version `hash-v2` | 256 | Development and test only (refused elsewhere) |

A model's **space** is `provider/provider_model@version:dimensions:distance`. Vectors are only
ever compared within one model: every stored vector carries its model id, space and dimensions,
and the vector query filters by the active model's id and dimensions. The space fields of a
row cannot be changed after creation (`save()` refuses); a new model or version is a new row,
and documents are re-embedded for it.

**Choosing the active model** (`choose_model`, the same in Core and Runtime):

1. `EMBEDDING_MODEL`, when set: that model and no other. If its provider has no credentials,
   semantic search is unavailable (`provider_not_configured`); it never silently uses another
   model.
2. Otherwise the default model, if its provider is configured (an OpenAI key is set).
3. Otherwise, only in development and test, the mock model, so a machine without keys still
   exercises the whole pipeline.
4. Otherwise semantic search is unavailable.

`EMBEDDING_ENABLED=false` turns semantic search off in that service. Set it the same in Core
and Runtime (with it off in Core no document text is sent to an embedding provider).

**The mock provider** hashes content words and their character trigrams into 256 dimensions.
It is deterministic and needs no network, so tests and local development exercise storage,
fusion, authorization and fallbacks. Its vectors are **not semantic**: they help with typos and
shared word pieces, not with synonyms or paraphrases. Production refuses it (`AI_MOCK_PROVIDER`
cannot be enabled outside development and test).

## Indexing

After a document's keyword index is (re)built, the indexing task requests an embedding run
for the active model, in the same transaction (`request_embedding`); the run is dispatched
when the transaction commits, to the `core.documents` queue (`core.documents.embed`), or inline
when the broker is down.

A run (`embedding.run_embedding`):

1. **Claims** the document's embedding state (locks: document, then index, then state — the
   order every document writer uses) and checks the run's generation: a newer request
   supersedes an older run (`superseded`), a deleted document cancels it (`cancelled`).
2. **Reuses vectors** of chunks whose text has not changed (same `content_sha256` and model);
   only new or changed chunks are sent to the provider. Re-embedding with `force` sends all.
3. **Checks the organization's daily embedding budget** (`EMBEDDING_ORG_TOKENS_PER_DAY`,
   estimated tokens). Over budget, the run fails with `quota_exceeded` (not retried).
4. **Embeds in batches** (`EMBEDDING_BATCH_SIZE`, capped by the model's `max_batch_size` and
   `EMBEDDING_MAX_BATCH_TOKENS`). The `Embedder` retries rate limits and provider errors with
   backoff (`EMBEDDING_MAX_ATTEMPTS`), applies `EMBEDDING_TIMEOUT_SECONDS` per request, and
   validates every response: one vector per input, the model's dimensions, finite numbers,
   non-zero length. Vectors are L2-normalized before storage.
5. **Stores each batch** in its own transaction, after re-checking the generation, with
   `INSERT … ON CONFLICT (chunk_id, embedding_model_id) DO UPDATE` (idempotent: a retried
   batch overwrites, never duplicates).
6. **Finalizes**: removes vectors of chunks that no longer exist, and marks the state `ready`
   only when every current chunk has a vector.

Each batch writes one AI usage event (feature `embedding`, source `document`, actor `system`,
the document owner as user) with its token count and cost, so embedding spend appears in the
usage ledger and organization reports.

**States** (`document_embeddings.status`, shown as `index.semantic` on every document):
`pending`, `embedding`, `ready`, `failed` (with `error_code`, `error_message`, `retryable`).
The API also derives `none` (never requested for this model), `stale` (the vectors belong to
an older index generation; new ones are being made) and `unavailable` (with the reason).
Vectors are only searched when the state is `ready` **and** was built from the current index
generation, so a re-indexed document is never searched with vectors of its old chunks.

**Failures**: `provider_unavailable` (rate limit, timeout, provider outage; retried by Celery,
`EMBEDDING_TASK_RETRIES` times), `provider_error` (refused request; not retried),
`quota_exceeded`, `timeout`, `no_chunks`, `embedding_failed` (unexpected error). A run whose
worker died is failed (retryable) by the purge task after twice `EMBEDDING_JOB_TIMEOUT_SECONDS`.
A failed embedding never affects keyword search.

**Deletion**: deleting a document removes its chunks, its vectors (the `chunk_embeddings`
foreign key also cascades from `documents`) and its embedding state in the same transaction.

**Operations**:

```text
uv run python apps/core/manage.py embed_documents                      # documents without current vectors
uv run python apps/core/manage.py embed_documents --organization <id>  # one organization
uv run python apps/core/manage.py embed_documents --force              # re-embed everything (billed)
uv run python apps/core/manage.py embed_documents --dry-run --limit 100
```

`POST /api/v1/documents/{id}/index/` with `{"reembed": true}` re-embeds one document (same
permission as re-indexing). `GET /api/v1/documents/search-status/` (admins with
`documents.manage_all`) is the restricted diagnostic: whether semantic search runs and why
not, the active model (key, provider, model, dimensions, distance), the vector store (extension,
table, approximate index), and the organization's counts (documents total, indexed,
semantically ready, pending, embedding, failed; passages total and embedded). See [the documents API](../api/documents.md).

## Retrieval

Runtime's `RagEngine.retrieve`:

1. **Decide the methods.** `retrieval_mode` in the grounding request: `auto` (default — the
   deployment's `RAG_RETRIEVAL_MODE`, `hybrid` by default), `lexical`, `semantic` or `hybrid`.
   Semantic search runs only when `SemanticCatalog.status()` is `ok`: embeddings enabled,
   pgvector and the table present, an active model, a configured provider (cached for
   `RAG_SEMANTIC_STATUS_TTL_SECONDS`). Otherwise `auto` quietly uses keywords, and an explicit
   `semantic` or `hybrid` request uses keywords and records `fallback: "semantic_<reason>"`.
2. **Embed the question** (only the question: no history, no memory), with the active model,
   `RAG_QUERY_EMBEDDING_TIMEOUT_SECONDS`, two attempts. One usage event (feature `embedding`,
   source `rag_query`) is written per question, successful or not. On failure: keyword
   results, `fallback: "query_embedding_failed"`.
3. **Search** (`vector.VectorRetriever`, strategy `pgvector-cosine/v1`): one SQL statement
   that joins documents, their index, their `ready` and current embedding state for the active
   model, the current chunks and their vectors (same `content_sha256`, same dimensions), applies
   **the same visibility predicate as keyword search** (organization, not deleted, completed,
   owner / organization sharing / team membership / `documents.manage_all`, and the requested
   scope), and orders by cosine distance (`<=>`). Similarity is `1 − distance`; passages below
   the model's `relevance_floor` are dropped, at most `RAG_VECTOR_CANDIDATES` are kept. The
   query runs in a savepoint: on a database error the keyword results stand and
   `fallback: "vector_search_failed"` is recorded.
4. **Combine** ([Hybrid retrieval](#hybrid-retrieval)), drop near-duplicates (Phase 08's rule),
   keep the best `max_sources`.

The `grounding` record of each reply stores `retrieval_requested`, `retrieval_mode` (what ran:
`lexical`, `semantic` or `hybrid`), `fallback` and `strategy`; regeneration reuses the
requested mode. `GET /api/v1/ai/retrieval/` tells the client which modes it can offer
(see [the conversations API](../api/conversations.md#retrieval-options)).

**Relevance floors** are per model (`relevance_floor` on the model row): 0.2 for both seeded
models. They are heuristics to keep obviously unrelated passages out, chosen from the
synthetic evaluation set; they are not confidence scores and are not calibrated on real
documents. Similarity values are never shown to users.

**Explicit `semantic` mode** uses vectors only. When nothing is above the floor the reply is the
usual "no evidence" answer (no model call), as with keywords.

## Hybrid retrieval

Hybrid mode runs both searches and fuses the two ranked lists with **reciprocal rank fusion**
(`fusion.reciprocal_rank_fusion`):

```text
score(passage) = Σ over lists containing it   1 / (RAG_RRF_K + rank in that list)     (k = 60)
```

- Ranks, not raw scores, are combined: full-text scores and cosine similarities are on
  different scales and neither is calibrated, so averaging them would be meaningless.
- A passage found by both searches appears once, with both ranks (`lexical_rank`,
  `vector_rank`) and the sum of both contributions.
- Ties are broken by the best single rank, then document id, then chunk position, so the same
  question over the same data always gives the same order.
- The strategy is recorded as `rrf(k=60):postgres-fts/v1+pgvector-cosine/v1`.

**Reranking**: `fusion.Reranker` is an interface for a cross-encoder or provider reranker
applied to the fused candidates (with a timeout; a reranker may reorder candidates but not add
or remove any, otherwise its result is ignored). **No reranker is enabled** (`NoReranker`): no
suitable model is configured in this project, and a fake one would only reorder at random.

## Indexes

By default the vector query is an **exact** (sequential) scan of the organization's vectors
for the active model, which uses the `chunk_embeddings_scope_idx` b-tree index on
(model, organization, document) to narrow the rows. It is exact and simple, and fast enough for
tens of thousands of chunks per organization (see the latency figures below).

For larger libraries, create an **HNSW** index for one model (partial, on that model's rows and
dimensions):

```text
uv run python apps/core/manage.py vector_index create --model nexa-embed-small [--m 16] [--ef-construction 64]
uv run python apps/core/manage.py vector_index drop --model nexa-embed-small
```

It is built `CONCURRENTLY` (no write lock). pgvector cannot build HNSW indexes on vectors with
more than 2 000 dimensions; the command refuses such models. HNSW is approximate:
`RAG_HNSW_EF_SEARCH` (100) trades recall for speed at query time. Filters that remove most
rows (a small organization in a large table) can make an approximate index return fewer
results than requested; for such deployments keep exact search, or use pgvector 0.8's
iterative scans.

## Security and privacy

- **Tenant isolation**: the vector query carries the same SQL authorization predicate as the
  keyword query; vectors carry their organization and a composite foreign key to
  `(organization, document)`. Tests cover other organizations, colleagues' private documents,
  deleted documents and forged document ids in semantic and hybrid modes; the predicate itself
  is Phase 08's, tested against the shared policy over a matrix of roles, teams and sharing.
- **No vectors leave the server**: no API returns vectors or similarity scores. Document
  payloads show only the semantic status, the logical model key, chunk counts and error
  messages. Model dimensions appear only in the search-status diagnostic, which is admin-only
  and organization-scoped.
- **What goes to the provider**: chunk text (Core) and the question (Runtime), to the
  configured embedding provider only. `EMBEDDING_ENABLED=false` in both services keeps all
  text on the server. Provider keys come from the environment (`AI_OPENAI_API_KEY`), never
  from source or `.env.example`.
- **Logs**: never contain document text, questions, passages or vectors; they contain ids,
  counts, timings, model keys and error codes.

## Observability and cost

| Event | Where | Fields |
| --- | --- | --- |
| `documents.embedding.completed` | Core | chunks, embedded, reused, input_tokens, duration_ms, model |
| `documents.embedding.failed` / `.provider_failed` / `.skipped` / `.stopped` | Core | error_code, retryable, reason |
| `rag.retrieval.completed` | Runtime | requested, mode, fallback, lexical/semantic candidates, query_embedding_ms, retrieval_ms, supplied, strategy |
| `rag.query_embedding.failed`, `rag.vector_search.failed` | Runtime | error code / type |

Usage events with feature `embedding` record every provider call (documents and questions),
with tokens and cost when the model has a price. Cost controls: reuse of unchanged chunks'
vectors, the daily organization budget for document embeddings, the existing per-user rate
limit on grounded questions, and `EMBEDDING_ENABLED`.

## Settings

Core:

| Setting | Default | Meaning |
| --- | --- | --- |
| `EMBEDDING_ENABLED` | true | Off: no document embeddings, keyword search only |
| `EMBEDDING_MODEL` | (empty) | Model key; empty uses the rule above |
| `EMBEDDING_BATCH_SIZE` | 64 | Chunks per provider request (capped by the model) |
| `EMBEDDING_MAX_BATCH_TOKENS` | 100 000 | Estimated tokens per request |
| `EMBEDDING_TIMEOUT_SECONDS` | 30 | Per provider request |
| `EMBEDDING_MAX_ATTEMPTS` | 3 | Attempts per batch inside a run |
| `EMBEDDING_TASK_RETRIES` | 3 | Celery retries of a run that failed retryably |
| `EMBEDDING_JOB_TIMEOUT_SECONDS` | 900 | Time limit of one run |
| `EMBEDDING_ORG_TOKENS_PER_DAY` | 2 000 000 | Document embedding budget per organization |

Runtime:

| Setting | Default | Meaning |
| --- | --- | --- |
| `EMBEDDING_ENABLED` / `EMBEDDING_MODEL` | true / (empty) | As in Core; keep them equal |
| `RAG_RETRIEVAL_MODE` | hybrid | What `auto` means: `lexical`, `semantic` or `hybrid` |
| `RAG_VECTOR_CANDIDATES` | 40 | Vector candidates before fusion |
| `RAG_RRF_K` | 60 | Fusion constant |
| `RAG_QUERY_EMBEDDING_TIMEOUT_SECONDS` | 10 | Question embedding timeout |
| `RAG_HNSW_EF_SEARCH` | 100 | HNSW search breadth (when an index exists) |
| `RAG_SEMANTIC_STATUS_TTL_SECONDS` | 30 | Cache of the availability check |

Both: `AI_OPENAI_API_KEY`, `AI_OPENAI_BASE_URL`, `AI_MOCK_PROVIDER` (as for the AI Gateway).

## Evaluation

```text
uv run pytest -s apps/runtime/tests/test_rag_eval_modes.py    # prints the comparison
```

`apps/runtime/tests/data/rag_eval_v2.json` is a synthetic set written for this purpose: eleven
documents (a distractor with an outdated rule, overlapping and contradicting documents, an
exact copy, a table, a private document of another user) plus a copy in another organization;
fifteen answerable questions labelled exact, paraphrase, synonym, typo, morphology,
multi-passage, distractor, overlapping and tabular; four unanswerable questions; a
weak-evidence question; and a document whose content changes and must be re-embedded. Results
with the **mock** embedding provider (k = 6, measured on 2026-10-21, PostgreSQL 16 +
pgvector 0.6.2):

| Mode | recall@6 | precision@6 | MRR | nDCG@6 | Out-of-scope passages | Unanswerable with no evidence |
| --- | --- | --- | --- | --- | --- | --- |
| lexical | 0.87 | 0.51 | 0.87 | 0.87 | 0 | 4/4 |
| semantic (mock) | 0.67 | 0.58 | 0.67 | 0.67 | 0 | 4/4 |
| hybrid (mock) | 0.87 | 0.51 | 0.83 | 0.84 | 0 | 4/4 |

Precision@6 is the share of the passages supplied (at most six) that contain an expected
answer. Hybrid answers in chat had no invalid citations. On a generated library of 150
documents (262 chunk vectors, exact search, a laptop), retrieval took p50/p95 ≈ 39/50 ms
lexical, 88/96 ms semantic, 134/151 ms hybrid, including the question embedding and the test
client's overhead.

**What this shows and does not show.** With the mock, "semantic" retrieval is a second
lexical method and is *worse* than full-text search on this set (it misses a paraphrase, a
typo it was expected to catch and the distractor). Hybrid keeps lexical recall and loses a
little MRR on the distractor question. The set measures that the pipeline, authorization,
fusion, fallbacks and no-evidence behaviour work; it cannot show that real embeddings improve
answers. Neither synonym question nor the badly misspelt one is found by any mode with the
mock. A live comparison with OpenAI is opt-in and was **not run** for this report:

```text
NEXA_LIVE_EMBEDDING_EVAL=1 NEXA_AI_OPENAI_API_KEY=sk-... \
  uv run pytest -s apps/runtime/tests/test_rag_eval_modes.py::test_live_openai
```

It sends the synthetic documents and questions to OpenAI (billed) and prints the same table.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `semantic.status = unavailable`, reason `pgvector_missing` | Install pgvector and run `setup_vector_search` ([above](#installing-pgvector)) |
| reason `provider_not_configured` | `AI_OPENAI_API_KEY` missing in that service, or `EMBEDDING_MODEL` names a model whose provider has no key |
| reason `no_model` | No active embedding model row; run migrations (they seed the registry) |
| reason `disabled` | `EMBEDDING_ENABLED=false` |
| Documents stay `pending` | The `core.documents` worker is not running: start it, or run `embed_documents` |
| `failed`, `quota_exceeded` | The organization's daily budget (`EMBEDDING_ORG_TOKENS_PER_DAY`) is used up |
| `failed`, `provider_error` | The provider refused the request (key, model, input): check Core's logs for the error code |
| Replies show "searched by keywords" (`fallback`) | Semantic search was not available for that answer; the fallback code says why |
| Core and Runtime disagree (documents ready, Runtime unavailable) | Different `EMBEDDING_ENABLED`, `EMBEDDING_MODEL` or keys in the two services |
| `ERROR: extension "vector" is not available` | The extension's files are not installed on the database server (not the client) |
| `permission denied to create extension "vector"` | Create it as a superuser, or use pgvector ≥ 0.7 (trusted) as the database owner |

## Rollback

- **Turn semantic search off**: `EMBEDDING_ENABLED=false` in Core and Runtime (or
  `RAG_RETRIEVAL_MODE=lexical` in Runtime to keep embedding documents but answer by
  keywords). Answers go back to Phase 08 behaviour immediately; nothing else changes.
- **Drop an HNSW index**: `vector_index drop --model <key>`.
- **Remove the vectors**: migrate `documents` back to `0004` (drops `chunk_embeddings`), then to
  `0003` (drops `document_embeddings`); `ai` back to `0002` removes the embedding model
  registry. The extension itself can stay or be dropped with `DROP EXTENSION vector`.
  Keyword indexes and chunks are not touched by any of these.

## Known limitations

- Measured only with the mock provider here; real-embedding quality on real documents is not
  established (see [Evaluation](#evaluation)).
- No reranker. Relevance floors are heuristics.
- One active model at a time; switching models means re-embedding (old vectors stay until
  pruned with the documents, and are ignored).
- Token counts for the mock are estimates; OpenAI's are the provider's.
- Exact search by default; HNSW is opt-in per model and limited to 2 000 dimensions.
- The local Windows development database (PostgreSQL 18 without pgvector) runs keyword search
  only; pgvector paths were verified on PostgreSQL 16 + pgvector 0.6.2 and run in CI on
  `pgvector/pgvector:pg17`.

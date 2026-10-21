# ADR-037: Embeddings in PostgreSQL with pgvector, hybrid retrieval by reciprocal rank fusion, keyword search as the floor

Status: accepted

## Context

Phase 08 retrieves passages by keywords only ([ADR-036](036-rag-in-chat-lexical-baseline.md)):
paraphrases, synonyms and misspellings are missed. Phase 09 asks for embeddings, vector search
and hybrid retrieval, without weakening tenant isolation, without exposing vectors, with honest
behaviour where vector search is not available, and with an evaluation that compares the
methods. Choices to make: where vectors live, how embedding models are identified and kept
apart, who embeds what and when, how the two result lists are combined, and what happens when
any part of the vector path is missing or fails.

## Decision

1. **Vectors live in the application's PostgreSQL, with pgvector**, in one table
   (`chunk_embeddings`) keyed by chunk and embedding model. No separate vector database. The
   vector query is ordinary SQL that joins the same tables and applies the same authorization
   predicate as keyword search.
2. **An embedding model registry separate from chat models** (`ai_embedding_models`). A
   model's space (`provider/model@version:dimensions:distance`) is immutable; every vector is
   stored with its model and dimensions, and only vectors of the one active model are
   compared. Core and Runtime choose the active model with one shared function.
3. **Core embeds documents** in a Celery task triggered by indexing (versioned by a
   generation counter like indexing, idempotent upserts, vectors of unchanged chunks reused,
   per-batch usage events, a daily organization budget). **Runtime embeds only the question.**
   Vectors are searched only when a document's embedding state is ready and built from its
   current index generation.
4. **Hybrid retrieval by reciprocal rank fusion** (k = 60) of the keyword and vector lists,
   with deterministic tie-breaking. Ranks are fused, not scores, because neither score is
   calibrated.
5. **Keyword search is the floor.** Missing pgvector, no model, no provider key, a failed
   question embedding or a failed vector query all fall back to keyword retrieval; an explicit
   request for semantic or hybrid retrieval that could not run records why (`fallback`) on the
   reply. Clients can ask which modes are available (`GET /ai/retrieval/`).
6. **Exact search by default**, HNSW per model on request. **No reranker** is enabled: the
   interface exists, no suitable model is configured, and no stand-in pretends to rerank.
7. **A deterministic mock embedding provider** (hashed words and trigrams) for development and
   tests, documented as not semantic and refused in production.

## Alternatives

- **A dedicated vector database** (Qdrant, Weaviate, Pinecone). Better at very large scale,
  but a second store to secure and keep consistent: every vector would need the document's
  visibility copied in and kept in sync, deletions would cross two systems, and authorization
  could no longer be one SQL predicate. Rejected at this scale; the `Retriever` protocol keeps
  the option open.
- **A vector column on `document_chunks`.** Simpler, but one model at a time and a schema
  change per dimension size; switching models would overwrite vectors in place. A separate
  table keyed by model allows re-embedding with a new model side by side.
- **Score blending** (weighted sum of normalized scores). Needs calibration per model and per
  corpus that we cannot do honestly; RRF needs none and is the common baseline.
- **Embedding in Runtime at query time** (lazy). Puts provider latency and failures on the
  chat path and duplicates work; rejected.
- **Random or hash vectors in production** when no provider is configured. They would look
  like semantic search and are not; refused.

## Consequences

- Semantic search needs pgvector on the database server. Compose and CI have it; the local
  Windows development database does not, and runs keyword search only (verified on both).
- Document text and questions are sent to the embedding provider when semantic search is on;
  `EMBEDDING_ENABLED=false` keeps them on the server.
- Exact search cost grows with an organization's chunk count; HNSW is available per model
  (up to 2 000 dimensions) when that matters.
- Quality gains from real embeddings are not demonstrated by the CI evaluation, which uses
  the mock; the live evaluation is opt-in and billed.
- Switching the active model requires re-embedding all documents (`embed_documents`).

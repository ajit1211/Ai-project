# ADR-036: Document-grounded answers inside the chat engine, lexical retrieval first, validated inline citations

Status: accepted

## Context

Phase 08 asks for question answering over the user's documents with traceable citations,
through the existing AI Gateway and chat, authorized at every step, and explicitly without
embeddings or vector search (Phase 09). Choices to make: where grounded queries enter the API,
how passages are found, who stores what, how the model's citations are made trustworthy, and
how retrieval stays replaceable.

## Decision

1. **Grounded questions are chat messages.** `POST /conversations/{id}/messages/` accepts an
   optional `grounding` (documents, a team, or all readable documents); there is no separate
   `/rag/query/` endpoint. The reply is an ordinary assistant message (same streaming,
   cancellation, retries, usage ledger, persistence) with a `grounding` record and a `sources`
   relation; the stream gains one `sources` event.
2. **Chunks and the index belong to Core**; the indexing worker chunks the text Phase 07
   stored (never the original file) and replaces a document's chunks in one transaction,
   guarded by a generation counter. **Retrieval runs in Runtime**, which reads the chunks
   directly (every Core table is readable by `nexa_runtime`), as the memory engine does.
3. **PostgreSQL full-text search** (`english`, section weighted above text, GIN index) with a
   transparent score (share of query terms + cover density) is the Phase 08 retriever, behind
   a `Retriever` protocol so a vector retriever can be added beside it.
4. **Authorization happens twice and inside the query.** The requested scope is checked with
   the shared policy before anything is stored, and every search applies the same policy as a
   SQL predicate; stored sources are re-authorized on every read.
5. **Inline citation markers (`[S1]`), validated by the server** against the labels given in
   that turn, instead of a JSON answer schema. Sources are stored as references (document id,
   chunk id), never as copied text.
6. **No model call without evidence**: when retrieval finds nothing, the reply says so.

## Alternatives

- **A separate RAG query API** with its own persisted query records. Duplicates conversation
  persistence, streaming and usage tracking, and splits history in two. Rejected; the chat
  message is the query record.
- **pgvector or a vector database now.** Out of scope by the brief; also needs an embedding
  model and an evaluation set first. Phase 09.
- **Retrieval in Core over HTTP.** Adds a service-to-service call on the latency path and a
  second authorization surface; ADR-033 already chose direct reads.
- **Structured JSON answers** (`{answer, citations}`) through the gateway's structured output.
  Strong typing, but the answer could not be streamed as text, and providers differ in how
  reliably they follow schemas. Markers validated afterwards give the same guarantee about
  what is shown as a citation.
- **Copying excerpts into `message_sources`.** Survives document deletion, which is exactly the
  problem: deleted or unshared documents would keep showing their text in old conversations.

## Consequences

- Lexical retrieval misses paraphrases and synonyms, and one shared common word can surface
  an unrelated passage; the model is instructed to say when the evidence does not answer, and
  the UI flags uncited answers. Documented as a limitation until Phase 09.
- Runtime's visibility predicate duplicates the shared policy in SQL; a test keeps them equal.
- Citations from a provider that ignores the marker convention are lost (`uncited`), not
  repaired.
- Changing the chunking strategy requires re-indexing; stale indexes stay searchable and are
  reported as `stale`.
- Chunk ids are deterministic, so unchanged documents keep their citations across re-indexing.

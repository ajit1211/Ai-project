# API: conversations and messages

All endpoints live under `/api/v1` and use the envelope described in
[authentication.md](authentication.md). They are served by FastAPI (Runtime) and need
`Authorization: Bearer <access token>`, an active membership in the token's active
organization, and the `conversation.*` permission named below (every system role has them).
A conversation is private to the user who created it: another member of the same
organization, or anyone in another organization, gets 404. Identity and organization come
from the token; the request body cannot name a user, an organization or an owner.

| Method and path | Permission | Purpose |
| --- | --- | --- |
| POST `/conversations/` | conversation.create | Start a conversation |
| GET `/conversations/` | conversation.view | Your conversations, newest activity first |
| GET `/conversations/{id}/` | conversation.view | One conversation |
| PATCH `/conversations/{id}/` | conversation.update | Rename, change the model, archive or restore |
| DELETE `/conversations/{id}/` | conversation.delete | Delete it and every message |
| GET `/conversations/{id}/messages/` | conversation.view | Messages, paginated |
| POST `/conversations/{id}/messages/` | conversation.update + ai.chat | Send a message and generate the reply |
| POST `/conversations/{id}/regenerate/` | conversation.update + ai.chat | Generate the last reply again |
| POST `/conversations/{id}/cancel/` | conversation.update | Stop the reply being generated |
| GET `/conversations/{id}/messages/{message_id}/sources/` | conversation.view | Whole passages behind a document-grounded reply (Phase 08) |

## Error codes

Besides the codes in [authentication.md](authentication.md) and the `AI_*` codes in
[ai.md](ai.md):

| Code | Status | Meaning |
| --- | --- | --- |
| RESOURCE_NOT_FOUND | 404 | No such conversation, or not yours |
| GENERATION_IN_PROGRESS | 409 | A reply is already being generated; wait for it or cancel it |
| CONFLICT | 409 | The conversation is archived, or there is nothing to regenerate |
| VALIDATION_FAILED | 422 | A field is invalid; see `details` |
| AI_MODEL_UNAVAILABLE | 422 | The conversation's model is not available to this organization |
| RATE_LIMITED | 429 | Too many new conversations in a minute; see `Retry-After` |
| DOCUMENT_UNAVAILABLE | 404 | A selected document or team does not exist for you (deleted, another organization, not shared, or a guessed id); nothing was stored |
| DOCUMENT_NOT_INDEXED | 409 | A selected document is not searchable yet; `details` lists which |
| RAG_INVALID_SCOPE | 422 | `grounding` must name exactly one of: documents, a team, all documents |
| SERVICE_UNAVAILABLE | 503 | Answers from documents are switched off (`RAG_ENABLED=false`) |
| RAG_RETRIEVAL_FAILED | — | (stream `error` event only) Searching the documents failed; regenerate to retry |
| AI_EMPTY_RESPONSE | — | (stream `error` event only) The model finished without any text; retryable |
| GENERATION_INTERRUPTED | — | (stored `error_code` only) The process generating the reply died; regenerate to retry |

## Objects

Conversation:

```json
{
  "id": "0199...",
  "title": "Explain Django middleware",
  "model": "nexa-balanced",
  "status": "active",
  "message_count": 4,
  "generating": false,
  "created_at": "2026-10-09T08:00:00+00:00",
  "updated_at": "2026-10-09T08:05:12+00:00",
  "last_message_at": "2026-10-09T08:05:12+00:00"
}
```

`model` is a logical model name (see `GET /ai/models/`); an empty string means the
organization's default model. `generating` is true while a reply is being produced.

Message:

```json
{
  "id": "0199...",
  "conversation_id": "0199...",
  "sequence": 2,
  "role": "assistant",
  "content": "Middleware in Django is ...",
  "status": "completed",
  "client_message_id": "",
  "model": "nexa-balanced",
  "provider": "anthropic",
  "provider_model": "claude-sonnet-5-5",
  "input_tokens": 212,
  "output_tokens": 341,
  "cost_usd": "0.003834",
  "latency_ms": 2210,
  "ttft_ms": 480,
  "finish_reason": "stop",
  "error_code": null,
  "request_id": "req_0199...",
  "created_at": "2026-10-09T08:05:09+00:00",
  "completed_at": "2026-10-09T08:05:12+00:00",
  "grounding": null,
  "sources": []
}
```

`grounding` and `sources` are set only on document-grounded replies (see
[below](#document-grounded-answers)); they are `null` and `[]` everywhere else.

`role` is `user` or `assistant`. `status` is `completed` for user messages and one of
`pending`, `streaming`, `completed`, `failed`, `cancelled` for assistant messages. A failed or
cancelled assistant message keeps whatever text had arrived; `error_code` names the failure,
and its token counts and cost are what the stopped or failed call used. Generation fields
(`provider` onwards) are empty or zero on user messages.

Text fields (`content`, `title`, the `q` search) must not contain the NUL character
(U+0000): 422 `VALIDATION_FAILED`. NUL in a model's answer is dropped.

## Conversations

### POST /conversations/

Body: `{"title"?: string ≤ 200, "model"?: string}`. `model` must be a name from
`GET /ai/models/`. Returns 201 with the conversation. A new conversation has no messages and
no title; the first user message becomes its title. At most 20 new conversations per user
per minute (429 `RATE_LIMITED` beyond that).

### GET /conversations/

Query: `status=active|archived` (default `active`), `q=` (title search), `limit=` (1 to 100,
default 30), `cursor=` (from the previous page's `meta.next_cursor`). Ordered by last activity,
newest first. Returns `data: [Conversation]` and `meta: {"next_cursor": "...", "has_more": bool}`.

### GET /conversations/{id}/

Returns the conversation.

### PATCH /conversations/{id}/

Body: any of `{"title": string, "model": string, "status": "active" | "archived"}`. Returns
the updated conversation. Renaming to an empty title restores the automatic title.

### DELETE /conversations/{id}/

Deletes the conversation and all of its messages. Usage records in the AI ledger are kept
(they carry no message text). 200 on success. 409 `GENERATION_IN_PROGRESS` while a reply is
being generated.

## Messages

### GET /conversations/{id}/messages/

Query: `limit=` (1 to 200, default 50), `before=` (a message `sequence`; returns messages
older than it). Without `before`, the newest `limit` messages. `data` is ordered by
`sequence`, oldest first; `meta` is `{"has_more": bool, "next_before": sequence | null}`.

### POST /conversations/{id}/messages/

Body:

```json
{"content": "Explain Django middleware", "client_message_id": "f3c4...", "model": "nexa-fast"}
```

`content` is 1 to 100 000 characters. `client_message_id` (optional, ≤ 64 characters) makes
the submission idempotent: sending the same id again returns the original user message and
its reply instead of generating a new one. `model` (optional) changes the conversation's
model for this and later messages.

The user message is stored first, then an assistant message in state `pending`, then the
reply is generated through the AI Gateway with the conversation's recent messages as
context. 409 `GENERATION_IN_PROGRESS` if a reply is already being generated; 409 `CONFLICT`
if the conversation is archived.

**Streaming** (`Accept: text/event-stream`): the response is `200 text/event-stream` as soon
as the two messages are stored. Protocol `nexa.chat.v1`, also sent as the
`X-NEXA-Stream-Protocol` response header:

```text
event: message_start
data: {"protocol":"nexa.chat.v1","conversation_id":"0199...","request_id":"req_...",
       "user_message":{...Message...},"assistant_message":{...Message, "status":"pending"}}

event: text_delta
data: {"message_id":"0199...","text":"Middleware "}

event: usage
data: {"message_id":"0199...","input_tokens":212,"output_tokens":341,"total_tokens":553,
       "cached_input_tokens":0,"cache_write_input_tokens":0,"cost_usd":"0.003834"}

event: message_complete
data: {"message":{...Message, "status":"completed"}}
```

Terminal events, exactly one per stream: `message_complete` (status `completed`),
`message_cancelled` (status `cancelled`, with the text that had arrived) or `error`:

```text
event: error
data: {"message_id":"0199...","code":"AI_TIMEOUT","message":"The AI provider took too long to answer.",
       "retryable":true,"retry_after":null,"request_id":"req_...",
       "assistant_message":{...Message, "status":"failed"}}
```

`usage` arrives once the AI Gateway has metered the call: before `message_complete`, and
also before an `error` caused by the model call (with what the failed call used). An `error`
for a refusal before the call (an exhausted quota, for example) has no `usage` before it. A
client should treat `usage` as information and wait for the terminal event.

Every event carries an incrementing `id`; `: ping` comment lines keep the connection alive
while the model thinks. Events after `message_start` always name the assistant `message_id`,
so a client can ignore events for a message it is not showing. Failures that happen before
the messages are stored (authorization, validation, a busy or archived conversation) are
ordinary JSON errors with their HTTP status; check the response `Content-Type`.

Aborting the request cancels generation: the assistant message is saved as `cancelled` with
the text received so far. The provider is told to stop by closing its stream; tokens it
generated before noticing are still billed.

**Non-streaming** (`Accept: application/json`, the default): the response waits for the
reply and returns `201 {"data": {"user_message": Message, "assistant_message": Message}}`. On
a generation failure the assistant message is stored as `failed` and the response is the
AI error (its status and code), so the client should reload the messages. If the reply is
stopped with `POST /cancel/`, the response is the same `201` with the assistant message
`cancelled`.

Retrying with the same `client_message_id` returns `200` with the stored exchange as JSON,
even when the request asked for a stream. While that reply is still being generated the
retry gets 409 `GENERATION_IN_PROGRESS`; if the process generating it died, the reply comes
back `failed` with `GENERATION_INTERRUPTED`.

### POST /conversations/{id}/regenerate/

Body: `{"model"?: string}`. Generates the last reply again: the last assistant message
(completed, failed or cancelled) is deleted and a new one is generated for the last user
message. If the last message is a user message with no reply (its reply failed before being
stored), a reply is generated for it. Same streaming and non-streaming behaviour as sending a
message; `message_start.user_message` is the user message being answered. 409 `CONFLICT` when
there is nothing to regenerate.

### POST /conversations/{id}/cancel/

Stops the reply currently being generated, from any client. Returns
`{"cancelled": true, "message_id": "..."}` or `{"cancelled": false, "message_id": null}` when
nothing is running. A streaming request ends with a `message_cancelled` event; a
non-streaming one returns its reply as `cancelled`. A stop that arrives after the model has
finished (after the `usage` event) changes nothing: the reply completes. Because providers are
stopped by closing their stream, a few more tokens may be billed after the cancel; they are
not shown.

## Context

The reply is generated from NEXA's system instructions plus the most recent messages of the
same conversation: at most 40 messages and a token budget that fits the model's context
window (estimated at three characters per token, with the output budget reserved). Since
Phase 06, relevant memories and a rolling summary of older messages are added
([memory-engine.md](../architecture/memory-engine.md)); since Phase 08, a grounded reply also
gets the retrieved passages ([rag.md](../architecture/rag.md#evidence-and-prompt)). Failed
assistant messages are skipped; cancelled ones contribute the text they have. The window
always starts with a user message.

## Document-grounded answers

Phase 08 ([rag.md](../architecture/rag.md)). Send a message with `grounding` to have the reply
answered from documents you may read, with citations. Without `grounding` nothing changes.

```json
{
  "content": "How long do customers have to request a refund?",
  "grounding": {"document_ids": ["0199...", "0199..."], "max_sources": 6}
}
```

`grounding` names exactly one of: `document_ids` (1 to 20 documents), `team_id` (documents
shared with a team you belong to), or `all_documents: true` (every document you may read).
`max_sources` (optional, 1 to 12, default 6) bounds the passages given to the model.
`retrieval_mode` (Phase 09, optional): `auto` (default: the deployment's choice, hybrid where
semantic search runs), `lexical` (keywords), `semantic` (meaning, by embeddings) or `hybrid`
(both, fused); see [vector-search.md](../architecture/vector-search.md#retrieval) and
[Retrieval options](#retrieval-options). The
question is at most 4 000 characters. The scope is checked before anything is stored: 404
`DOCUMENT_UNAVAILABLE` for a document or team that does not exist for you, 409
`DOCUMENT_NOT_INDEXED` for one that is not searchable yet, 422 `RAG_INVALID_SCOPE`, 429 when
too many grounded questions were asked this minute. Regenerating a grounded reply grounds it
again with the same scope, checked again.

The stream gains one event, after `message_start` and before any text:

```text
event: sources
data: {"message_id":"0199...","searched_documents":2,"sources":[
        {"label":"S1","rank":1,"cited":false,"available":true,"score":1.2412,
         "document_id":"0199...","chunk_id":"6f1e...","document_title":"Employee handbook",
         "filename":"handbook.pdf","format":"pdf","section":"Handbook > Refunds",
         "page_start":3,"page_end":3,"row_start":null,"row_end":null,
         "char_start":10230,"char_end":11405,"excerpt":"Customers may request a refund ...",
         "excerpt_truncated":true}]}
```

The answer text cites passages as `[S1]`, `[S1][S3]` or `[S1, S2]`. `message_complete`
carries the reply with its final `grounding` and `sources` (now with `cited` set):

```json
"grounding": {
  "mode": "documents", "scope": "documents", "document_ids": ["0199..."], "team_id": null,
  "outcome": "answered", "searched_documents": 2, "retrieved": 5, "supplied": 5, "cited": 1,
  "invalid_citations": [], "retrieval_ms": 18,
  "strategy": "rrf(k=60):postgres-fts/v1+pgvector-cosine/v1",
  "retrieval_requested": "auto", "retrieval_mode": "hybrid", "fallback": null
}
```

`retrieval_requested` is the mode asked for, `retrieval_mode` what ran (`lexical`, `semantic`
or `hybrid`), and `fallback` why they differ, when they do: `semantic_disabled`,
`semantic_pgvector_missing`, `semantic_no_model`, `semantic_provider_not_configured` (semantic
search does not run here), `query_embedding_failed` or `vector_search_failed` (it failed for
this question). A fallback answers by keywords; it is not an error. Replies from before Phase
09 have `retrieval_mode: "lexical"` and no fallback.

`outcome` is `answered` (at least one valid citation), `uncited` (the answer cites none of the
passages: treat it as unverified), `no_evidence` (nothing matched; the model was not called and
the reply says so), `failed` or `cancelled`. `invalid_citations` lists labels the answer used
that were never given to it; they never become sources. A clean failure of the search is an
`error` event with code `RAG_RETRIEVAL_FAILED`.

When messages are listed, `sources` are read again and the documents re-authorized for the
reader: a source whose document was deleted, is no longer shared, or whose passage changed is
`{"label": "S1", "rank": 1, "cited": true, "available": false, ...}` with every other field
`null`.

### Retrieval options

`GET /api/v1/ai/retrieval/` (Phase 09; members with `documents.view`) tells a client which
retrieval modes it can offer:

```json
{"data": {"modes": ["hybrid", "semantic", "lexical"], "default_mode": "hybrid",
          "semantic_available": true, "semantic_unavailable_reason": null,
          "embedding_model": "nexa-embed-small"}}
```

Without semantic search: `"modes": ["lexical"]`, `"default_mode": "lexical"`,
`"semantic_available": false` and the reason (`disabled`, `pgvector_missing`, `no_model`,
`provider_not_configured`). Requesting a mode that is not listed is allowed and falls back as
described above.

### GET /conversations/{id}/messages/{message_id}/sources/

The sources of one grounded reply with whole passages (`excerpt` up to 4 000 characters),
re-authorized now. `[]` for an ordinary message; 404 for a message not in the conversation.

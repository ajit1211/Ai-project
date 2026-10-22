# Chat engine

Phase 05. Users hold private conversations with NEXA's models: start one, pick a model, send
messages, watch replies stream in, stop or regenerate a reply, and come back to the history
later. The chat engine is the first product feature built on the AI Gateway
([ai-gateway.md](ai-gateway.md)); it adds conversations, messages, context assembly and a
streaming protocol, and reuses everything else.

```text
React (/chat) ──► edge ──► FastAPI /api/v1/conversations/*   (Runtime)
                             │  CurrentPrincipal: token verified with Core's JWKS,
                             │  membership + permissions read from PostgreSQL
                             ▼
                     ChatService ── conversations / messages (PostgreSQL; Core owns the schema)
                             │
                             ▼
                     AI Gateway (nexa_ai) ── policy, quota, router, adapters, usage ledger
                             │
                             ▼
                          Provider ──► nexa.chat.v1 SSE stream ──► React
```

## Where the code lives, and why not in Django

| Part | Where |
| --- | --- |
| Tables, migrations, constraints, admin | `apps/core/nexa_core/chat` (Django owns every schema) |
| Permissions `conversation.view/create/update/delete` | Core's catalog (`access/catalog.py`), granted to every system role |
| Endpoints, generation lifecycle, context, cancellation | `apps/runtime/nexa_runtime/chat` |
| Model calls, quotas, usage | the Phase 04 gateway, unchanged |
| UI | `apps/web/src/pages/ChatPage.tsx`, `components/chat/`, `hooks/`, `api/conversations.ts` |

The Phase 05 brief sketches React → Django → FastAPI, with Django holding conversation
management and calling FastAPI for generation. NEXA keeps the split decided in Phase 01
(section 18.2), Phase 03 (services never call each other) and ADR-029 instead:
`/conversations` is served by Runtime, and Django owns the schema, the permissions and the
admin. A Django hop would put a synchronous Django worker in the middle of every token stream
and would need a new service-to-service credential; the edge routing table already sends
`/conversations` to Runtime. [ADR-033](../adr/033-conversations-in-runtime.md) records the
decision and the alternatives.

### How Django and FastAPI cooperate

There is no request from one service to the other. They cooperate through three shared things:

| Concern | Mechanism |
| --- | --- |
| Authentication | Core issues the access token. Runtime verifies it with Core's public keys from `/.well-known/jwks.json` (fetched once, refetched on an unknown key id) and checks the session row on every request. No secret is shared. |
| Authorization | Runtime reads the membership, role and permissions from the tables Core owns, through the `nexa_runtime` database role, and applies the shared policy (`nexa_common.access`). |
| Data | One PostgreSQL database. Core owns every migration; `nexa_runtime` may write only `conversations`, `messages` and `ai_usage_events`, enforced by grants. |
| Errors | Both services raise `nexa_common.errors` classes and render the same envelope; the codes are shared. |
| Request IDs | The edge (Vite in development) forwards `X-Request-ID`; each service's middleware keeps a safe incoming ID or mints one, binds it to every log line, returns it in the response header, and Runtime stores it on the assistant message and the usage row. |

## Data model

```text
Organization ─< Conversation ─< Message
                   │
                   └─ owner (User)
```

| Table | Key columns | Constraints and indexes |
| --- | --- | --- |
| `conversations` | id (UUIDv7), organization, owner, title, model (logical name; empty = default), prompt_version, status (`active`/`archived`), message_count, active_generation_id, generation_started_at, metadata, created_at, updated_at, last_message_at | unique (organization, id) as the target of the messages' composite key; index (organization, owner, last_message_at desc) for the sidebar |
| `messages` | id, organization, conversation, sequence, role (`user`/`assistant`), content, status, client_message_id, model, provider, provider_model, input/output tokens, cost_micros, latency_ms, ttft_ms, usage_event_id, request_id, finish_reason, error_code, created_at, completed_at | unique (conversation, sequence); unique (conversation, client_message_id) where set; composite foreign key (organization, conversation) → conversations, so a message can never sit in another tenant's conversation; cascade on conversation delete |

Deletion: deleting a conversation deletes its messages (cascade). Deleting a user or an
organization deletes their conversations. Usage ledger rows are never deleted: they hold no
text and point at messages by plain UUID (`source_id`). `metadata` is reserved for
non-sensitive per-conversation settings; credentials and internal data never go there.

No separate generation table: an assistant message *is* the generation record (status,
model, timings, error), and token and cost truth stays in the Phase 04 usage ledger, linked
both ways (`messages.usage_event_id` ↔ `ai_usage_events.source_type = 'message'`,
`source_id = message id`). Duplicating usage per message would create two sources of truth.

## Ownership and authorization

A conversation is private to its owner inside one organization. Every request loads the
conversation scoped by the organization from the token, then applies the shared policy with
`visibility = private`: anyone but the owner, in the same organization or another, gets 404,
never 403. Message endpoints always go through the conversation, so a message id cannot be
used to reach another conversation. Sending and regenerating also need `ai.chat`, and the
gateway re-checks it, applies the organization's model grants and quotas, and meters the call.
Request bodies are closed schemas: a client cannot set an owner, an organization, a role or a
permission, and user text is always sent to the model as a `user` turn.

## Generation lifecycle

```text
POST /conversations/{id}/messages/  {content, client_message_id?, model?}
 1. Authenticate, load the principal                         ─┐
 2. Lock the conversation row; owner + permission checks      │ one database
 3. Validate: active conversation, model allowed, idempotency │ transaction;
 4. Claim the conversation for one generation (or 409)        │ JSON errors
 5. Store the user message and a `pending` assistant message ─┘
 6. Respond 200 text/event-stream, send `message_start`
 7. Build context: system prompt + recent messages within budget
 8. Gateway: permission, policy, quota reservation, router, provider stream
 9. Relay `text_delta`s; the assistant message becomes `streaming`;
    partial text is saved every 2 seconds
10. Finish exactly once: `completed`, `failed` (with a safe code) or `cancelled`,
    usage copied from the gateway, the claim released; terminal event sent
```

Step 10 is one transaction that locks the conversation row before the message row, the same
order sending, regenerating and deleting use, so two of them cannot deadlock. It runs in a
task of its own: a client that leaves during this final write cannot interrupt it half-way
and leave the conversation claimed. If the write fails (the database went away, a value it
rejects), a smaller write marks the reply `failed` with `AI_INTERNAL_ERROR` and releases the
claim, and the stream still ends with an `error` event; if even that fails, the claim expires
on its own (see below).

Message states: user messages are stored `completed`. Assistant messages go `pending` →
`streaming` → `completed` | `failed` | `cancelled`. A reply is `completed` only after the
gateway's `message.end`; a stream that breaks after some text is `failed` with the text it
had, never `completed`. A reply with no text is `failed` with `AI_EMPTY_RESPONSE` (or
`AI_CONTENT_REJECTED` for a refusal).

On failure the user's message is always kept, the assistant message stores the NEXA error
code (never a provider message or a stack trace), the stream's `error` event says whether a
retry makes sense and after how long, and **Regenerate** retries: it deletes the last reply,
whatever its state, and generates a new one for the same question.

### Concurrency and idempotency

- One generation per conversation at a time. Step 4 is an `UPDATE ... WHERE
  active_generation_id IS NULL` on the locked row; a second send gets 409
  `GENERATION_IN_PROGRESS`. A claim older than the AI total timeout plus a minute belongs to a
  process that died and is taken over; the reply that process left `pending` or `streaming`
  is marked `failed` with `GENERATION_INTERRUPTED` instead of staying unfinished forever. A
  replayed `client_message_id` whose reply was abandoned that way reports it the same way.
- `client_message_id` makes a submission idempotent: a retried request with the same id
  returns the stored user message and its reply as JSON instead of calling the model again.
  The UI creates one per submission and reuses it when it retries after a network error.
- Exactly-once across the provider is not promised: if the connection drops after the
  provider started generating, the provider bills that work, the reply is stored as
  `cancelled`, and a retry is a new generation (with its own usage row).

### Stopping

A reply stops when the client aborts the request, or when `POST /cancel/` is called from any
client. The generation loop reads the gateway stream in a separate task and checks a stop
flag four times a second; `/cancel/` sets that flag in the same process and a Redis key that
other processes poll once a second. Stopping closes the gateway stream, which closes the
provider's stream. Providers have no cancel API for a running generation: tokens generated
before the provider notices the closed connection are billed and recorded as a `cancelled`
usage row, and the reply shows them. Without Redis, `/cancel/` only reaches generations in the
same process; aborting the request always works.

A stop is honoured at any point until the gateway has metered the call: also before the
provider was called at all (the gateway stream is handed to its reader before anything else
can run, so even then the quota reservation is released and a `cancelled` usage row is
written). A stop that arrives once the model has finished (after the `usage` event) changes
nothing; the reply completes. `/cancel/` also stops a reply requested without streaming: the
gateway call is cancelled (or not started, if the stop came first) and the `201` response
carries the reply as `cancelled`.

The web client presses **Stop** by calling `/cancel/` and then waiting for the stream's
terminal event, never by dropping the request first. If `/cancel/` finds nothing running yet
(the messages are still being stored), dropping the request would not undo the send, so the
client asks again as soon as `message_start` arrives; only if nothing arrives within five
seconds does it close the stream and reload.

A client that disconnects (closed tab, lost network) stops the generation too: the reply is
stored as `cancelled` with the text that had arrived, written in a task of its own so the
cancellation of the request cannot interrupt it, and awaited at shutdown. That holds even
when the client is gone before the response starts and not one event is read: the
generation's stream object settles itself when it is closed unread, and the SSE response
closes it directly, not only through the byte encoder (which, never started, would run no
cleanup). Generation that
continues after a disconnect, and resumable streams, are deferred to Phase 18.

## Streaming protocol `nexa.chat.v1`

Server-Sent Events over the `POST` response, read with `fetch` and the SPA's SSE parser (as
in Phase 04; WebSockets would add connection state for a one-way stream). The protocol name is
sent in `message_start` and in the `X-NEXA-Stream-Protocol` header so it can evolve.

| Event | Payload | When |
| --- | --- | --- |
| `message_start` | protocol, conversation_id, request_id, user_message, assistant_message (`pending`) | First, once the messages are stored |
| `text_delta` | message_id, text | Each chunk, as it arrives; never buffered |
| `usage` | message_id, input/output/total/cached/cache-write tokens, cost_usd | Once the gateway has metered the call: before `message_complete`, and before an `error` from the provider (what the failed call used) |
| `sources` | message_id, searched_documents, sources | A grounded reply, once retrieval is done (Phase 08, [rag.md](rag.md)) |
| `tool_call` | message_id, invocation_id, call_id, tool_id, tool_name, step, status, summary, risk, error | A tool call the model asked for, once checked (Phase 10, [tools.md](tools.md)) |
| `tool_result` | message_id, invocation_id, call_id, tool_id, status, duration_ms, result, sources, error | When that call ends |
| `message_complete` | message (`completed`) | Terminal |
| `message_cancelled` | message (`cancelled`, with partial text) | Terminal |
| `error` | message_id, code, message, retryable, retry_after, request_id, assistant_message (`failed`) | Terminal |
| `approval_required` | message (`awaiting_approval`), approval | Terminal: the reply waits for the user's decision on an action (Phase 10) |
| `: ping` | — | Every 15 s while the model is quiet |

Exactly one terminal event ends every stream. Events carry an incrementing SSE `id`; within one
stream they cannot repeat, and every event after `message_start` names the assistant message,
so a client ignores events for a message it is not showing. Errors detected before step 6
(authorization, validation, a busy or archived conversation) are ordinary JSON errors with
their HTTP status; errors after it (quota, provider, timeout) are `error` events, because the
messages already exist and the client needs the failed message to show it. There is no
replay after a network interruption: the client reloads the messages, which include the
partial or final reply.

## Context

The model sees NEXA's system instructions followed by the most recent messages of the same
conversation:

| Rule | Value |
| --- | --- |
| Messages | at most 40 (`CHAT_CONTEXT_MAX_MESSAGES`) |
| Tokens | at most 24 000 estimated (`CHAT_CONTEXT_MAX_TOKENS`), and never more than the model's context window minus the 2 048-token reply budget and a 1 000-token margin |
| Estimate | three characters per token, the gateway's estimator |
| Order | newest first until a limit is hit; the question being answered is always included; the window always opens with a user turn (an answer whose question was cut off goes too: providers reject a conversation that starts with the model) |
| Skipped | failed replies (no answer to learn from) |
| Kept | cancelled replies' partial text |
| Too long | a single message larger than the budget fails with `AI_CONTEXT_TOO_LARGE` |

Since Phase 06 the context also carries recalled memories and a rolling summary of the
older messages ([memory-engine.md](memory-engine.md#context)); older messages are still
never deleted, only left out of the window once a summary covers them. The
history comes only from the authorized conversation; nothing from another conversation or
organization can enter it.

## System instructions

`nexa_runtime/chat/prompts.py` holds versioned instructions (`nexa-assistant/v1`). A
conversation records the version it started with, so changing the instructions is a new
version and old conversations keep theirs. Users cannot replace them: user text is always a
`user` turn, and the request schema has no role field. Specialized assistants (later phases)
add versions; prompts move to the database when an admin needs to edit them.

## Usage and cost

Each generation is one gateway call and one usage row (`feature = chat`, `source_type =
message`, `source_id` = the assistant message, plus organization, user, request id, alias,
provider, model, tokens, cost, latency, status). The assistant message copies the tokens,
cost and timings for display and points at the row. A regenerated reply leaves the earlier
row in place: both calls were billed. A replayed `client_message_id` makes no call and no row.
A stream that fails or is cancelled after some output records what the provider reported, or
an estimate from the text that arrived (`usage_source = estimated`); the provider may bill a
little more than was delivered. Failed and cancelled replies carry those numbers and the
link to their row as well, not zeros; only a reply refused before the gateway call was
planned (an unknown model, an exhausted quota) has no row.

PostgreSQL text cannot hold the NUL character. Requests containing it are refused with 422;
NUL in a model's output is dropped before the client sees it, so the stored reply is exactly
what was streamed. Database errors never include statement parameters (`hide_parameters`), so
message text cannot reach a log through one, and the admin shows conversation metadata but
neither message text nor titles (a title is the first line of the first message).

## Observability

Request IDs and the gateway's `ai.request.completed` line work as in Phase 04. The chat
engine adds `chat.context.built` (conversation, message, messages included and dropped,
estimated tokens), `chat.cancel.requested`, and `chat.generation.completed` (conversation,
message, usage event, status, error code, provider, model, tokens, latency, time to first
token, length in characters). Message text is never logged.

## Rate limits

Sending and regenerating are AI calls and use the Phase 04 quotas (requests per minute and
day, tokens per day, requests in flight, per user and per organization). Creating
conversations is limited to 20 per user per minute (`CHAT_CONVERSATIONS_PER_MINUTE`), counted in
Redis or per process without it.

## Known limitations

- A disconnect stops the reply; there is no background completion or stream resumption
  (Phase 18).
- Cross-process `/cancel/` needs Redis; without it only the generation's own process sees it.
- The non-streaming path (`Accept: application/json`) does not notice a client that went away
  (`/cancel/` does stop it). A non-streaming reply stopped this way does not show the tokens
  it used; its usage row (`source_id` = the message) has them. A stop that lands in the
  instant after the model answered, while the usage row is being written, stores that reply
  as `cancelled` without its text, although the call completed and is billed.
- Context is a recent-message window plus, since Phase 06, a rolling summary of what fell
  out of it and recalled memories (memory-engine.md).
- Titles are the first line of the first message, not model-generated.
- Conversations are private; sharing with a team is a later feature (the policy already
  supports team visibility and grants).

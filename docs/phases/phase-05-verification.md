# Phase 05 verification report

The chat engine: private conversations at `/chat`, replies generated through the Phase 04 AI
Gateway and streamed to React with the `nexa.chat.v1` protocol, every message and its usage
stored. Design: [chat-engine.md](../architecture/chat-engine.md); API:
[conversations.md](../api/conversations.md); decision: [ADR-033](../adr/033-conversations-in-runtime.md).

Every number in this report comes from a run on 2026-10-10 against the code being committed;
nothing is carried over from an earlier run.

## Implemented

| Brief section | Status | Where |
| --- | --- | --- |
| 3 Architecture | ✓ | Runtime serves `/conversations`; Core owns the schema, permissions and admin; no service-to-service calls (ADR-033, [chat-engine.md](../architecture/chat-engine.md#how-django-and-fastapi-cooperate)) |
| 4 Database design | ✓ | `conversations`, `messages` (Core migrations `chat/0001`, `0002`): composite tenant key, unique sequence, unique `client_message_id`, cascade on delete, Runtime grants limited to these tables |
| 5 Conversation behaviour | ✓ | Create, list (search, archive filter, cursor pagination by activity), get, rename (empty restores the automatic title), archive, delete; title from the first message |
| 6 Generation lifecycle | ✓ | `pending` → `streaming` → `completed` / `failed` / `cancelled`; user message always kept; partial text flushed every 2 s; exactly one terminal state, written in a shielded task with a fallback write |
| 7 Streaming | ✓ | `nexa.chat.v1` over SSE: `message_start`, `text_delta`, `usage`, `message_complete`, `message_cancelled`, `error`, pings, incrementing ids, `X-NEXA-Stream-Protocol` header |
| 8 Context | ✓ | At most 40 messages and 24 000 estimated tokens within the model's window; newest first; never opens with an assistant turn; failed replies skipped; one conversation only |
| 9 System prompts | ✓ | Versioned server-side instructions (`nexa-assistant/v1`), recorded per conversation; no role field in requests |
| 10 API | ✓ | `POST/GET/PATCH/DELETE /conversations/`, `GET/POST …/messages/`, `…/regenerate/`, `…/cancel/`; streaming or JSON by `Accept` |
| 11 React workspace | ✓ | Sidebar with history, search, active/archived, new conversation; message panel; Markdown with highlighting; copy, regenerate, stop, retry; model selector; composer (Enter sends, Shift+Enter new line, IME-safe); loading, streaming, empty and error states; mobile drawer |
| 12 Frontend architecture | ✓ | `api/conversations.ts`, `api/stream.ts`, `hooks/useChat.ts`, `useConversations.ts`, `chatThread.ts` (pure state helpers), `components/chat/*`, `pages/ChatPage.tsx` (lazy-loaded), `types/chat.ts` |
| 13 Markdown | ✓ | `react-markdown` + GFM + `rehype-highlight`; raw HTML never rendered; unsafe and relative URLs inert; images shown as links (no fetch); code blocks scroll horizontally |
| 14 Security | ✓ | Organization from the token, owner-only policy (404 for everyone else), closed request schemas, model validated server-side, rate limits; NUL refused (422); database errors without parameters; admin without message text or titles |
| 15 Reliability | ✓ | One generation per conversation (claim under row lock, stale claims taken over and their replies marked `GENERATION_INTERRUPTED`); `client_message_id` idempotency; lock order conversation → message; no exactly-once promise across providers |
| 16 Usage | ✓ | One gateway call = one ledger row (`feature = chat`, `source_type = message`), linked both ways; failed and stopped replies carry their tokens and link; replays make no call |
| 18 Observability | ✓ | `chat.context.built`, `chat.cancel.requested`, `chat.generation.completed`, `chat.generation.interrupted`, `chat.finalize_failed`; request ids on messages and usage rows; no message text in logs |
| 19 Documentation | ✓ | [chat-engine.md](../architecture/chat-engine.md), [conversations.md](../api/conversations.md), [chat-testing.md](../development/chat-testing.md), README, ADR-033 |
| 21 Out of scope | ✓ | No RAG, embeddings, vector search, knowledge bases, long-term memory, agents, multi-agent, research or coding agents, workflows |

## Tested

Every row below was run for this report.

| Brief item | Tests |
| --- | --- |
| Conversation creation, listing, rename, delete, archive | `test_chat_api.py::TestConversations` |
| Message persistence and pagination | `test_stream_event_sequence_and_persistence`, `test_messages_are_paginated_oldest_first` |
| Authorization, organization isolation, ownership through payloads, message ids, cancel and regenerate by others | `TestAuthorization` (colleague and outsider on every endpoint, closed schemas, permissions) |
| Model validation | `test_model_is_validated_server_side` |
| Rate limits | `test_creation_is_rate_limited`, `test_quota_refusal_after_the_message_is_stored`, a stop that releases the quota reservation |
| Context construction | `test_chat_context.py` (11), `test_context_is_bounded_and_from_this_conversation_only` |
| Generation success, provider failure, timeout, empty answer | `TestGeneration` |
| Cancellation, client disconnect, partial persistence | `TestDuringGeneration` (stop mid-stream, before the model starts, after it finished, non-streaming and before a non-streaming call; disconnect, also during the final write and before anything was read), `test_sse_response.py` |
| Idempotency, regenerate, retry | `TestRetries`, replay of a reply whose process died |
| Usage tracking | ledger link asserted for completed, failed (streaming and not) and stopped replies |
| Streaming event sequence, chunks, completion, failure | `test_stream_event_sequence_and_persistence`, failure tests, `test_streaming.py` in the gateway |
| Frontend: list, submission, streaming display, stop, retry, empty state, API errors, mobile navigation, accessibility, organization switch, Markdown safety, stream clients | `ChatPage.test.tsx` (23), `ChatRoute.test.tsx` (1), `Markdown.test.tsx` (9), `conversations.test.ts` (13), `ai.test.ts` (11) |

### Results

| Suite | Count | Result |
| --- | --- | --- |
| Python: `packages/nexa-common` | 17 | pass |
| Python: `packages/nexa-ai` | 167 | pass |
| Python: `apps/core` | 125 | pass |
| Python: `apps/runtime` (43 chat API, 11 context, 1 SSE response, plus earlier phases) | 98 | pass |
| Web (Vitest) | 90 | pass |
| End-to-end, chat (`scripts/e2e_chat.py`) | 41 checks | pass |
| End-to-end, AI gateway (`scripts/e2e_ai.py`) | 31 checks | pass |
| End-to-end, sign-up and sign-in (`scripts/e2e_auth.py`) | 23 checks | pass |
| ruff, ruff format, mypy --strict (158 files), ESLint, tsc, Vite build, `makemigrations --check` | — | clean |

The end-to-end scripts ran through the Vite proxy against `pnpm dev` with the project-local
PostgreSQL, no Redis and no provider keys (mock provider). No test calls a paid provider.

## Code reviews

### First review: twelve findings

After the backend was built, an independent review of the chat engine found twelve defects.
All are fixed, each with a regression test. For the seven that depend on timing or ordering
(L2, L4, L6, L7, L8, L9, D1), each fix was reverted in turn and its test confirmed to fail.

| Finding | Fix |
| --- | --- |
| S1: message text could reach logs through SQL statement parameters; NUL characters caused database errors | `hide_parameters` on the engine; NUL refused (422) in requests and dropped from model output |
| S2: the admin showed conversation titles, which are the first line of the first message | The admin and `Conversation.__str__` show metadata only |
| L1: the context window could open with an assistant turn, which Anthropic rejects | The orphaned answer is dropped |
| L2: a client leaving during the final write left the conversation claimed | The final write runs in a shielded task |
| L3: a failed final write sent no terminal event and kept the claim | A fallback write marks the reply failed and releases the claim; the stream still ends with `error` |
| L4: a stop before the model started kept the quota reservation and wrote no usage row | The gateway stream settles when closed unread; the reader task starts eagerly |
| L5: an unexpected error on the non-streaming path kept the claim | The reply fails with `AI_INTERNAL_ERROR` and the claim is released |
| L6: `/cancel/` did not stop non-streaming replies | They register with the cancel registry and race the gateway call against the stop |
| L7: a stale claim taken over left the old reply `pending` forever | It is marked failed with `GENERATION_INTERRUPTED` (also when replayed) |
| L8: finishing a reply locked the message before the conversation (deadlock risk) | The conversation row first, everywhere |
| L9: a stop after the model had finished marked a complete reply cancelled | Ignored once the call is metered |
| D1: failed and stopped replies showed zero tokens and were not linked to the ledger | `usage` before every stream `error`; the gateway stream exposes its call id and usage; `AIError` carries them for `chat()` |

### Second review: the finished phase

The whole uncommitted phase was then reviewed in four areas: web security, stream and state
handling in the web client, accessibility, and regressions from the fixes above. Every finding
was re-checked against the code before it was fixed. Three reviewers first failed on a network
error and were run again; the accessibility findings were verified by hand.

| Finding | Fix |
| --- | --- |
| Switching organization left the previous organization's conversations on screen, and every action on them failed with 404 | One chat workspace per organization; a conversation from the other organization is closed |
| Stop before `message_start` dropped the request, although the send still went through on the server | The client waits for the reply to start and cancels it then; it gives up only after five seconds |
| A stop during the quota reservation (non-streaming) left the reservation held until it expired | A reservation that lands after a cancellation is released and the call recorded as cancelled; a stop before the call skips it |
| A stop while the gateway saved a stream's usage lost the numbers | Usage is set on the stream before the write |
| SSE responses closed only the byte encoder, so a source never read was never settled (it matters for ASGI 2.4 servers) | Responses close their source too; a chat stream closed unread stores its reply as cancelled and releases the conversation |
| The playground showed a cut-off stream as a finished answer | A stream without `message.end` is a network error |
| Archiving the open conversation during a reply hid the Stop button | The composer stays until the reply ends |
| After archiving or deleting, focus fell to the page and the mobile drawer stopped answering Escape | The drawer handles keys and focus at the document level; focus moves to the search field |
| Screen readers never heard that a reply had finished | "Reply ready" is announced |
| Regenerate, Retry and Stop dropped keyboard focus | Focus moves to the composer |
| Error banners widened the page at 360 px | Long request ids wrap |
| If the chat's code bundle failed to load, the whole app went blank | An error boundary offers Reload |

One finding is documented rather than fixed (see the limitations below): a non-streaming
reply stopped in the instant between the model's answer and the end of its usage write.
Fixing it would mean swallowing cancellation inside the gateway, which costs more than this
narrow window is worth.

## Known limitations

| Limitation | Effect |
| --- | --- |
| A client that disconnects stops its reply; no background completion or resumable stream | Reopening shows the reply as stopped with the text that arrived (Phase 18) |
| Cross-process `/cancel/` needs Redis | Without Redis only the generation's own process sees a stop; aborting the request always works |
| Providers have no cancel API | Tokens generated before the provider notices a closed stream are billed; a few may not be shown |
| A non-streaming reply stopped with `/cancel/` shows no token counts | Its usage row (`source_id` = message) has them |
| A non-streaming reply stopped in the instant after the model answered (while its usage is written) | Stored as `cancelled` without its text, although the call completed and is billed |
| The non-streaming path ignores a client that went away | It runs to completion and is billed |
| A recent-message window, not summaries | Long conversations lose their beginning (Phase 06) |
| Titles are the first line of the first message | Model-generated titles are a later improvement |
| If the database is unreachable when a reply finishes | The reply cannot be stored; its claim expires after the AI total timeout plus a minute, then the reply is marked `GENERATION_INTERRUPTED` |
| No browser-level (Playwright) tests; the layout was not checked in a real browser at phone width | Component tests cover behaviour, not visual layout |
| Redis is not installed on this development machine | Quotas and cancellation run in per-process mode locally |

## Deferred

| Item | Phase |
| --- | --- |
| Summaries and long-term memory | 06 |
| Document knowledge, embeddings, retrieval | 07 and later |
| Agents, tools, workflows | 10 and later |
| Shared and team conversations (the policy already supports team visibility) | later |
| Background generation and resumable streams | 18 |
| Image input in chat (the gateway types are ready) | 10 |

## Decisions made during the phase

- Conversations are served by Runtime, not proxied through Django ([ADR-033](../adr/033-conversations-in-runtime.md)): no synchronous Django hop inside a token stream and no service-to-service credential.
- No separate generation table: the assistant message is the generation record; tokens and cost stay in the Phase 04 ledger, linked both ways.
- A replayed `client_message_id` answers with JSON even on the streaming route, so a retry never produces a second model call.
- The gateway now reports `usage` before every stream `error`, and errors raised by `Gateway.chat` carry their usage row's id, so failed replies show what they cost (a Phase 04 protocol change, backwards compatible for clients that ignore `usage`).

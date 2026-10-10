# Testing the chat engine locally

## Run it

```powershell
./scripts/dev-db.ps1 start   # after every reboot
pnpm migrate                 # applies the chat migrations (conversations, messages, grants)
pnpm dev                     # React :5173, Django :8000, FastAPI :8001, Celery worker
```

Open http://localhost:5173/chat, start a conversation and send a message. Without provider
keys every reply comes from the deterministic mock provider; with `AI_ANTHROPIC_API_KEY` or
`AI_OPENAI_API_KEY` in `.env` the real models answer (see
[local-ai-testing.md](local-ai-testing.md)).

## Exercising failure paths

The mock provider's control phrases work in chat messages too (development and test only):

| Type this | You should see |
| --- | --- |
| `[[slow:3000]] tell me a story` | A slow stream: press **Stop**; the reply ends as *stopped* with the text so far |
| `[[fail:timeout]] hi` | The reply fails with "took too long"; **Retry** generates it again (the phrase is in the question, so it fails again; edit nothing, just send a normal message next) |
| `[[fail:stream_after:3]] hi` | Three chunks, then a failure; the partial text stays visible, marked failed |
| `[[fail:content]] hi` | "The AI provider declined to answer"; not retryable |
| `[[fail:rate_limit]] hi` | The provider is busy; retried, then reported with a retry hint |

Other things to try: send twice quickly (the second gets "a reply is already being
generated"), close the tab mid-reply and reopen the conversation (the reply is stored as
stopped), open the same conversation in a second tab and press Stop there (needs Redis to
reach another process; in one `pnpm dev` process it works without), rename to an empty title
(the automatic title comes back), archive and look under archived conversations.

## Scripted walk-through

`uv run python scripts/e2e_chat.py` runs against the running stack through the Vite proxy:
sign-up → conversation → streamed reply → stored history and title → usage row → idempotent
retry → regenerate → provider failure → validation errors → a second user is refused →
archive → delete. The exit code is non-zero if any check failed.

## Automated tests

| Suite | What it covers |
| --- | --- |
| `apps/runtime/tests/test_chat_api.py` | CRUD, search, cursor pagination, archive, model validation, creation rate limit; NUL refused in text fields and dropped from model output; database errors without statement parameters; isolation between users and organizations on every endpoint; closed request schemas and permissions; the `nexa.chat.v1` event sequence, persistence and the usage link (also for failed and stopped replies, streaming and not); message pagination; bounded context from one conversation only; the non-streaming path; provider failure, timeout, mid-stream failure and empty answers; an unexpected error and a failed final write still end the reply and release the conversation; lock order when a reply finishes; quota refusal after storing; idempotent retries, regenerate, retry after failure; stop (including before the model starts, which releases the quota reservation, after the model finished, which changes nothing, and for a non-streaming reply), client disconnect (also during the final write, and before a single event was read), a stop before a non-streaming call starts, one generation at a time, stale-claim takeover and replay of a reply whose process died |
| `apps/runtime/tests/test_sse_response.py` | The SSE response closes the stream it reads from even when the client left before the response started |
| `apps/runtime/tests/test_chat_context.py` | System prompt first and server-side, unknown prompt versions, message and token budgets, small windows, failed and cancelled replies, oversized messages, a window never opening with an assistant turn |
| `apps/core/tests/test_chat_models.py` | Composite tenant key, sequence and idempotency constraints, cascade on delete, conversation permissions in every role, an admin that shows neither message text nor titles |
| `packages/nexa-ai/tests/test_streaming.py`, `test_gateway.py` | Gateway side: `usage` before every stream `error`, the stream's call id and usage, closing an unread stream releases its reservation, a reservation cancelled in flight is released, usage known while it is being saved, errors from `chat()` naming their usage row |
| `apps/web/src/**/*.test.ts(x)` | Conversation list, sending and streaming display, stop (also before the reply starts, and with the grace period running out), retry, empty states, errors, mobile drawer and its focus trap, focus after archive, delete, retry and stop, the completion announcement, archiving during a reply, a fresh workspace per organization, a chat that fails to load, composer keyboard, Markdown safety, the stream clients (a stream cut off before its end is an error) |

`uv run pytest` and `pnpm --filter @nexa/web test` run them. The Runtime tests need the
project-local PostgreSQL; none call a paid provider.

Note on the test client: Starlette's `TestClient` buffers streamed responses, so tests that
act during a stream (stop, disconnect, a second send) call the chat service directly inside
the app's event loop through `client.portal`.

# Testing the AI Gateway locally

## Without provider keys

Nothing to configure. In development and test Runtime registers the mock provider, every
logical model ends with a mock target, and every answer is deterministic text derived from
the prompt. Costs are zero, quotas and the usage ledger work exactly as they do with real
providers.

```powershell
./scripts/dev-db.ps1 start   # after every reboot
pnpm migrate                 # applies migrations and seeds the AI catalog
pnpm dev                     # React :5173, Django :8000, FastAPI :8001, Celery worker
```

Open http://localhost:5173/playground, pick a model, send a prompt. The response panel
streams the answer and shows tokens, cost, latency and time to first token.

Control phrases in the prompt make the mock provider misbehave on purpose, so every error
path can be seen in the playground:

| Phrase | Effect |
| --- | --- |
| `[[fail:rate_limit]]` | Provider 429, retried, then the next model (the mock is usually the last one, so the request ends as `AI_RATE_LIMIT`) |
| `[[fail:timeout]]` | `AI_TIMEOUT` (504) |
| `[[fail:provider]]` | Provider outage, retried twice, `AI_PROVIDER_UNAVAILABLE` (503) |
| `[[fail:auth]]` | Credential fault, `AI_AUTHENTICATION_ERROR` (502) |
| `[[fail:content]]` | `AI_CONTENT_REJECTED` (422) |
| `[[fail:context]]` | `AI_CONTEXT_TOO_LARGE` (413) |
| `[[fail:stream_after:3]]` | Three deltas, then an `error` event in the stream |
| `[[slow:3000]]` | Three seconds before answering (and between chunks), capped at ten |
| `[[length]]` | Answer cut off with `finish_reason: length` |
| `[[bad_json_once]]` | With a response schema: invalid JSON first, valid after the repair turn |

These phrases are honoured only in development and test; the mock provider cannot be
enabled anywhere else.

## With provider keys

Add to `.env` and restart Runtime:

```text
AI_ANTHROPIC_API_KEY=sk-ant-...
AI_OPENAI_API_KEY=sk-...
```

The first target of each logical model then answers (Claude Sonnet 5.5 for `nexa-balanced`,
Claude Haiku 5.5 for `nexa-fast`); the mock stays last in the chain as a fallback in
development. Check prices in the Django admin (AI → AI models → prices) before relying on
cost figures: they were taken from the provider pricing pages on 2026-10-08.

## Quotas without Redis

Without Redis the quotas run in degraded mode: each process enforces half of every limit
(`AI_QUOTA_PROCESS_COUNT=2`) in memory and logs `ai.quota.degraded` once per outage. With
Memurai or Docker Redis running, the real Lua-script path is used. The per-user defaults
(20 requests per minute, 500 per day, 500 000 tokens per day) are easy to hit when scripting
against the stack; raise `AI_USER_REQUESTS_PER_MINUTE` in `.env` if needed.

## Administration

http://localhost:8000/admin/ (create a login with `uv run python apps/core/manage.py
createsuperuser`) shows the AI catalog: models and their price history, logical models and
their fallback order, per-organization policies (AI on/off, limits) and alias grants, and
the usage ledger (read-only). Deactivating a model stops new requests from reaching it
within 30 seconds; the "Deactivate EVERY model of the selected models' providers" action is
the provider kill switch.

## The scripted walk-through

`uv run python scripts/e2e_ai.py` runs against the running stack through the Vite proxy:
register and verify a throwaway user → list models → chat → structured output → policy and
validation errors → provider error paths → streaming (normal and mid-stream failure) →
usage summary. The exit code is non-zero if any check failed.

## Automated tests

| Suite | What it covers |
| --- | --- |
| `packages/nexa-ai/tests` | Types, cost, retries, structured output and its safety rules, routing, the gateway against the mock provider, streaming, SSE encoding, the Anthropic and OpenAI adapters against canned HTTP responses |
| `apps/runtime/tests/test_ai_quota.py` | Reserve and settle, per-user limits across organizations, concurrency, degraded mode |
| `apps/runtime/tests/test_runtime_ai.py` | The HTTP routes with real tokens, the catalog in PostgreSQL, grants per organization, the kill switch, usage rows, streaming, quotas |
| `apps/core/tests/test_ai_models.py` | Seed idempotence, price versions, the append-only trigger, the usage endpoint's scopes and permissions |

`uv run pytest` runs them all; the Runtime integration tests need the project-local
PostgreSQL.

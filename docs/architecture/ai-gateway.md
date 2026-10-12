# AI Gateway

Phase 04. Every call to a language model goes through one library, the NEXA AI Gateway.
Callers name a *logical model*; the Gateway picks a provider and a concrete model, enforces
who may call what and how much, normalizes the response or the stream, meters tokens and
cost, and turns every provider failure into one of a few NEXA errors.

```text
React ──► FastAPI /api/v1/ai/* ──► Gateway ──► Router ──► Provider adapter ──► Provider
                                   │  policy: permission, model access, quota reservation
                                   │  execution: one deadline, retries, fallback
                                   │  metering: usage event, cost, metrics, one log line
                                   └──► normalized response or SSE stream
```

No code outside the adapters imports a provider SDK. The browser never holds a provider
credential and never talks to a provider.

## Where the code lives

| Package | Responsibility | May import |
| --- | --- | --- |
| `packages/nexa-ai` (`nexa_ai`) | Canonical types, errors, provider adapters, router, retry and fallback, cost, structured output, SSE encoding, the mock provider. No web framework, no database, no Redis. | `nexa_common`, provider SDKs |
| `apps/runtime/nexa_runtime/ai` | What needs Runtime's infrastructure: the registry loaded from PostgreSQL, organization policy, quotas in Redis, the usage recorder, the `/api/v1/ai/*` routes. | `nexa_ai`, `nexa_common` |
| `apps/core/nexa_core/ai` | Django models and admin for the catalog, organization policies and the usage ledger; the seed catalog; `GET /api/v1/ai/usage/`. | `nexa_common` |

Phase 01 (section 26) planned `packages/nexa-ai` for exactly this split: the gateway is
framework-free so the AI workers of later phases (embeddings in Phase 08, agents in Phase
11) reuse it without FastAPI. The Phase 04 brief's `app/ai/...` layout inside FastAPI is the
same code one directory lower; the package boundary is the only difference.

## Request flow

```text
POST /api/v1/ai/chat/
  1. Authenticate       Bearer token → JWKS verify → live session (CurrentUser)
  2. Organization       token's active org → membership + permissions from the DB (CurrentPrincipal)
  3. Validate           pydantic ChatRequest (roles, parts, limits, schema shape and safety)
  4. Context            AIRequestContext(request_id, user, org, permissions): server-side values only
  5. Permission         ai.chat, checked inside the Gateway so every caller is checked the same way
  6. Policy             org AI enabled? alias known, active and allowed for this org?
  7. Route              alias → ordered candidates with the needed capabilities and a configured provider
  8. Reserve            requests/minute, requests/day, tokens/day, in-flight slots (Redis, atomic)
  9. Execute            per candidate: retries for retryable errors, next candidate for fallback-eligible ones
 10. Normalize          ChatResponse / stream events
 11. Settle and meter   quota reservation replaced by the real count; usage event; metrics; one log line
 12. Respond            {success, data: ChatResponse} or SSE stream
```

Steps 1 and 2 are the dependencies every Runtime route uses; AI adds nothing to
authentication. Steps 5 to 11 run inside the Gateway, so an agent run in Phase 11 goes through
exactly the same checks as a playground request.

## Canonical types (`nexa_ai.types`)

```text
ChatRequest      model (alias, optional → default alias), messages, temperature?, max_output_tokens?,
                 response_format?, stop?, feature
Feature          api | playground          (closed set; later phases add chat, title, agent and set
                                            it server-side)
Message          role: system | user | assistant | tool; content: str | [Part]
Part             TextPart {type: "text", text}
                 ImagePart, ToolCallPart, ToolResultPart: declared for Phases 05 and 10, refused
                 with AI_INVALID_REQUEST until then
ResponseFormat   {type: "text"} | {type: "json_schema", name, schema}
ChatResponse     id (= the usage event's id), model (alias), provider, provider_model, content,
                 parsed?, finish_reason, usage, latency_ms, attempts, fallback_used
Usage            input_tokens (all input: plain + cached + cache-written), output_tokens,
                 total_tokens, cached_input_tokens, cache_write_input_tokens, cost_usd (string)
FinishReason     stop | length | content_filter | tool_calls | error
Capability       chat, streaming, structured_output, tool_calling, vision, embeddings, reasoning, audio
StreamEvent      MessageStart | MessageDelta | UsageEvent | MessageEnd | StreamError
```

Only `chat`, `streaming` and `structured_output` are exercised in Phase 04. The other
capabilities describe models in the registry so later phases add behaviour without changing
the schema. Identity never comes from the body: a request that names an organization, user,
role or permission fails validation (`extra="forbid"`).

## Provider adapters (`nexa_ai.providers`)

```python
class AIProvider(Protocol):
    name: str  # "anthropic", "openai", "mock"

    async def chat(self, call: ProviderCall) -> ProviderResult: ...
    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]: ...
    async def aclose(self) -> None: ...
```

`ProviderCall` is the canonical request plus the concrete model, resolved limits, timeouts and
an opaque end-user identifier. Adapters translate to and from the provider's API, map its
errors to `nexa_ai.errors`, and nothing else. They use the official async SDKs with the
SDK's own retries switched off (`max_retries=0`), so the Gateway is the only place that
retries, and with the Gateway's timeouts. One client per provider per process, created at
startup and closed at shutdown.

| Adapter | API | Structured output | Notes |
| --- | --- | --- | --- |
| `anthropic` | Messages API, async streaming | Native `output_config.format` JSON-schema mode (schema passed through the SDK's `transform_schema`; a schema it cannot express is `AIUnsupportedByProvider`, so the next provider is tried) | `system` messages become the `system` field. Sampling temperature is not sent: current Claude models run at their default only (`accepts_temperature = false` in the catalog). `stop_reason: refusal` → `content_filter`; `max_tokens` and `model_context_window_exceeded` → `length`. Input tokens = plain + cache read + cache write. An `error` SSE event inside a 200 stream is mapped by its error type. |
| `openai` | Chat Completions, async streaming with `include_usage` | `response_format: json_schema`, `strict: false` (strict mode rejects common schemas; the Gateway validates anyway) | `max_completion_tokens`; `message.refusal` → `content_filter` with empty content; account-level 429s (`insufficient_quota`, spend limits) are credential faults, not rate limits. |
| `mock` | In-process | A document that satisfies the schema | Deterministic answers derived from the prompt; token counts from text length; zero price. |

The mock provider is the only provider in tests and the default on a development machine
without API keys. Runtime registers it only when `nexa_env` is `development` or `test`;
setting `AI_MOCK_PROVIDER=true` anywhere else is refused at startup. Only then are its
control phrases honoured; they make it fail on purpose so every failure path can be
exercised end to end, including from the playground:

```text
[[fail:rate_limit]]  [[fail:timeout]]  [[fail:provider]]  [[fail:auth]]
[[fail:context]]     [[fail:content]]  [[fail:invalid]]   [[fail:model]]
[[fail:stream_after:N]]   fail after N text chunks
[[slow:MS]]               wait MS milliseconds (capped at 10 s)
[[bad_json]] / [[bad_json_once]]   invalid JSON for a structured request (once: valid after repair)
[[length]]                stop with finish_reason=length
```

## Model registry

Core owns the tables; Runtime reads them and caches the snapshot for 30 seconds (one refresh
at a time), so a model disabled in the admin stops being routed within a minute. If the
database is unreachable, the last snapshot is served for at most five minutes, then
requests fail with `AI_PROVIDER_UNAVAILABLE`; after a failed refresh the database is not
asked again for five seconds, so requests do not queue behind connection timeouts.

| Table | Row | Key fields and constraints |
| --- | --- | --- |
| `ai_models` | One concrete provider model | provider, provider_model (unique together), display_name, capabilities[], context_window, max_output_tokens (≤ window), accepts_temperature, is_active |
| `ai_model_prices` | One price version | model, input / output / cached-input / cache-write micro-USD per million tokens, effective_from (unique per model). The price in force at T is the row with the latest `effective_from <= T`; a change is a new row. |
| `ai_model_aliases` | One logical model | name (`nexa-fast`, unique), display_name, description, is_active, is_default (at most one), available_by_default |
| `ai_alias_targets` | One candidate for an alias | alias, model, priority (unique per alias; 1 = preferred, the rest are the fallback chain) |
| `ai_organization_policies` | AI settings for one organization (optional row) | ai_enabled, requests_per_minute, requests_per_day, tokens_per_day, max_output_tokens, concurrent_requests |
| `ai_organization_alias_grants` | Access to an alias that is not available by default | organization, alias (unique together) |
| `ai_usage_events` | One Gateway call (append-only) | see *Usage ledger* |

Logical model names follow the Phase 04 brief: `nexa-fast`, `nexa-balanced` (default),
`nexa-reasoning` (granted per organization), `nexa-vision` (seeded inactive until image input
arrives in Phase 05). They replace the `chat.fast` / `chat.default` names sketched in Phase
01 section 12.2; the semantics are the same, and embeddings will add `nexa-embed` in Phase
08. Provider API keys are never stored in these tables; they come from the environment.

The seed catalog (`nexa_core.ai.catalog`) runs after every migration. It creates rows that
are missing and never overwrites rows an operator has edited. Each alias lists the mock
model as its last target, so a development machine without API keys routes everything to
the mock provider; in staging and production the mock provider is not registered and those
targets are skipped. Kill switches are the `is_active` flags: the admin has a bulk action
that deactivates every model of a provider at once.

Phase 01's per-organization alias overrides (narrowing providers, A/B weights) are not built:
Phase 04 has allow-only grants for restricted aliases. A deny rule would be an additive column.

## Router and fallback

```text
alias ──► targets by priority ──► drop: inactive model, provider not configured in this process,
                                        missing capability (streaming, structured_output, vision),
                                        context window < estimated input + max_output_tokens
                               ──► candidates (ordered)
```

An empty candidate list is `AI_MODEL_UNAVAILABLE` (422) when the request asks for something
the alias cannot do, `AI_CONTEXT_TOO_LARGE` (413) when the input does not fit any target, and
`AI_PROVIDER_UNAVAILABLE` (503) when the alias has no active target or no configured
provider, because that is an operations fault, not the caller's. Execution walks the
candidates under one deadline:

| Error class | Retry same model | Try next candidate |
| --- | --- | --- |
| Rate limited by the provider (429) | yes, after `Retry-After` if ≤ 4 s | yes |
| Provider error or outage (5xx, overloaded, connection dropped) | yes, 2 retries, exponential backoff with full jitter (0.5 s base, 4 s cap) | yes |
| Timeout | no (retrying doubles the wait); the input is metered as an estimate | yes |
| Provider rejected NEXA's credentials or account (bad key, no credit, spend limit) | no | yes, but every other model at the same provider is skipped |
| Unknown model at the provider | no | yes |
| Provider rejected a parameter the Gateway generated (`AIUnsupportedByProvider`) | no | yes, and it is logged as a translation bug |
| Context too large, reported by the provider | no | only candidates with a strictly larger window |
| Invalid request, content rejected | no | no |
| Structured output failed validation | one repair attempt with the validation error | no |

Bounds: at most 5 attempts per call; no new attempt starts with less than 2 seconds of the
deadline left; a `Retry-After` longer than 4 seconds moves to the next candidate instead.

When every candidate has failed, the error reported is the most useful one: a rate limit
(with the shortest `Retry-After`) over a timeout, over a provider outage, over a context
error, over an unknown model, over a credential fault. Credential faults are logged at
warning level with the provider's message because they are configuration problems.

Fallback happens only before the first text reaches the client. Once a stream has delivered
text, a failure is reported as an `error` event and the stream ends; splicing two models'
answers is never acceptable (Phase 01 section 12.4). `attempts` counts provider calls,
including the structured-output repair; `fallback_used` is true when the serving model is
not the first candidate.

## Timeouts

| Timeout | Default | Applies to |
| --- | --- | --- |
| Connect | 5 s | Opening a connection to the provider |
| Request | 120 s | One non-streaming attempt, end to end |
| First token | 45 s | Streaming: from the request until the provider shows any activity (a ping or a thinking block counts) |
| Idle | 30 s | Streaming: the longest gap between two provider events |
| Total | 300 s | A whole Gateway call: every attempt, retry and fallback |

All are settings (`AI_TIMEOUT_*`). The SDK clients get the connect and request timeouts as a
backstop; the Gateway enforces each limit itself with `asyncio.timeout`, reading provider
streams in a separate task so a timeout never has to cancel the provider mid-read. A stuck
provider therefore never holds a request slot for longer than the total timeout.

## Streaming

`POST /api/v1/ai/chat/stream/` answers `text/event-stream`. The SPA reads it with `fetch`
and a small SSE parser (the browser's `EventSource` cannot send an `Authorization` header or
a POST body). This is the Phase 01 decision (ADR-007): one request produces one stream, it
passes through any proxy, auth is the normal header, and cancelling is aborting the request.
WebSockets stay reserved for server-initiated events in later phases.

```text
event: message.start   data: {"id", "model", "provider", "provider_model", "request_id"}
event: message.delta   data: {"text"}
event: usage           data: {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                              "cache_write_input_tokens", "cost_usd"}
event: message.end     data: {"finish_reason", "latency_ms", "attempts", "fallback_used"}
event: error           data: {"code", "message", "retryable", "request_id"}
: ping                 while waiting on the provider, every 15 s
```

The commit point is fixed. Everything up to and including quota reservation (steps 1 to 8)
runs before the response starts, so those failures are ordinary JSON errors with a real HTTP
status (401, 403, 413, 422, 429, 503). The response then starts as 200 `text/event-stream`,
and from that moment every failure, before or after the first token, is an `error` event
with the same NEXA code and message a JSON error would carry, followed by the end of the
stream. The `error` event is preceded by a `usage` event with what the failed call consumed
(tokens the provider accepted, text it produced), so a caller can show and store the cost of
a failure as well as of a success. The HTTP access log shows 200 for a streamed error; the `ai.request.completed` log
line carries the real outcome. `message.start` is sent exactly once, immediately before the
first text of the model that will serve the answer, so fallback before the first token never
produces two starts. Events carry an incrementing `id`; responses set `Cache-Control:
no-cache, no-transform` and `X-Accel-Buffering: no`, and Runtime must never gain a
compressing middleware.

A client disconnect closes the event stream; the Gateway records the call as `cancelled`
with the text seen so far (output tokens estimated, `usage_source = estimated`), settles the
quota and writes the usage row in a task of its own, outside the cancelled request, and
awaits those tasks at shutdown. The non-streaming route does not observe disconnects: an
aborted `POST /ai/chat/` runs to completion and is billed.

In code, `Gateway.open_stream` returns a `GatewayStream`. Its `call_id` (the usage row's id)
is known before the first event, and its `usage` holds the call's final counts once the call
is metered: after the `usage` event, or after the stream was closed early. Every caller must
close it (`aclose()`); closing settles the call however far it got, including a stream that
was opened (quota reserved) but never read, which an ordinary async generator could not do.
`sse_bytes`, the SSE responses (which close the stream itself as well as the encoder) and the
chat engine always close it. The usage of a call is set on the stream before its row is
written, so a reader that stops during the write still has the numbers. An `AIError` raised by the non-streaming
`Gateway.chat` after the call was planned carries the same `call_id` and `usage`, so callers
can link a failed call to its row too.

## Structured output

`response_format: {"type": "json_schema", "name": "...", "schema": {...}}` asks for a JSON
document. The Gateway uses the provider's native mechanism and *always* validates the result
against the caller's schema (draft 2020-12, via `jsonschema`) before returning it in
`parsed`. Structured output is not streamed in Phase 04.

The schema is tenant-supplied, untrusted input. It must describe an object; validators are
built on an empty reference registry and never perform network or file I/O; only local
`#/...` references are accepted and every reference must resolve; `$id`, `$schema`,
`$anchor`, `$dynamicAnchor`, `pattern` and `patternProperties` are refused (Python's regex
engine has no timeout); recursive schemas are refused; size, depth and node count are
capped; `format` stays an annotation. Any of these is `AI_INVALID_REQUEST` at request time,
never a 500.

Repair rules: one failed validation triggers one repair attempt on the same model that
includes the validation error (the error text quotes the model's values, so it is sent only
to the model; logs and error records name the failing keywords without values); a second failure is `AI_OUTPUT_INVALID`. An answer cut off by
`max_output_tokens` is not repaired (asking again cannot fix truncation) and fails at once
with a message naming the limit; a refusal is `AI_CONTENT_REJECTED` without repair. Both
calls' tokens are metered on the one usage row, and the quota reservation for a structured
request includes the repair turn.

## Usage ledger and cost

Every call, including failures, timeouts and cancellations, appends one row to
`ai_usage_events`. Its `id` is the Gateway call ID, returned as `ChatResponse.id` and in
`message.start`, so a message (Phase 05) or an agent step (Phase 11) can point at its row.

```text
id, created_at, organization_id, user_id, actor_type, request_id, source_type, source_id,
feature, alias, provider, provider_model, model_id, price_id,
input_tokens, output_tokens, cached_input_tokens, cache_write_input_tokens, total_tokens,
cost_micros (null when the model has no price), latency_ms, ttft_ms,
status (success | error | cancelled), error_code, attempts, fallback_used, stream, structured,
usage_source (provider | estimated)
```

Tokens are summed over every attempt that cost something: the serving call, the repair
call, and abandoned attempts the provider received (a timed-out attempt is metered as its
estimated input). `usage_source` is `estimated` when any part of the count came from the
Gateway's estimator rather than the provider (cancelled or broken streams, abandoned
attempts). Identifiers are plain UUID columns, not foreign keys, so rows survive the
deletion of an organization, user or model; nothing points into the table, which keeps
monthly partitioning (Phase 18, as for `audit_logs`) possible.

Cost is computed when the row is written, from the price version in force, in micro-dollars
(integer arithmetic, each component rounded half-up):

```text
cost_micros = uncached_input × input_micros_per_million / 1_000_000
            + cached_input   × cached_input_micros_per_million / 1_000_000
            + cache_writes   × cache_write_micros_per_million / 1_000_000
            + output_tokens  × output_micros_per_million / 1_000_000
```

A price change is a new `ai_model_prices` row with a later `effective_from`; existing usage
rows keep pointing at the price they were charged with and are never recomputed. A model
without a price row is routed, records `cost_micros = null` and logs an error on every call,
so the gap is visible rather than silently free. Prompt and completion text are stored
nowhere in Phase 04; the ledger holds metadata only.

Runtime inserts into this table and nothing else: a migration grants `SELECT, INSERT` to the
`nexa_runtime` role (skipped where the role does not exist, as in CI without the init
script). A database trigger refuses `UPDATE` and `DELETE` for every role, the owner included;
the Django model refuses them too. If the insert fails, the Gateway retries three times and
then logs the whole row at error level under `ai.usage.write_failed` so it can be replayed.

## Quotas

Fixed UTC windows in Redis, reserved atomically before the provider is called:

| Limit | Default | Subject |
| --- | --- | --- |
| Requests per minute | 60 per organization, 20 per user | org, user |
| Requests per day | 2 000 per organization, 500 per user | org, user |
| Tokens per day | 2 000 000 per organization, 500 000 per user | org, user |
| Requests in flight | 20 per organization, 4 per user | org, user |
| Output tokens per request | 2 048 by default, 8 192 at most (and never above the model's own limit) | request |

Per-organization limits can be raised or lowered per organization by platform staff in
`ai_organization_policies`; the tenant cannot edit them. Per-user limits are platform
settings keyed by the user alone, so they follow a person across every organization they
belong to: creating more organizations does not buy more AI, which is the ceiling Phase 01's
money budgets would otherwise have provided (ADR-031 records that budgets are deferred).

One Lua script checks every counter and, only if all have room, adds the request and the
call's worst-case tokens (estimated input + `max_output_tokens`, doubled plus one answer for
a structured request because of the possible repair turn) to each. Concurrent requests
therefore cannot jointly overshoot a limit; the residual overrun is the input estimator's
error, and the estimator (three characters per token) errs high. When the call ends, however
it ends, the reservation is replaced by the real count and the in-flight slot is released.
That includes a call cancelled while its reservation is still in flight: the reservation may
land after the cancellation, and is then released and the call recorded as `cancelled`.
Calls NEXA makes on a user's behalf in the background (memory extraction and conversation
summaries, Phase 06; recognized by their server-set `source_type`) count against the token
allowances only, never the request or concurrency limits, so they cannot make the user's own
next message wait or fail.
A breach is `AI_QUOTA_EXCEEDED` (429) with `Retry-After` set to the time until the window
ends: the next minute, or 00:00 UTC for daily limits (two seconds for a busy in-flight slot).
In-flight slots get their expiry when the first one is taken and are never extended by later
requests, so a slot that was never released (a crashed process, a lost settle) frees itself
within the total timeout.

If Redis is unreachable, quotas do not fail open, because an AI call costs money in a way a
login attempt does not. Each process enforces the same limits divided by
`AI_QUOTA_PROCESS_COUNT` in memory and logs `ai.quota.degraded`; a five-second breaker keeps
requests from each paying a Redis timeout. A development machine without Redis runs in
this mode all the time. Workers and agents (Phase 11) will fail closed in degraded mode, as
Phase 01 section 25.2 specifies; the `actor_type` field already carries what is needed.

## Errors

Provider errors are mapped inside the adapters to one hierarchy (`nexa_ai.errors`) whose
classes are `NexaError` subclasses, so the existing exception handlers render them:

| Code | Status | Meaning | Retry | Fallback |
| --- | --- | --- | --- | --- |
| `AI_INVALID_REQUEST` | 422 | The request is malformed or uses an unsupported feature | no | no (yes when the provider rejected a Gateway-generated parameter) |
| `AI_MODEL_UNAVAILABLE` | 422 | Unknown or inactive alias, not allowed for the organization, or missing capability | no | from a provider: yes |
| `AI_CONTEXT_TOO_LARGE` | 413 | The input exceeds the usable context window | no | larger windows only |
| `AI_CONTENT_REJECTED` | 422 | The provider refused the content for safety reasons | no | no |
| `AI_QUOTA_EXCEEDED` | 429 | A NEXA limit for the organization or user was hit | after `Retry-After` | — |
| `AI_RATE_LIMIT` | 429 | Every candidate provider is rate limiting NEXA | after `Retry-After` | yes |
| `AI_TIMEOUT` | 504 | Every candidate timed out | later | yes |
| `AI_AUTHENTICATION_ERROR` | 502 | The provider rejected NEXA's credentials or account (a configuration fault, never the user's) | no | other providers only |
| `AI_PROVIDER_UNAVAILABLE` | 503 | Every candidate failed with a provider-side error, or the alias has no configured target | later | yes |
| `AI_OUTPUT_INVALID` | 502 | Structured output did not match the schema after a repair attempt, or was truncated | no | no |
| `AI_INTERNAL_ERROR` | 500 | A bug in the Gateway or an adapter | no | — |

Messages are safe for end users; the provider's own message, status and request ID go to
the log only, truncated. A response never contains a provider error body, and an unexpected
exception inside an adapter is logged by type name only.

## Security

- Credentials: `AI_ANTHROPIC_API_KEY` and `AI_OPENAI_API_KEY` are read into `SecretStr`
  settings, passed to the SDK client once, and appear in no model, response, log or audit
  row. The log pipeline additionally redacts key-shaped strings and bearer tokens, and never
  renders frame locals in tracebacks, so a bug cannot dump a prompt or a header into the log
  store.
- Identity and tenancy come from the verified token and the database. The request body
  cannot name an organization, a user, a role or a permission; `feature` is a closed enum.
- `ai.chat` gates every route and is re-checked inside the Gateway. Usage for the whole
  organization additionally needs `analytics.view`; everyone sees their own usage, scoped to
  the active organization.
- Model access is decided by the registry and the organization's grants, both platform-owned.
  Limits are platform-owned too.
- Quotas and the usage ledger are keyed by the organization and user from the token, so
  usage can never be attributed to another tenant.
- The response schema is untrusted input and is validated without I/O (see *Structured output*).
- Prompts are not logged. Log lines carry IDs, model names, counts, latencies and codes.
- The mock provider, and its control phrases, exist only in development and test.
- Request bodies are capped at 4 MB by Runtime's middleware, by `Content-Length` and by
  counting bytes as they arrive (chunked bodies included), before FastAPI reads them; text
  parts are capped at 400 000 characters.
- Runtime refuses to start without `NEXA_ENV`, so a deployment cannot fall back to the
  development defaults (mock provider, API docs) by omission.

## Observability

One structured log line per AI call, `ai.request.completed`, with the usage event ID,
request ID, organization, user, feature, alias, provider, model, status, error code,
attempts, fallback_used, stream, input and output tokens, cost, latency, time to first token
and usage source. Failed attempts log `ai.attempt.failed` with the provider's status and a
truncated message. In-process counters (requests by alias, provider, model and outcome;
tokens and cost per model; failed attempts; retries; fallbacks; latency buckets) live in
`GatewayMetrics` for the OpenTelemetry exporter of Phase 17; there is no HTTP metrics route,
because Runtime has no notion of platform staff to protect one with. The field names match
the GenAI attributes from Phase 01 section 23.2.

## Caching

Generated answers are never cached: the same prompt from two users must not share an answer
(tenant walls), and a cached answer is a stale answer. What is cached: the model registry
and organization policies (30 s, in process) and the JWKS. Provider prompt caching of a
stable system prefix arrives with the chat engine in Phase 05, when prompts exist to cache;
the ledger and prices already carry cache-read and cache-write tokens for it.

## API

| Method and path | Served by | Permission | Purpose |
| --- | --- | --- | --- |
| `GET /api/v1/ai/models/` | Runtime | ai.chat | The logical models this organization may use, with capabilities and limits |
| `POST /api/v1/ai/chat/` | Runtime | ai.chat | One completion |
| `POST /api/v1/ai/chat/stream/` | Runtime | ai.chat | The same, streamed as SSE |
| `GET /api/v1/ai/usage/` | Core | ai.chat (`scope=me`), analytics.view (`scope=organization`) | Totals, per-model and per-day breakdowns for the last N days, plus recent events |

`/api/v1/ai/usage` routes to Core and the rest of `/api/v1/ai/` to Runtime, in the Vite proxy
and at the production edge. Phase 01's routing table placed `/models` on Core; `/ai/models`
on Runtime supersedes it, because only Runtime knows which providers are configured. Full
request and response shapes: [docs/api/ai.md](../api/ai.md).

## Frontend

`src/api/ai.ts` is the only module that calls these routes; `src/services/ai/` holds the SSE
parser and the playground state; `src/types/ai.ts` mirrors the API types. The playground at
`/playground` is a development surface: pick a model, send a prompt, watch the stream, read
tokens, cost and latency, and see your own usage. The Phase 05 chat UI replaces it; the SSE
parser is event-name agnostic so Phase 05's turn events reuse it.

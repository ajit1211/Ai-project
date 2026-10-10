# Phase 04 verification report

Date: 2026-10-09. Scope: the AI Gateway (`packages/nexa-ai`), its Runtime wiring and routes,
the Core catalog, policies and usage ledger, the React playground, tests, documentation and
ADRs 026 to 032.

## Checklist (brief section 49)

| Item | Status | Evidence |
| --- | --- | --- |
| AI Gateway | ✓ | `nexa_ai.gateway.Gateway`: permission, policy, routing, quota reservation, execution, metering in one place; re-checks `ai.chat` itself |
| Provider abstraction | ✓ | `nexa_ai.providers.base.AIProvider` (chat, stream, aclose); `ProviderCall`, `ProviderResult`, `ProviderEvent` |
| Model abstraction | ✓ | Logical names `nexa-fast`, `nexa-balanced`, `nexa-reasoning`, `nexa-vision`; application code never names a provider model |
| Model registry | ✓ | `ai_models`, `ai_model_prices`, `ai_model_aliases`, `ai_alias_targets` in Core; Django admin; seed catalog that never overwrites edits; 30 s snapshot in Runtime |
| Model router | ✓ | `nexa_ai.router`: availability, capability and context-window filters in priority order; `test_building_blocks.py::TestRouter` |
| Provider fallback | ✓ | Fallback-eligible errors move to the next candidate, credential faults skip a whole provider, context errors try larger windows only, never after the first token; `TestRetriesAndFallback`, `test_streaming.py` |
| Retry strategy | ✓ | Exponential backoff with full jitter, `Retry-After` honoured up to 4 s, two retries, only for retryable classes; `TestRetryPolicy` |
| Timeouts | ✓ | Connect, request, first-token, idle and total; one deadline per call enforced with `asyncio.timeout`; `test_timeout_is_enforced`, `test_first_token_timeout_falls_back_and_pings`, `test_idle_timeout_after_the_first_token` |
| Normalized requests | ✓ | `ChatRequest` with closed roles, parts, limits and a closed `feature` set; unknown fields and identity fields rejected |
| Normalized responses | ✓ | `ChatResponse` with usage, finish reason, latency, attempts, fallback flag; identical shape for every provider |
| Streaming | ✓ | SSE with `message.start`, `message.delta`, `usage`, `message.end`, `error`, pings and ids; fixed commit point; keep-alives; cancellation recorded |
| Structured output | ✓ | Native provider modes plus gateway-side JSON Schema validation, one repair, truncation and refusal rules, untrusted-schema safety (no I/O, no patterns, no recursion) |
| Token tracking | ✓ | Every call, attempt and cancellation metered; estimates flagged `usage_source = estimated` |
| Cost tracking | ✓ | Integer micro-dollars from the price in force, four token kinds; `TestCost` |
| AI usage database | ✓ | `ai_usage_events`: append-only by Python model and database trigger, Runtime `INSERT` grant, indexes per organization and user |
| Rate limiting | ✓ | Reserve-then-settle quotas (requests/minute, requests/day, tokens/day, in-flight) per organization and per user in Redis via Lua; degraded in-process mode without Redis; `test_ai_quota.py` |
| Authentication | ✓ | Same JWKS-verified tokens and per-request session check as Phase 03; `test_runtime_ai.py::TestAuthentication` |
| Authorization | ✓ | Membership from the database, `ai.chat` at the route and in the gateway, grants per organization, `analytics.view` for organization usage |
| Mock provider | ✓ | Deterministic, scripted failures, control phrases; registered only in development and test (refused elsewhere at startup) |
| Frontend AI client | ✓ | `src/api/ai.ts` (only caller), `src/services/ai/sse.ts` (event-name-agnostic parser), `authorizedFetch` with single-flight refresh for streams |
| AI Playground | ✓ | `/playground`: model select, prompt, system prompt, max tokens, stream toggle, cancel, tokens, cost, latency, time to first token, usage card |
| Tests | ✓ | 339 Python tests, 43 web tests, 31 live end-to-end checks (below) |
| Documentation | ✓ | `docs/architecture/ai-gateway.md`, `docs/api/ai.md`, `docs/development/local-ai-testing.md`, README |
| ADRs | ✓ | [026 to 032](../adr/README.md) |

## Test matrix (brief section 48)

| Area | Where |
| --- | --- |
| Authentication | `test_runtime_ai.py::TestAuthentication`, `test_ai_models.py::TestUsageEndpoint` |
| Authorization | `test_gateway.py::TestPolicy` (permission, AI disabled, grants per organization, foreign grants), `test_runtime_ai.py::TestModels`, usage scopes |
| Model selection | `TestRouter`, `test_model_selection`, `test_lists_models_the_organization_may_use` |
| Provider selection | `test_unconfigured_providers_are_skipped`, `test_kill_switch` |
| Fallback | `test_provider_outage_falls_back`, `test_fallback_without_retry`, `test_account_faults_skip_every_model_at_that_provider`, `test_context_error_falls_back_only_to_larger_windows`, `test_failure_before_the_first_token_falls_back` |
| Retries | `TestRetryPolicy`, `test_rate_limit_is_retried_on_the_same_model`, `test_attempts_are_bounded` |
| Timeout | `test_timeout_is_enforced`, first-token and idle timeout stream tests, `test_timed_out_attempts_are_metered_as_estimates` |
| Streaming | `test_streaming.py` (event order, fallback before first token, error after first token, missing usage, disconnect, cancellation, SSE encoding), `test_runtime_ai.py::TestStream`, adapter stream tests, `sse.test.ts`, `ai.test.ts` |
| Structured output | `TestStructuredOutput` (gateway), `TestUntrustedSchemas`, adapter translation tests, `test_structured_output` over HTTP |
| Token tracking | `test_usage_record_and_cost`, `test_one_repair_attempt`, `test_missing_usage_is_estimated`, adapter usage mapping tests |
| Cost calculation | `TestCost` (formula, cache reads and writes, rounding, conversions), `test_unpriced_models_record_unknown_cost` |
| Rate limiting | `test_ai_quota.py` (reserve/settle, per-user across organizations, concurrency, overrides, degraded mode), `test_quota` over HTTP |
| Organization isolation | `test_grants_do_not_leak_to_other_organizations`, `test_grants_are_per_organization`, `test_scopes_and_permissions` (other organization's rows invisible), identity-in-body rejected |
| Mock provider | Every gateway and route test; `test_providers.py` covers the real adapters against canned responses |
| Frontend AI client | `ai.test.ts` (typed events, JSON errors, 401 refresh and replay, abort), `sse.test.ts`, `PlaygroundPage.test.tsx` |

## Test results

| Suite | Count | Result |
| --- | --- | --- |
| Python: `packages/nexa-common` | 16 | pass |
| Python: `packages/nexa-ai` (types, cost, retry, structured output, router, gateway, streaming, adapters, review regressions) | 159 | pass |
| Python: `apps/core` (Phase 03 suites plus the AI catalog, ledger and usage endpoint) | 128 | pass |
| Python: `apps/runtime` (Phase 03 suites plus quotas, catalog back-off, body cap and the AI routes against PostgreSQL) | 36 | pass |
| Web: API client, SSE parser, AI client, route guards, playground, status | 43 | pass |
| End-to-end through the Vite proxy (`scripts/e2e_ai.py`) | 31 checks | pass |
| End-to-end Phase 03 regression (`scripts/e2e_auth.py`) | 23 checks | pass |
| ruff, ruff format, mypy --strict (139 files), ESLint strict, tsc, Vite build, `makemigrations --check` | — | clean |

The Python suite runs in about a minute; the gateway package alone in under four seconds.

## Live run

With `pnpm dev`, no Redis and no provider keys: register → verify → login → models (two
listed, `nexa-reasoning` withheld) → chat from the mock provider with usage and cost →
structured output → six policy and validation refusals (unknown model, restricted model,
empty messages, identity in the body, no token, unsafe schema) → three provider error paths
(content, timeout, credentials) → a streamed answer with the full event sequence → a
mid-stream failure ending in an `error` event → a JSON error for a bad model on the stream
route → usage summary for `me` and for the organization → logout.

## Design review

Before implementation the design was attacked by three independent reviewers (security,
simplicity, operability) and their findings adversarially re-verified. Changes made as a
result, all reflected in the code and the gateway document: reserve-then-settle quotas
instead of check-then-charge; per-user limits keyed by the user alone; concurrency limits;
the response schema treated as untrusted input (offline validation, no patterns, no
recursion); frame locals and key-shaped strings removed from logs; the mock provider bound
to development and test in code; cache-write tokens in the usage type, prices and ledger;
abandoned attempts metered; one deadline per call with attempt and time floors; a fixed SSE
commit point with exactly one `message.start`; cancelled streams recorded outside the
cancelled task; provider-wide credential faults; context errors falling back to larger
windows only; Anthropic structured output through its native mode (the planned forced tool
call would be rejected by current models); truncated and refused structured answers not
repaired; `/ai/usage` on Core; no HTTP metrics route; a closed `feature` set; the usage
row's id returned to callers.

## Code review

After implementation an independent reviewer read the new backend code against the installed
SDK, Starlette and FastAPI sources and the development database. It confirmed tenant
isolation, secret handling, the schema sandbox, the retry and fallback rules, the streaming
contract, cost accounting, the migration's trigger and grant, and the adapters' use of both
SDKs, and found twelve defects. All twelve are fixed, each with a regression test:

| Finding | Fix |
| --- | --- |
| A valid schema the Anthropic SDK cannot transform (a property without a type) became a 500 with no fallback | Mapped to `AIUnsupportedByProvider`, so the next provider is tried |
| Quota `Retry-After` was the Redis key's expiry (up to two days) | Time until the window ends: next minute or 00:00 UTC |
| In-flight slots had their expiry pushed out by every new request, so a lost release never expired | Expiry set only when the slot counter is created |
| A failing database made every request wait, one at a time, behind a 3 s connection timeout | Five-second back-off; the stale snapshot is served meanwhile |
| Cancelling a request during the final ledger write lost the row and the quota settle | The write runs in a shielded task that shutdown waits for |
| The 4 MB body cap ran after FastAPI had parsed the body, and chunked bodies skipped it | Enforced in the middleware by header and by counting bytes; new `PAYLOAD_TOO_LARGE` code |
| The structured-output repair log line quoted model output | Logs and error records carry only the failing keyword names |
| Closing the SSE byte stream did not close the gateway stream until garbage collection | The encoder closes the inner stream |
| Property names such as `pattern` or `$ref` were refused as keywords | Names and data values are no longer inspected as keywords |
| A stream the provider accepted and then broke was not metered | Its input is counted as an estimate |
| Runtime defaulted to `development` when `NEXA_ENV` was missing, enabling the mock provider | `NEXA_ENV` is required |
| A per-organization limit of 0 meant "use the default"; adapter translation bugs were indistinguishable in logs | 0 is a real limit; attempt logs carry the error class |

## Decisions made during the phase

- The gateway lives in `packages/nexa-ai`, as Phase 01 planned, rather than inside the
  FastAPI app as the brief sketched: later phases' workers reuse it without a web framework.
- Providers are Anthropic (primary) and OpenAI (fallback); the user had not named providers.
  Model IDs and prices were taken from the providers' pages on 2026-10-08 and are flagged in
  the seed catalog as values to verify before relying on cost figures.
- Logical names follow the brief (`nexa-*`), replacing Phase 01's `chat.*` sketch.
- Quotas replace Phase 01's money budgets for this phase (ADR-031); 402 codes are superseded
  by `AI_QUOTA_EXCEEDED` 429.
- `temperature` is accepted but dropped for models that run at their default only, which is
  every current Claude model.
- The brief's ADR-008 to ADR-014 are filed as ADR-026 to ADR-032.

## Known gaps

| Gap | Plan |
| --- | --- |
| No per-alias timeout overrides (reasoning models may want a longer first-token budget) | Catalog columns when Phase 05 measurements call for them |
| The non-streaming route does not observe client disconnects | Documented; the stream route is the one the UI uses |
| Readiness still treats Redis as a hard dependency, although AI quotas degrade gracefully | Revisit the readiness contract in Phase 18 with the rest of the operations work |
| Prices are a snapshot and Haiku's long-prompt tier is not modelled | Prices are rows; add a tier when long prompts matter (Phase 09) |
| No provider-level RPM/TPM token bucket (Phase 01 section 12.5) | Phase 18 when traffic justifies it |
| Image input, tool calls, prompt caching | Phases 05 and 10; the types, capabilities and ledger columns are ready |
| Redis is not installed on this development machine | Quotas run in degraded mode locally; install Memurai from an administrator PowerShell |

# ADR-027: Provider adapters are isolated behind one contract

Status: accepted

## Context

Providers differ in request shape, streaming events, token accounting, structured-output
mechanisms and error semantics, and they change often (both SDKs shipped new major versions
this year, moving to a new HTTP library). None of that may leak into the gateway or the
application.

## Decision

- An adapter implements `AIProvider`: `chat`, `stream`, `aclose`. It translates a
  `ProviderCall` into the provider's request, the provider's answer or stream into NEXA's
  `ProviderResult` and `ProviderEvent`s, and the provider's failures into `nexa_ai.errors`.
  Retries, timeouts beyond the SDK's own, fallback, quotas and metering are not its concern.
- Adapters use the official async SDKs with built-in retries off (`max_retries=0`), so the
  gateway is the single retry authority and total latency stays bounded. One client per
  provider per process; closed at shutdown.
- Error mapping is by the provider's error type and code first, HTTP status second: an
  account fault that arrives as a 429 (`insufficient_quota`) or a 400 (low credit) is a
  credential error, never something to wait on or blame the caller for. A 400 caused by a
  parameter the gateway generated is `AIUnsupportedByProvider`: fallback-eligible and logged
  as a translation bug, not reported as the caller's mistake.
- Token accounting is normalized to NEXA's shape: `input_tokens` is all input (plain, cache
  read, cache write). Anthropic reports the three separately and is summed; OpenAI's prompt
  tokens already include cached tokens.
- The mock provider is an adapter like the others, with deterministic answers and scripted
  failures. Runtime registers it only in development and test; a setting that would enable
  it elsewhere is refused at startup.

## Alternatives

- Raw HTTP clients instead of SDKs: fewer dependencies, but hand-written SSE parsing and
  error decoding for every provider, and a maintenance burden the SDKs already carry.
- Mapping errors by HTTP status only: simpler, and wrong for billing faults and for
  in-stream error events that arrive inside a 200.

## Consequences

- A new provider is one module plus a catalog row: roughly 200 lines, tested against canned
  responses through the SDK's own transport hook.
- SDK upgrades are confined to the adapters; the Phase 04 upgrade to `httpx2`-based SDKs
  touched nothing else.

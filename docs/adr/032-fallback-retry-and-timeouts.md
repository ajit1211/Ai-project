# ADR-032: Fallback, retry and timeout strategy

Status: accepted

## Context

Providers rate-limit, overload, time out and occasionally reject credentials. Users should
get an answer from a second model when the first cannot serve, but never a spliced answer,
never a doubled bill for a timeout, and never a request that holds a worker for minutes.

## Decision

- Errors are classified by the adapter into a small hierarchy with two flags: *retryable*
  (the same model may answer if asked again) and *fallback-eligible* (another candidate may
  answer). Rate limits and provider-side failures are both; timeouts, unknown models,
  credential faults and gateway-generated-parameter rejections are fallback only; invalid
  requests and content refusals are neither. A provider-reported context overflow falls back
  only to candidates with a strictly larger window. A credential or billing fault marks the
  whole provider dead for the rest of the call.
- Retries use exponential backoff with full jitter (0.5 s base, 4 s cap, at most two), honour
  a provider's `Retry-After` up to 4 seconds and otherwise move on.
- One deadline bounds the whole call (`AI_TIMEOUT_TOTAL`, 300 s). Each attempt gets the
  smaller of its own timeout and the time left; no attempt starts with under two seconds
  left; at most five attempts per call. Streaming has separate first-token (45 s) and idle
  (30 s) timeouts; provider pings and thinking blocks count as activity. The non-streaming
  request timeout is 120 s so a long answer from a reasoning model is not cut off.
- Fallback happens only before the first text reaches the client. Afterwards a failure ends
  the stream with an `error` event.
- When every candidate fails, the error reported is the most actionable one: rate limit
  (shortest `Retry-After`), then timeout, then provider outage, then context, then unknown
  model, then credentials; credential faults are logged separately because they are
  configuration problems.
- Abandoned attempts are metered: a timed-out attempt charges its estimated input, because
  the provider received it.

## Alternatives

- Retry everything a fixed number of times: doubles cost on timeouts, retries what cannot
  succeed (credentials, refusals), and multiplies worst-case latency by the candidate count.
- Per-attempt timeouts only: three candidates with retries could hold a request for close to
  ten minutes.
- Mid-stream fallback: the client would receive two models' text in one answer.

## Consequences

- Worst-case latency is the total timeout, regardless of how many candidates an alias has.
- Fallback rate, retry count and attempt failures are counted per provider and model, for
  the dashboards of Phase 17.
- Per-alias overrides (a longer first-token timeout for reasoning models) can be added in
  the catalog when measurements call for them.

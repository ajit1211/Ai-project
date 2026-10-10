# ADR-030: Token streaming over Server-Sent Events with a fixed commit point

Status: accepted (applies Phase 01 ADR-007 to the gateway)

## Context

Users expect to see an answer appear as it is generated. The transport must pass through
CDNs, load balancers and corporate proxies, authenticate with the normal bearer token, and
make cancellation trivial. Errors can happen before the first token (quota, routing, a
provider that fails to start) and after it (a provider dying mid-answer).

## Decision

- `POST /ai/chat/stream/` answers `text/event-stream`, read in the browser with `fetch` and a
  small event-name-agnostic SSE parser. `EventSource` is not used: it cannot send headers or
  a POST body.
- The commit point is the end of planning. Permission, policy, routing and quota reservation
  run before the response starts and fail as JSON errors with real HTTP statuses. Once the
  response starts (200), every failure is one `error` event carrying the same code and
  message a JSON error would, followed by the end of the stream. The access log shows 200
  for streamed errors; the `ai.request.completed` log line carries the outcome.
- `message.start` is sent exactly once, immediately before the first text of the model that
  will answer. Fallback to another model is allowed until then and never afterwards.
- `: ping` comments every 15 seconds keep proxies from closing a quiet stream; events carry
  an incrementing `id`; `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`
  are set; Runtime must never gain a compressing middleware.
- Cancellation is aborting the request. The gateway records the call as cancelled with the
  tokens seen so far and finishes the quota and ledger writes in a task of its own, outside
  the cancelled request, drained at shutdown.

## Alternatives

- WebSockets: connection state, a custom authentication handshake, framing and reconnect
  logic for what is one request with one response. Kept for server-initiated events.
- Returning JSON errors after the first ping: impossible, the status is already sent; the
  earlier draft of this design promised it and could not deliver it.
- Emitting `message.start` when a candidate is chosen: would require a second start after a
  fallback, which clients would have to special-case.

## Consequences

- Clients branch on `Content-Type` once, then on event names. The Phase 05 turn stream wraps
  the same events with turn-level names and reuses the parser.
- Streamed errors are not visible in HTTP status metrics; alerting uses the gateway log line.
- The non-streaming endpoint does not observe disconnects: an aborted request runs to
  completion and is billed.

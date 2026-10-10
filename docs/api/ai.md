# API: AI Gateway

All endpoints live under `/api/v1` and use the envelope described in
[authentication.md](authentication.md). Every AI endpoint needs `Authorization: Bearer
<access token>`, an active membership in the token's active organization (switch with
`POST /auth/switch-organization/`) and the `ai.chat` permission. Identity and organization
always come from the token: the request body cannot name either.

| Method and path | Served by | Permission | Purpose |
| --- | --- | --- | --- |
| GET `/ai/models/` | FastAPI | ai.chat | Logical models this organization may use |
| POST `/ai/chat/` | FastAPI | ai.chat | One completion |
| POST `/ai/chat/stream/` | FastAPI | ai.chat | One completion, streamed as Server-Sent Events |
| GET `/ai/usage/` | Django | ai.chat / analytics.view | Usage and cost summaries |

## Error codes

| Code | Status | Meaning | What to do |
| --- | --- | --- | --- |
| VALIDATION_FAILED | 422 | A field is invalid; see `details` | Fix the request |
| AI_INVALID_REQUEST | 422 | Unsupported feature (images, tools, streamed structured output) or an unsafe response schema | Fix the request |
| AI_MODEL_UNAVAILABLE | 422 | Unknown or inactive model, not enabled for this organization, or it lacks a needed capability | Pick a model from `/ai/models/` |
| AI_CONTEXT_TOO_LARGE | 413 | The input does not fit the model's context window | Shorten the input or lower `max_output_tokens` |
| PAYLOAD_TOO_LARGE | 413 | The request body is over 4 MB | Send less |
| AI_CONTENT_REJECTED | 422 | The provider declined to answer for safety reasons | Do not retry unchanged |
| AI_QUOTA_EXCEEDED | 429 | A NEXA limit for the organization or user | Wait `Retry-After` seconds |
| AI_RATE_LIMIT | 429 | Every provider is rate limiting NEXA | Wait `Retry-After` seconds |
| AI_TIMEOUT | 504 | Every provider timed out | Retry later |
| AI_PROVIDER_UNAVAILABLE | 503 | Providers are down, or no provider is configured for the model | Retry later |
| AI_AUTHENTICATION_ERROR | 502 | NEXA's provider credentials were rejected | Report it; not the user's fault |
| AI_OUTPUT_INVALID | 502 | The structured answer did not match the schema, or was truncated | Simplify the schema or raise `max_output_tokens` |
| AI_INTERNAL_ERROR | 500 | A bug | Report the `request_id` |

The usual `AUTH_*` (401), `PERMISSION_DENIED` (403, also when AI is disabled for the
organization) and `RESOURCE_NOT_FOUND` (404) codes apply as everywhere else. Error messages
never contain provider details.

## GET /ai/models/

```json
{
  "success": true,
  "data": [
    {
      "name": "nexa-balanced",
      "display_name": "NEXA Balanced",
      "description": "The default: strong general answers at a moderate price.",
      "is_default": true,
      "capabilities": ["chat", "streaming", "structured_output"],
      "context_window": 200000,
      "max_output_tokens": 8192
    }
  ]
}
```

Only models with an active, configured target are listed. `capabilities`, `context_window`
and `max_output_tokens` describe what every candidate behind the name guarantees.

## POST /ai/chat/

Request:

```json
{
  "model": "nexa-fast",
  "messages": [
    {"role": "system", "content": "Answer briefly."},
    {"role": "user", "content": "Explain FastAPI dependency injection."}
  ],
  "max_output_tokens": 500,
  "temperature": 0.7,
  "response_format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}},
  "stop": ["END"],
  "feature": "playground"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `model` | no | A name from `/ai/models/`; the default model when omitted |
| `messages` | yes | 1 to 200 messages; roles `system`, `user`, `assistant`; `content` is a string or a list of `{"type": "text", "text": "..."}` parts. At least one non-system message. |
| `max_output_tokens` | no | Default 2 048; capped at 8 192 and at the model's own limit |
| `temperature` | no | 0 to 2. Ignored by models that run at their default only (currently every Claude model) |
| `response_format` | no | `{"type": "text"}` or `{"type": "json_schema", "name"?, "schema"}`. The schema must describe an object, use only local `#/...` references, and may not use `$id`, `$schema`, anchors, `pattern` or `patternProperties`. Not available on the stream endpoint. |
| `stop` | no | Up to 4 stop sequences of 1 to 64 characters |
| `feature` | no | `api` (default) or `playground`. Recorded on the usage row. |

Unknown fields are rejected. Response:

```json
{
  "success": true,
  "data": {
    "id": "0199c6c9-4f44-7b1e-9c58-6d6a0c2b1a77",
    "model": "nexa-fast",
    "provider": "anthropic",
    "provider_model": "claude-haiku-5-5",
    "content": "Dependency injection in FastAPI ...",
    "parsed": null,
    "finish_reason": "stop",
    "usage": {
      "input_tokens": 23,
      "output_tokens": 118,
      "total_tokens": 141,
      "cached_input_tokens": 0,
      "cache_write_input_tokens": 0,
      "cost_usd": "0.000061"
    },
    "latency_ms": 1840,
    "attempts": 1,
    "fallback_used": false
  }
}
```

`id` is also the ID of the usage record. `parsed` holds the validated JSON document when
`response_format` is `json_schema`. `finish_reason` is `stop`, `length` (cut off by
`max_output_tokens`) or `content_filter` (the model declined). `cost_usd` is `null` when the
model has no price configured. `attempts` counts provider calls; `fallback_used` is true when
a model other than the first candidate answered.

## POST /ai/chat/stream/

Same request body (without `response_format`). Errors found before any provider is called
(validation, permission, model, quota) are normal JSON errors with their HTTP status; check
the response `Content-Type`. Otherwise the response is `200 text/event-stream`:

```text
event: message.start
id: 1
data: {"id":"0199c6c9-...","model":"nexa-fast","provider":"anthropic","provider_model":"claude-haiku-5-5","request_id":"req_0199..."}

event: message.delta
id: 2
data: {"text":"Dependency "}

: ping

event: usage
id: 17
data: {"input_tokens":23,"output_tokens":118,"total_tokens":141,"cached_input_tokens":0,"cache_write_input_tokens":0,"cost_usd":"0.000061"}

event: message.end
id: 18
data: {"finish_reason":"stop","latency_ms":1840,"attempts":1,"fallback_used":false}
```

Rules:

- `message.start` arrives exactly once, immediately before the first text of the model that
  will answer. Until then the gateway may still switch to a fallback model.
- `: ping` comment lines keep the connection alive while the provider is thinking; ignore
  them.
- Any failure after the response started, before or after the first text, is one
  `event: usage` with what the failed call consumed, then one `event: error` with
  `{"code", "message", "retryable", "request_id"}`, and then the stream ends. The codes are
  the same as for JSON errors.
- Cancel by aborting the request. The call is recorded as cancelled with the tokens seen so
  far.
- Read the stream with `fetch` and an SSE parser; `EventSource` cannot send the
  `Authorization` header. Browsers treat `event`, `id` and `data` lines as in the SSE
  specification; multi-line `data` is possible.

## GET /ai/usage/

Query parameters: `scope=me` (default; your own calls in the active organization, needs
`ai.chat`) or `scope=organization` (everyone's calls, needs `analytics.view`); `days` 1 to 90
(default 30). Served by Django.

```json
{
  "success": true,
  "data": {
    "scope": "me",
    "organization_id": "0199...",
    "period": {"days": 30, "since": "2026-09-09T10:00:00+00:00"},
    "totals": {"requests": 42, "errors": 1, "input_tokens": 9020, "output_tokens": 15100,
               "total_tokens": 24120, "cost_usd": "0.173400", "unpriced_requests": 0},
    "by_model": [{"alias": "nexa-balanced", "provider": "anthropic", "provider_model": "claude-sonnet-5-5",
                  "requests": 30, "errors": 1, "input_tokens": 7000, "output_tokens": 12000,
                  "total_tokens": 19000, "cost_usd": "0.134000", "unpriced_requests": 0}],
    "by_day": [{"date": "2026-10-08", "requests": 12, "...": "..."}],
    "recent": [{"id": "0199...", "created_at": "...", "user_id": "...", "feature": "playground",
                "alias": "nexa-fast", "provider": "mock", "provider_model": "mock-1",
                "status": "success", "error_code": null, "input_tokens": 12, "output_tokens": 40,
                "total_tokens": 52, "cost_usd": "0.000000", "latency_ms": 310, "ttft_ms": 120,
                "stream": true, "fallback_used": false}]
  }
}
```

`unpriced_requests` counts calls whose model had no price; their cost is not in `cost_usd`.

## Limits

| Limit | Default |
| --- | --- |
| Requests per minute | 60 per organization, 20 per user |
| Requests per day | 2 000 per organization, 500 per user |
| Tokens per day | 2 000 000 per organization, 500 000 per user |
| Requests in flight | 20 per organization, 4 per user |
| Request body | 4 MB; 400 000 characters per text part |

Per-user limits follow the user across organizations. A breach is `AI_QUOTA_EXCEEDED` with a
`Retry-After` header giving the seconds until the window ends (the next minute, or 00:00 UTC
for daily limits). Windows are in UTC.

## Example

```bash
TOKEN=...   # from POST /auth/login/
curl -s http://localhost:5173/api/v1/ai/chat/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Explain Django middleware"}],"feature":"playground"}'

curl -sN http://localhost:5173/api/v1/ai/chat/stream/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"nexa-fast","messages":[{"role":"user","content":"Explain SSE"}]}'
```

Without provider keys the development stack answers from the mock provider; prompts
containing `[[fail:timeout]]`, `[[fail:rate_limit]]`, `[[fail:content]]` and similar phrases
(see `nexa_ai.providers.mock`) exercise the error paths.

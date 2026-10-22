# Agents and tasks API

Phase 11 ([agents.md](../architecture/agents.md), [ADR-039](../adr/039-agent-runs-in-runtime.md)).
Two services, one envelope:

- **Agents** (definitions) are served by Core under `/api/v1/agents/`.
- **Tasks** (runs) are served by Runtime under `/api/v1/runs/`. The architecture document
  (§18.7) names this resource `runs`; the user interface calls them tasks.

Every route needs a verified access token with an active organization; the organization
comes from the token. Responses use the usual envelope (`{"success": true, "data": …}`, errors
`{"success": false, "error": {"code", "message", "details"}}`).

## Errors

| Code | Status | When |
| --- | --- | --- |
| `VALIDATION_FAILED` | 422 | A field is invalid; `details` names it (`input.topic` for task input) |
| `TOOL_NOT_ALLOWED` | 422 | An agent configuration names a tool that is switched off or that its author may not use |
| `PERMISSION_DENIED` | 403 | Missing permission (creating, changing or deleting an agent; running agents) |
| `RESOURCE_NOT_FOUND` | 404 | An agent of another organization; a task that is not yours |
| `AGENT_UNAVAILABLE` | 404 | Submitting to (or resuming with) an agent that is disabled, deleted or not yours to use |
| `RUN_NOT_ACTIVE` | 409 | A control that the task's state does not allow (resume a running task, cancel a finished one) |
| `RUN_LIMIT_REACHED` | 429 | Too many of your (or your organization's) tasks are in progress |
| `RATE_LIMITED` | 429 | Too many submissions this hour |
| `TOOL_APPROVAL_STALE` / `_CLOSED` / `_EXPIRED` | 409 | A decision on a pending action (see [Approvals](#post-runsrun_idapprovalsapproval_id)) |
| `SERVICE_UNAVAILABLE` | 503 | Agents are switched off (`AGENTS_ENABLED=false`) |

## Agents (Core)

### GET /agents/

The agents you can see: the platform's (first) and your organization's. `?enabled=true` lists
only enabled ones. A summary per agent:

```json
{"data": [
  {"id": "0199…", "name": "Document Assistant", "slug": "document-assistant",
   "description": "Answers a question from the documents you can access, …",
   "version": 1, "enabled": true, "is_system": true, "scope": "platform", "model": "",
   "tools": [{"id": "search_documents", "name": "Document search", "risk": "medium",
              "requires_approval": false},
             {"id": "document_metadata", "name": "Document details", "risk": "low",
              "requires_approval": false}],
   "limits": {"max_steps": 8, "max_tool_calls": 6, "max_duration_seconds": 300,
              "max_tokens": 120000},
   "approval_policy": "registry", "has_input_schema": false, "has_output_schema": true,
   "created_by": null, "can_edit": false, "can_delete": false,
   "created_at": "2026-10-22T09:00:00+00:00", "updated_at": "2026-10-22T09:00:00+00:00"}
]}
```

### GET /agents/{agent_id}/

The summary plus `instructions`, `input_schema` and `output_schema`.

### POST /agents/

Create an agent in your organization (`agents.create`). 201 with the detail.

```json
{"name": "Refund helper",
 "description": "Answers refund questions.",
 "instructions": "Answer questions about our refund policy. Search the documents first.",
 "model": "",
 "allowed_tools": ["search_documents"],
 "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}},
                  "required": ["topic"], "additionalProperties": false},
 "output_schema": null,
 "max_steps": 12, "max_tool_calls": 20, "max_duration_seconds": 600, "max_tokens": 200000,
 "approval_policy": "registry", "enabled": true}
```

| Field | Rules |
| --- | --- |
| `name` | 1–80 characters (required); the slug is derived from it (`refund-helper`, then `-2`, …) |
| `description` | ≤ 500 characters |
| `instructions` | 1–20,000 characters (required) |
| `model` | `""` (the organization's default) or an active model available to the organization |
| `allowed_tools` | ≤ 32 tool ids; each must exist, be switched on and be usable by you (`TOOL_NOT_ALLOWED`) |
| `input_schema`, `output_schema` | `null` or a JSON Schema object (local references only) |
| `max_steps` | 1–50 (default 12) |
| `max_tool_calls` | 0–100 (default 20) |
| `max_duration_seconds` | 10–3600 (default 600) |
| `max_tokens` | 1,000–1,000,000 (default 200,000) |
| `approval_policy` | `registry` (tools that require approval ask) or `all_tool_calls` |
| `enabled` | default `true` |

Any other field is refused (`422`): an agent is configuration, never code. Audited as
`AGENT_CREATED`.

### PATCH /agents/{agent_id}/

Change any of the fields above (only those sent). Needs `agents.update` and being the
creator, or `agents.manage_all`; platform agents cannot be changed (403). A change to
behaviour (everything except `name`, `description` and `enabled`) increments `version`; tasks
already submitted keep the configuration they started with. Audited as `AGENT_UPDATED` with
the changed field names.

### DELETE /agents/{agent_id}/

Soft delete (`agents.delete` and creator, or `agents.manage_all`). The agent disappears from
the API and cannot be run; its tasks keep their history. Audited as `AGENT_DELETED`.

## Tasks (Runtime)

### POST /runs/

Submit a task. Needs `ai.chat`, `agents.execute` and `ai.agent.execute`.

```json
{"agent_id": "0199…",
 "goal": "How many days do customers have to request a refund?",
 "input": {},
 "idempotency_key": "6f1c2a0e-3c1b-4c58-9d6f-8e0d2f3a9b11"}
```

| Field | Rules |
| --- | --- |
| `agent_id` | An enabled agent of your organization, or a platform agent |
| `goal` | 1–10,000 characters |
| `input` | An object validated against the agent's `input_schema` (≤ 50,000 characters) |
| `conversation_id` | Optional; one of your conversations (context link only) |
| `idempotency_key` | Optional, `[A-Za-z0-9_.:-]{1,128}`: the same key returns the earlier task (200) instead of starting another |

`202` with the task (`status: "queued"`); a worker picks it up within a second. Audited as
`AGENT_RUN_STARTED` (without the goal).

### The task object

```json
{"data": {
  "id": "0199…", "agent_id": "0199…", "agent_name": "Document Assistant", "agent_version": 1,
  "status": "completed",
  "goal": "How many days do customers have to request a refund?", "input": {},
  "conversation_id": null, "model": "mock-1",
  "progress_message": "Done", "step_count": 2, "tool_call_count": 1,
  "limits": {"max_steps": 8, "max_tool_calls": 6, "max_duration_seconds": 300,
             "max_tokens": 120000},
  "usage": {"model_calls": 2, "input_tokens": 1834, "output_tokens": 96, "cost_usd": "0.000000"},
  "result": "According to the documents: Customers may request a refund within thirty days of purchase. [R1]",
  "output": {"answer": "According to the documents: …", "answered": true},
  "sources": [{"ref": "R1", "document_id": "0199…", "chunk_id": "0199…",
               "title": "handbook.md", "available": true}],
  "failure_reason": null, "error_message": null,
  "cancel_requested": false, "pause_requested": false, "pending_approval": null,
  "can_pause": false, "can_resume": false, "can_cancel": false,
  "created_at": "…", "started_at": "…", "updated_at": "…", "completed_at": "…",
  "deadline_at": "…", "time_left_seconds": null
}}
```

- `status`: `queued`, `running`, `waiting_for_approval`, `paused`, `cancel_requested`,
  `completed`, `failed`, `cancelled`, `timed_out`.
- `failure_reason` (when not completed): `cancelled`, `timeout`, `step_limit`, `token_budget`,
  `loop_detected`, `validation_failed`, `agent_reported`, `permission_denied`,
  `approval_expired`, `provider_error`, `model_unavailable`, `quota_exceeded`, `context_too_large`, `interrupted`,
  `invalid_configuration`, `internal_error`; `error_message` is a safe sentence.
- `usage` is measured from the usage ledger; `cost_usd` is null when a model has no price.
- `sources` are the passages the answer cites, re-checked for you on every read
  (`available: false` when you can no longer open the document).
- `pending_approval`: the action waiting for your decision (shape as in
  [tools.md](tools.md); includes `arguments_hash`).
- `can_pause`, `can_resume`, `can_cancel`: the controls that apply now.
- `deadline_at` while running; while paused or waiting the clock is stopped: `deadline_at` is
  null and `time_left_seconds` says what remains.

### GET /runs/

Your tasks, newest first. Query: `status` (`active`, `finished` or one status), `agent_id`,
`limit` (1–100, default 30), `cursor` (from `meta.next_cursor`).

```json
{"data": [{"id": "…", "status": "completed", "…": "…"}],
 "meta": {"next_cursor": "MjAyNi0x…", "has_more": true}}
```

### GET /runs/{run_id}/

One task (yours; anyone else's is 404).

### GET /runs/{run_id}/steps/

The timeline, by `sequence`. Query: `after` (a sequence number), `limit` (1–500, default 200).

```json
{"data": [
  {"id": "…", "sequence": 1, "kind": "decision", "status": "succeeded",
   "summary": "Searching the documents",
   "detail": {"action": "call_tool", "context_dropped": 0,
              "decision": {"action": "call_tool", "tool": "search_documents",
                           "arguments": {"query": "refund window"},
                           "summary": "Searching the documents"}},
   "tool": null, "error_code": null, "retry_count": 0, "input_tokens": 912, "output_tokens": 41,
   "started_at": "…", "finished_at": "…", "duration_ms": 640, "created_at": "…"},
  {"id": "…", "sequence": 2, "kind": "tool", "status": "succeeded",
   "summary": "Searching documents for “refund window”",
   "detail": {"tool_id": "search_documents", "status": "succeeded", "truncated": false},
   "tool": {"invocation_id": "…", "tool_id": "search_documents", "status": "succeeded",
            "result": {"…": "…"}, "sources": [{"…": "…"}], "approval": null, "…": "…"},
   "…": "…"},
  {"sequence": 3, "kind": "decision", "…": "…"},
  {"sequence": 4, "kind": "final", "status": "succeeded", "summary": "Completed",
   "detail": {"answer_chars": 96, "cited": ["R1"], "invalid_citations": [], "has_output": true},
   "…": "…"}
]}
```

`kind`: `decision` (the model's validated decision; tool arguments redacted per the tool),
`tool` (a tool call; `tool` is the Phase 10 activity record: status, result, sources,
approval), `progress`, `final`, `error` (an invalid decision and why). What the model saw as
an observation is not repeated here: the activity record is the user's view of it.

### GET /runs/{run_id}/events/

Progress as Server-Sent Events (`Accept: text/event-stream`):

```text
event: run
data: {"type": "run", "run": {…the task object…}}

event: step
data: {"type": "step", "step": {…a step…}}

event: end
data: {"type": "end", "run": {…the final task object…}}
```

`run` whenever the task changes, `step` for each new step and again when a step's status
changes (update by `id`), and `end` once the task is final, after which the stream closes.
Comment lines (`: ping`) keep idle connections open. The stream reads from the database, so it
works whichever worker runs the task; it closes after an hour (reconnect to continue).

### POST /runs/{run_id}/pause/ · /resume/ · /cancel/

```json
{"data": {"id": "…", "status": "cancel_requested", "cancel_requested": true,
          "pause_requested": false,
          "message": "Cancelling: the run stops at its next checkpoint."}}
```

| | queued | running | waiting_for_approval | paused | final |
| --- | --- | --- | --- | --- | --- |
| pause | `paused` | `pause_requested` (pauses at the next checkpoint) | 409 | no-op | 409 |
| resume | 409 | 409 | 409 | `queued` (re-checks permissions and the agent) | 409 |
| cancel | `cancelled` | `cancel_requested`, then `cancelled` within seconds | `cancelled` | `cancelled` | 409 |

Audited as `AGENT_RUN_PAUSED`, `AGENT_RUN_RESUMED`, `AGENT_RUN_CANCELLED` (no-ops are not).

### POST /runs/{run_id}/approvals/{approval_id}/

Decide on the action the task is waiting for (the same contract as chat approvals):

```json
{"decision": "approve", "arguments_hash": "<pending_approval.arguments_hash>", "comment": ""}
```

Only the user who started the task; `arguments_hash` must match the pending action
(`409 TOOL_APPROVAL_STALE`); once (`409 TOOL_APPROVAL_CLOSED`); before it expires
(`409 TOOL_APPROVAL_EXPIRED`, and the task fails with `approval_expired`). Returns the task,
back in the queue: on `approve` the engine runs that exact action, on `reject` the agent is
told the user declined. Audited as `TOOL_APPROVAL_DECIDED` with the run id.

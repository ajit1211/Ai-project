# Tools API

Phase 10 ([tools.md](../architecture/tools.md)). Served by Runtime under `/api/v1/tools/`, with
the usual envelope. Every route needs a verified access token and an active membership; the
organization comes from the token. Tools are used inside conversations
([conversations.md](conversations.md#tool-calling)); these routes describe them, switch them on
and off for the organization, and read one invocation record.

## Errors

| Code | Status | When |
| --- | --- | --- |
| `TOOL_NOT_FOUND` | 404 | No such tool |
| `PERMISSION_DENIED` | 403 | Switching a tool needs `ai.tool.manage` |
| `RESOURCE_NOT_FOUND` | 404 | An invocation that does not exist for you (another user's conversation, another organization) |
| `TOOL_APPROVAL_PENDING` | 409 | Sending a message while an action in the conversation awaits your decision |
| `TOOL_APPROVAL_STALE` | 409 | A decision names a different action (`arguments_hash`) than the one pending |
| `TOOL_APPROVAL_EXPIRED` | 409 | The approval expired; the paused reply is closed |
| `TOOL_APPROVAL_CLOSED` | 409 | Already decided, or the reply is no longer paused |

## GET /tools/

Every tool NEXA has, and what the caller can do with it.

```json
{"data": [
  {"id": "calculator", "name": "Calculator", "description": "Evaluate an arithmetic expression …",
   "version": "1", "category": "math", "risk": "low", "read_only": true,
   "requires_approval": false, "enabled": true, "available": true, "disabled_by": null,
   "permissions": ["ai.tool.execute"]},
  {"id": "save_memory", "name": "Save to memory", "…": "…", "risk": "high", "read_only": false,
   "requires_approval": true, "enabled": true, "available": true, "disabled_by": null,
   "permissions": ["ai.tool.execute", "memory.create"]}
]}
```

`enabled`: not switched off for the platform or the organization (`disabled_by` says which).
`available`: enabled and the caller has every permission listed, so a model may be offered it.

## GET /tools/{tool_id}/

The same object with `input_schema`, `output_schema`, `timeout_seconds` and `max_retries`.

## PATCH /tools/{tool_id}/

`{"enabled": false}` switches a tool off for the caller's organization (`true` switches it back
on). Needs `ai.tool.manage` (owners and admins). A tool switched off for the whole platform
(Django admin) stays off. Audited as `TOOL_SETTINGS_CHANGED`. Returns the tool object.

## GET /tools/invocations/{invocation_id}/

One recorded call, for the owner of its conversation only:

```json
{"data": {
  "invocation_id": "0199…", "call_id": "toolu_01…", "tool_id": "search_documents",
  "tool_name": "Document search", "tool_version": "1", "step": 0, "position": 0,
  "status": "succeeded", "summary": "Searching documents for “refund policy”", "risk": "medium",
  "duration_ms": 41, "error_code": null, "error_message": null,
  "result": {"query": "refund policy", "results": [{"ref": "R1", "document_id": "0199…",
             "chunk_id": "6f1e…", "title": "Handbook", "excerpt": "Customers may …"}],
             "searched_documents": 3, "retrieval_mode": "hybrid", "note": null},
  "result_truncated": false, "result_hidden": false,
  "sources": [{"document_id": "0199…", "chunk_id": "6f1e…"}],
  "approval": null,
  "conversation_id": "0199…", "message_id": "0199…",
  "created_at": "…", "completed_at": "…"
}}
```

`status`: `requested`, `validated`, `awaiting_approval`, `approved`, `executing`, `succeeded`,
`rejected`, `failed`, `timed_out`, `cancelled`. `error_code` for rejections: `unknown_tool`,
`tool_disabled`, `permission_denied`, `invalid_arguments`, `arguments_too_large`,
`too_many_calls`, `rate_limited`, `one_action_at_a_time`, `invalid_call`, `duplicate_call`,
`not_approved`, `declined_by_user`; for failures the tool's own code (`division_by_zero`,
`document_unavailable`, …), `timeout`, `invalid_output`, `output_too_large`, `output_budget`,
`tool_error`. `result` is null for failures, and when a document it came from is no longer
readable by you (`result_hidden: true`; only the still-readable references remain in `sources`).
Arguments are not returned.

The same objects appear as `tools` on assistant messages (without `conversation_id`,
`message_id`, `created_at`, `completed_at`).

## Approvals

An action needing approval appears as `approval` on its invocation:

```json
{"id": "0199…", "invocation_id": "0199…", "tool_id": "save_memory", "tool_name": "Save to memory",
 "summary": "Saving to your memories",
 "details": {"action": "Save a memory", "target": "Your private memories",
             "content": "I prefer metric units", "kind": "preference",
             "consequences": "NEXA will use this in your future conversations. …"},
 "risk": "high", "status": "pending", "arguments_hash": "3f1c…(64 hex)",
 "expires_at": "…", "decided_at": null}
```

Decide with `POST /conversations/{conversation_id}/approvals/{approval_id}/`
([conversations.md](conversations.md#approvals)).

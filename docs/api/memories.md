# Memories API

Served by Runtime under `/api/v1/memories/`. Every route needs a bearer token with an active
organization. Responses use the NEXA envelope (`{"success", "data", "message", "meta"}`;
errors `{"success": false, "error": {"code", "message", "details"}, "request_id"}`). Design:
[memory-engine.md](../architecture/memory-engine.md); rules: [memory-security.md](../architecture/memory-security.md).

| Method and path | Permission | Purpose |
| --- | --- | --- |
| GET `/memories/` | memory.view | List memories you can see |
| POST `/memories/` | memory.create | Save a memory |
| GET `/memories/search/` | memory.view | Rank memories by relevance to a query |
| GET `/memories/settings/` | memory.view | Your automatic-memory settings |
| PATCH `/memories/settings/` | memory.update | Change them |
| POST `/memories/clear/` | memory.delete | Delete your private memories |
| GET `/memories/{id}/` | memory.view | One memory |
| PATCH `/memories/{id}/` | memory.update | Edit it |
| DELETE `/memories/{id}/` | memory.delete | Delete it |
| POST `/memories/{id}/approve/` | memory.update | Make a candidate active |
| POST `/memories/{id}/reject/` | memory.update | Reject a candidate |

Every role holds `memory.view/create/update/delete`; owners, admins and managers also hold
`memory.manage_all`, which reaches team and organization memories saved by others, never
anyone's private memories.

## The memory object

```json
{
  "id": "0199...",
  "scope": "user",
  "kind": "preference",
  "key": "preferred_language",
  "content": "Prefers Python for scripts",
  "value": null,
  "source_type": "explicit",
  "confidence": 0.9,
  "importance": 3,
  "status": "active",
  "owner_id": "0199...",
  "team_id": null,
  "conversation_id": "0199...",
  "source_message_id": "0199...",
  "superseded_by_id": null,
  "created_at": "2026-10-12T08:00:00+00:00",
  "updated_at": "2026-10-12T08:00:00+00:00",
  "last_accessed_at": "2026-10-12T09:30:00+00:00",
  "expires_at": null,
  "editable": true
}
```

- `scope`: `conversation`, `user` (private), `team`, `organization`.
- `kind`: `preference`, `fact`, `decision`, `episode` (what an earlier conversation was about).
- `source_type`: `explicit` (you said it), `inferred` (the model concluded it), `manual`
  (typed in), `summary` (from a conversation summary), `assistant` (reserved).
- `status`: `candidate` (waiting for approval), `active`, `superseded` (replaced; see
  `superseded_by_id`), `expired`, `rejected`.
- `key`: an optional subject; one active memory per subject per scope target, so saving a
  new memory with the same key supersedes the old one.
- `editable`: whether you may edit, approve, reject or delete it.

## GET /memories/

Query: `scope`, `kind`, `status` (default: all), `conversation_id`, `q` (words in the text),
`limit` (1 to 100, default 30), `cursor` (from `meta.next_cursor`). Newest first.
`meta: {"next_cursor": "...", "has_more": bool}`. Only memories you can see are listed:
your private ones, your teams', your organization's.

## POST /memories/

```json
{"content": "I prefer short answers", "kind": "preference", "scope": "user", "key": "answer_length", "importance": 4}
```

`content` 1 to 2 000 characters; `scope` defaults to `user`; `team_id` is required for `team`
(a team you belong to), `conversation_id` for `conversation` (one of your conversations);
`organization` needs `memory.manage_all`. `value` is optional JSON (at most 4 KB),
`expires_at` optional. The memory is active at once. Saving the same text again returns the
existing memory. 201 with the memory.

Errors: 422 `VALIDATION_FAILED` for a field error or text that looks like a password, key or
token ("NEXA never stores those"); 404 for a team or conversation that is not yours; 403 for
an organization memory without `memory.manage_all`.

## GET /memories/search/

`q` (required, 1 to 200 characters), `limit` (1 to 50, default 10). Active memories and
candidates you can see that share words with `q`, best first:
`data: [{"memory": Memory, "score": 3.42}]`. The score combines relevance, importance,
confidence, recency and scope (memory-engine.md#retrieval).

## PATCH /memories/{id}/

Any of `content`, `kind`, `key`, `importance`, `value`, `expires_at` (send `null` to clear).
Editing the text makes it yours: `source_type` becomes `manual` and `confidence` 1. 409
`CONFLICT` if the new key belongs to another active memory.

## DELETE /memories/{id}/

Deletes it. 200. The memory is never recalled again.

## POST /memories/{id}/approve/ and /reject/

Only for candidates (409 `CONFLICT` otherwise). Approving makes it active and supersedes the
active memory with the same key, if any. Rejecting keeps it as `rejected`, so the same
proposal is not made again.

## Settings

`GET /memories/settings/` → `{"extraction_enabled": true, "auto_save": true, "retention_days": null}`.
`PATCH` any of them: `extraction_enabled` turns automatic memory from conversations on or
off; `auto_save` lets statements you made yourself be saved without asking (inferences
always ask); `retention_days` (1 to 3 650, or `null`) expires memories unused for that long.

## POST /memories/clear/

Body (optional): `{"scope": "all" | "user" | "conversation" | "candidates", "conversation_id": "..."}`.
Deletes your own private memories of that kind (all by default); team and organization
memories are never cleared here. `data: {"deleted": 7}`.

## In the chat

Sending a message (docs/api/conversations.md) now also recalls your standing preferences,
the memories relevant to the message and the conversation's summary into the model's
context, and after a completed reply extracts candidate memories in the background. Neither
changes the chat API or its stream.

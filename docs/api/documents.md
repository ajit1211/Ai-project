# API: documents

All endpoints live under `/api/v1`, are served by Django (Core) and use the envelope described
in [authentication.md](authentication.md). They need `Authorization: Bearer <access token>`
with an active organization; the organization comes from the token, never from the request.
Architecture and limits: [docs/architecture/documents.md](../architecture/documents.md).

| Method and path | Needs | Purpose |
| --- | --- | --- |
| GET `/documents/` | documents.view | The library: documents you may see, newest first |
| POST `/documents/` | documents.upload | Upload a file (multipart) |
| GET `/documents/{id}/` | view | One document with its processing attempts |
| PATCH `/documents/{id}/` | update | Title, category, sharing, conversation link |
| DELETE `/documents/{id}/` | delete | Delete the document, its file, text and index |
| GET `/documents/{id}/status/` | view | Processing status only (cheap to poll) |
| GET `/documents/{id}/content/` | view | The extracted text, a slice or a page at a time |
| GET `/documents/{id}/preview/` | view | The start of the text as `text/plain` |
| GET `/documents/{id}/download/` | view | The original file, as an attachment |
| POST `/documents/{id}/retry/` | update | Process a failed, unsupported, image-only or stalled document again |
| POST `/documents/{id}/index/` | update | Rebuild the search index (Phase 08) |
| GET `/documents/{id}/index-status/` | view | Search index status (Phase 08) |

"view", "update" and "delete" are the shared policy's actions on the document: the matching
`documents.*` permission plus access to that document (owner, team, organization sharing,
or `documents.manage_all`).

## Error codes

| Code | Status | Meaning |
| --- | --- | --- |
| RESOURCE_NOT_FOUND | 404 | No such document, deleted, in another organization or not shared with you |
| PERMISSION_DENIED | 403 | You can see the document but not change it, or your role cannot upload |
| UNSUPPORTED_MEDIA_TYPE | 415 | Unsupported extension, or content that does not match it |
| PAYLOAD_TOO_LARGE | 413 | Larger than `DOCUMENTS_MAX_UPLOAD_BYTES` |
| QUOTA_EXCEEDED | 402 | The organization's document storage is full |
| VALIDATION_FAILED | 422 | Missing or empty file, invalid field or filter; see `details` |
| CONFLICT | 409 | Retry or re-index not possible in the current state; text not extracted yet |
| RATE_LIMITED | 429 | Too many uploads or re-index requests; see `Retry-After` |
| SERVICE_UNAVAILABLE | 503 | Storage could not be written or read |

## Document

```json
{
  "id": "0199...",
  "filename": "handbook.pdf",
  "title": "Employee handbook",
  "category": "policies",
  "format": "pdf",
  "media_type": "application/pdf",
  "size_bytes": 184320,
  "sha256": "9f2c...",
  "visibility": "organization",
  "team_id": null,
  "conversation_id": null,
  "owner": {"id": "0199...", "email": "ada@example.com"},
  "status": "completed",
  "error_code": null,
  "error_message": null,
  "attempt_count": 1,
  "page_count": 12,
  "word_count": 4210,
  "char_count": 25876,
  "warnings": ["pages_without_text:1"],
  "metadata": {"title": "Employee handbook", "author": "HR", "created": "2026-01-05T09:00:00+00:00"},
  "parser_version": "2026.10.1",
  "created_at": "2026-10-19T08:00:00+00:00",
  "updated_at": "2026-10-19T08:00:04+00:00",
  "processing_started_at": "2026-10-19T08:00:01+00:00",
  "processed_at": "2026-10-19T08:00:03+00:00",
  "has_text": true,
  "can_retry": false,
  "editable": true,
  "deletable": true,
  "index": {
    "status": "indexed", "searchable": true, "stale": false, "chunk_count": 31,
    "token_estimate": 8640, "strategy_version": "structured-chars/v1", "attempt_count": 1,
    "warnings": [], "error_code": null, "error_message": null,
    "requested_at": "...", "started_at": "...", "finished_at": "...", "indexed_at": "..."
  },
  "can_reindex": true
}
```

`status`: `uploaded`, `queued`, `processing`, `completed`, `failed`, `needs_ocr`,
`unsupported`. `GET /documents/{id}/` adds `ingestions`, the attempts newest first:
`{id, attempt, status (also "cancelled"), error_code, error_message, parser_version,
page_count, word_count, char_count, warnings, duration_ms, created_at, started_at,
finished_at}`. `index.status` is `none` before indexing was requested, then `pending`,
`indexing`, `indexed`, `failed`, or `stale` (indexed by an older chunking strategy; still
searchable). No response ever contains the storage key or a file path.

## GET /documents/

Query: `status=`, `format=` (`pdf docx txt md csv`), `owner=me|<user id>`, `category=`,
`team_id=`, `conversation_id=`, `searchable=true|false` (processed and indexed),
`q=` (in the file name, case-insensitive), `limit=` (1 to 100, default 30), `cursor=`.
`meta`: `{"next_cursor": id | null, "has_more": bool, "limits": {"max_upload_bytes": 52428800,
"accepted_extensions": [".csv", ".docx", ".markdown", ".md", ".pdf", ".txt"]}}`. Keyset
pagination: pass `next_cursor` back as `cursor`. The list never carries extracted text.

## POST /documents/

`multipart/form-data` with `file` (required) and optional `title` (≤ 200), `category`
(lowercase slug `^[a-z0-9][a-z0-9_-]*$`, ≤ 32), `visibility` (`private` default, `team`,
`organization`), `team_id` (required with `team`; a team you belong to), `conversation_id`
(one of your conversations). Returns `201` with the document in `queued`.

```bash
curl -X POST http://localhost:5173/api/v1/documents/ \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@handbook.pdf" -F "title=Employee handbook" -F "visibility=organization"
```

## PATCH /documents/{id}/

JSON, every field optional: `{"title", "category", "visibility", "team_id", "conversation_id"}`.
Returns the document. Audited as `DOCUMENT_UPDATED` with the changed field names.

## DELETE /documents/{id}/

Hides the document at once and removes its file, text and search chunks; audited as
`DOCUMENT_DELETED`. Returns `{"data": null}`.

## GET /documents/{id}/status/

`{id, status, error_code, error_message, attempt_count, processing_started_at, processed_at,
updated_at}`. Poll it (every few seconds) while a document is `queued` or `processing`.

## GET /documents/{id}/content/

`?offset=&limit=` (characters; limit 1 to 100 000, default 20 000) or `?page=N` (one PDF
page). Returns `{content, offset, limit, total_chars, has_more, next_offset, pages}` where
`pages` are the page spans `{page, char_start, char_end}` overlapping the slice. 409 before
the document is `completed`; 404 for a page that does not exist.

## GET /documents/{id}/preview/

The first 10 000 characters as `text/plain; charset=utf-8`, `Content-Disposition: inline`,
`X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`. Never HTML.

## GET /documents/{id}/download/

The original bytes with the detected media type, `Content-Disposition: attachment` and
`nosniff`. Audited as `DOCUMENT_DOWNLOADED`.

## POST /documents/{id}/retry/

For `failed`, `needs_ocr`, `unsupported` or a stalled document, within
`DOCUMENTS_MAX_ATTEMPTS`. Returns `202` with the document `queued`; 409 otherwise.

## POST /documents/{id}/index/ and GET /documents/{id}/index-status/

Phase 08. `POST` rebuilds the search index of a `completed` document (409 otherwise; rate
limited per user, `RAG_INDEX_REQUESTS_PER_HOUR`) and returns `202` with the `index` object;
audited as `DOCUMENT_INDEX_REQUESTED`. `GET` returns the `index` object. See
[rag.md](../architecture/rag.md#indexing).

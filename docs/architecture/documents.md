# Documents

Phase 07. Members upload files; NEXA stores them privately, extracts their text in the
background, and shows the library, each document's status and its text. Phase 08 adds a search
index over that text for document-grounded answers ([rag.md](rag.md)). The endpoints are in
[docs/api/documents.md](../api/documents.md); the upload decision is
[ADR-035](../adr/035-document-uploads-through-core.md).

```text
browser ──multipart──► Django /api/v1/documents/
   authenticate ─► organization from the token ─► documents.upload ─► rate limit
   ─► extension allowlist ─► size ─► content signature (the client's type is ignored)
   ─► organization quota ─► sanitized name, server-made key, SHA-256 ─► private storage
   ─► one transaction: document row + first attempt + audit entry
   ─► after commit: core.documents.process ──► Celery worker (queue core.documents)
          claim the attempt ─► copy bytes to a temp file ─► extract ─► store text + outcome
          ─► completed: queue core.documents.index (Phase 08)
```

## Ownership

Django (Core) owns everything here: the tables, the permissions, the upload, the lifecycle,
the APIs, the audit events and the worker. Runtime only reads the search chunks (Phase 08)
and needs no write grant. Documents are never injected into chat automatically, and no
memory is ever created from a document (Phase 06's memory engine skips grounded replies).

| Table | Holds |
| --- | --- |
| `documents` | One row per upload: owner, organization, sharing, name, format, size, SHA-256, server-made storage key, status, counts, parser metadata and warnings |
| `document_ingestions` | One row per processing attempt: status, error, timings, counts |
| `document_texts` | The extracted text and the page spans of a PDF, apart from the document so lists never carry it |
| `document_indexes`, `document_chunks` | The search index (Phase 08, [rag.md](rag.md)) |

Composite foreign keys keep every row in its document's organization (a document's team and
conversation, an attempt, an index row, a chunk), as in Phases 03 to 06.

## Formats

| Format | Library | What is extracted |
| --- | --- | --- |
| PDF | pypdf 6 | Text per page, joined by blank lines, with each page's character span. Pages without text are counted (`pages_without_text:N`); a PDF where no page has text is `needs_ocr` (no OCR is attempted or faked). Encrypted PDFs open only with an empty password; otherwise `failed`/`encrypted`. Over the page limit: `failed`/`too_many_pages`. |
| DOCX | python-docx 1.2 (lxml, entities not resolved) | Paragraphs, headings (as `#` lines), list items (`- `) and tables (pipe tables) in reading order; title, author and dates. Archives with macros are `unsupported`. |
| TXT, MD | standard library | UTF-8 first (a BOM is honoured), UTF-16 with a BOM, then cp1252; replacement characters are reported. Markdown is kept as text. |
| CSV | standard library | Delimiter sniffed among `, ; tab |`, the first non-blank row as header, rows capped, ragged rows padded or trimmed, rendered as a pipe table. Cells starting with `= + - @` get a leading `'` (formula injection). |

All text is normalized: line endings, control characters removed, Unicode NFC, trailing
spaces, at most one blank line in a row. Warnings (`text_truncated`, `encoding_fallback:cp1252`,
`rows_truncated`, `ragged_rows:N`, `formula_cells_neutralized`, `no_text`, ...) are stored with
the document and shown in the UI. Adding a format means one handler in
`apps/core/nexa_core/documents/extraction/` and one entry in its `HANDLERS` table.

## Lifecycle

```text
uploaded ─► queued ─► processing ─► completed
                                 ├► needs_ocr     (image-only PDF)
                                 ├► unsupported   (macros, a format the engine refuses)
                                 └► failed        (corrupted, encrypted, too large, timeout)
failed / needs_ocr / unsupported / a stalled attempt ──retry──► queued (new attempt)
```

`completed` is final: its text never changes, which the search index relies on. Each
attempt is a `document_ingestions` row. The worker claims an attempt with a row lock, so a
redelivered or duplicated task does nothing; a document deleted before or during processing
cancels its attempt and nothing is written. Retries are capped (`DOCUMENTS_MAX_ATTEMPTS`). An
attempt with no progress for twice the processing timeout counts as stalled: it may be
retried, and the purge marks it failed.

## Upload pipeline

1. A verified token with an active organization; the organization comes from the token and
   the permissions from the database. A browser-supplied organization or owner id is never
   used.
2. `documents.upload` (Members and above), and the per-user upload rate limit.
3. The extension must be one of `.pdf .docx .txt .md .markdown .csv`; the size must be above
   zero and within `DOCUMENTS_MAX_UPLOAD_BYTES`; the first bytes must look like that format
   (`%PDF-`, a ZIP with `word/document.xml`, or text without binary bytes). The client's
   content type is ignored, and the stored media type comes from the detected format.
4. The organization's storage quota (`DOCUMENTS_ORG_STORAGE_BYTES`).
5. The display name is sanitized (last path segment, no control characters, at most 255
   characters); the storage key is made by the server: `<organization>/<document><ext>`.
6. The bytes are written to a temporary file in the target folder and renamed into place;
   an existing key is never overwritten. The SHA-256 is computed while validating.
7. One transaction writes the document, its first attempt and the `DOCUMENT_UPLOADED` audit
   entry. If it fails, the stored bytes are removed.
8. Processing is queued after the commit. If the broker cannot be reached, Core processes the
   document itself right away and skips the broker for 30 seconds (as the email flow does),
   so nothing waits forever on a missing Redis.

## Storage

`DocumentStorage` (`save`, `open`, `delete`, `exists`) is the interface. Development and
tests use `LocalDocumentStorage`, a private folder (`DOCUMENTS_STORAGE_ROOT`, default
`.local/documents`, git-ignored). Keys are validated (no `..`, no absolute paths, resolved
inside the root). Files are never served by a static route: reading goes through the
authorized `/download/` and `/preview/` endpoints. An S3-compatible backend implements the
same interface when deployed (`DOCUMENTS_STORAGE_BACKEND`); storage paths, keys and
credentials never appear in an API response or a log.

## Celery

| Task | Queue | Does |
| --- | --- | --- |
| `core.documents.process` | `core.documents` | One processing attempt (soft limit 30 s below `DOCUMENTS_PROCESSING_TIMEOUT_SECONDS`) |
| `core.documents.index` | `core.documents` | Builds the search index (Phase 08) |
| `core.documents.purge` | `core.documents` | Retention: finishes deletions, fails stalled attempts and stalled indexing runs |

The development worker listens on `core.documents` (`pnpm dev`, or
`uv run celery -A nexa_core worker --pool solo --queues core.default,core.email,core.documents`).
Parsing has its own queue so a burst of uploads never delays an email. There is no beat
scheduler yet: run the purge from operations (`celery -A nexa_core call core.documents.purge`),
for example daily.

`compose.yaml` provides PostgreSQL and Redis only; Core, Runtime and the worker run on the
host. In containers, mount a persistent volume at `DOCUMENTS_STORAGE_ROOT` and share it
between the API and the worker, or use the object-store backend.

## Configuration

Environment variables (names as in `.env.example`; Core reads them):

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOCUMENTS_STORAGE_BACKEND` | `local` | Storage implementation |
| `DOCUMENTS_STORAGE_ROOT` | `.local/documents` | Private folder for the local backend |
| `DOCUMENTS_MAX_UPLOAD_BYTES` | 52 428 800 (50 MB) | Largest accepted file |
| `DOCUMENTS_MAX_TEXT_CHARS` | 5 000 000 | Extracted text beyond this is cut (`text_truncated`) |
| `DOCUMENTS_MAX_PAGES` | 500 | PDFs with more pages fail |
| `DOCUMENTS_MAX_CSV_ROWS` | 50 000 | Rows beyond this are dropped (`rows_truncated`) |
| `DOCUMENTS_PROCESSING_TIMEOUT_SECONDS` | 600 | Hard limit per attempt |
| `DOCUMENTS_MAX_ATTEMPTS` | 5 | Processing attempts per document |
| `DOCUMENTS_UPLOADS_PER_HOUR` | 60 | Uploads per user per hour |
| `DOCUMENTS_ORG_STORAGE_BYTES` | 5 368 709 120 (5 GB) | Total stored bytes per organization |
| `DOCUMENTS_PURGE_AFTER_DAYS` | 30 | Days a deleted document's row is kept before the purge removes it |

Fixed limits inside the engine: DOCX archives at most 10 000 entries and 200 MB uncompressed,
and a compression ratio of at most 50 once more than 8 MB is inflated; CSV cells at most
2 000 characters.

## Authorization

| Role | documents.view | upload | update | delete | manage_all |
| --- | --- | --- | --- | --- | --- |
| Viewer | yes | | | | |
| Member | yes | yes | yes (own) | yes (own) | |
| Manager | yes | yes | yes (own) | yes (own) | |
| Admin, Owner | yes | yes | yes | yes | yes |

A document is `private` (its uploader), `team` (members of that team; the uploader must be
in it) or `organization` (every member). Sharing is an explicit change. The shared policy
(`nexa_common.access`) decides every request: another organization's document, or a private
one, is 404 (no existence leak); a visible document the caller may not change is 403.
`documents.manage_all` reaches every document in the organization. Workers act on the
organization and owner stored with the document, never on a session.

## Security

- **Path traversal:** names are display text only; keys are server-made and validated.
- **MIME spoofing:** content signatures decide; the client's content type is ignored.
- **Oversize and bombs:** size limit before storage; page, row, text and archive limits;
  bounded reads; a time limit per attempt.
- **Malformed files:** every parser error becomes `failed` with a fixed message; tracebacks
  are logged by type only and never returned.
- **CSV formula injection:** formula cells are neutralized in the extracted text.
- **XSS:** the preview is `text/plain` with `X-Content-Type-Options: nosniff`; downloads are
  attachments; the web UI renders extracted text as text, never as HTML or Markdown.
- **Untrusted content:** extracted text is data. It is never added to a prompt as
  instructions; grounded answers put it in a delimited evidence block ([rag.md](rag.md)).
- **Logging:** ids, formats, sizes, counts, codes and timings only; never document text,
  file names in the admin, storage keys or credentials. Uploads, changes, retries,
  downloads, re-index requests and deletions are audited.

## Retention

Deleting a document hides it at once, removes its bytes, its text and its search chunks, and
cancels a queued attempt; past citations keep only their label ([rag.md](rag.md#citations)).
The row stays (for the audit trail) until the purge removes it after
`DOCUMENTS_PURGE_AFTER_DAYS`; the purge also retries bytes that could not be removed.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Stuck in `queued` | No worker and the broker is up, so nothing consumed the task. Start the worker with the `core.documents` queue. Without Redis, Core processes inline and this does not happen. A stalled attempt can be retried from the UI. |
| `needs_ocr` | A scanned or image-only PDF; OCR is not part of this phase. |
| `failed` / `encrypted` | A password-protected PDF; upload an unprotected copy. |
| 415 | Wrong extension, or the content does not match it (a renamed file). |
| 413 / 402 / 429 | File too large / organization storage full / too many uploads this hour. |
| Search index `failed` / `no_text` | The document has no extractable text. |

## Tests

```text
uv run pytest apps/core/tests/test_document_extraction.py   # the engine, every format and limit
uv run pytest apps/core/tests/test_documents_api.py         # upload, processing, reading, authorization, worker, storage
uv run python scripts/e2e_documents.py                      # against the running stack
pnpm --filter @nexa/web test                                # the library and detail pages
```

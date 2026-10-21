# Phase 07 verification report

Document intelligence and processing: members upload PDF, Word, text, Markdown and CSV files;
NEXA stores them privately, extracts their text in the background and shows the library,
each document's status, warnings and text. Design: [documents.md](../architecture/documents.md);
API: [documents.md](../api/documents.md); decision: [ADR-035](../adr/035-document-uploads-through-core.md).

Every number below comes from runs on 2026-10-19 to 2026-10-21 against the code being
committed. The backend was committed early by the user (`7a429e7`); this report covers that
commit plus the tests, fixes, web UI and documentation added with Phase 08.

## Step 1: what existed

Reused unchanged: Phase 03 authentication, organizations, teams, roles, the shared access
policy and the audit log; the `documents.*` permissions already in the catalog (one added:
`documents.update`; Members also got `documents.delete`, for their own documents only);
Celery with the broker-down inline fallback of the email flow; the envelope and error
handling. Missing and added: a documents app, storage and upload settings, multipart parsing
(only the upload view accepts it), 413/415/402 errors (`PAYLOAD_TOO_LARGE`,
`UNSUPPORTED_MEDIA_TYPE`, `QUOTA_EXCEEDED`), document audit actions, and web helpers for
uploads and downloads. Deviation from the Phase 01 plan (presigned direct-to-S3 uploads,
processing in Runtime): ADR-035.

## Implemented

| Brief section | Status | Where |
| --- | --- | --- |
| Ownership | ✓ | Core owns records, permissions, upload, quotas, lifecycle, APIs, audit, worker |
| Storage abstraction | ✓ | `DocumentStorage` interface, private `LocalDocumentStorage` (`.local/documents`), server-made validated keys, never in responses or logs |
| Formats | ✓ | PDF (pages, spans, `needs_ocr`, encryption), DOCX (paragraphs, headings, lists, tables in order; macros refused), TXT/MD (UTF-8 first, fallbacks), CSV (delimiter, header, row cap, formula neutralization) |
| Lifecycle | ✓ | `uploaded → queued → processing → completed`, `failed`, `needs_ocr`, `unsupported`, retry, cancellation on delete, stall detection; one `document_ingestions` row per attempt |
| Secure upload | ✓ | Auth, organization from the token, permission, rate limit, extension allowlist, size, content signature (client type ignored), quota, sanitized name, SHA-256, temp-file-then-rename, one transaction, enqueue after commit, inline fallback |
| Extraction rules | ✓ | Normalization, warnings, limits, bounded reads, temp-file cleanup, no content in logs |
| APIs | ✓ | List (filters, keyset pagination), upload, detail, status, content (slice or page), preview, download, retry, patch, delete |
| React | ✓ | `/documents` library (upload button, drag and drop, picker, progress, cancel, icons, status badges, search, filters, pagination, empty/loading/error states, retry, delete with confirmation) and `/documents/:id` (metadata, warnings, attempts, sharing, text viewer with paging and deep links, download, retry, delete) |
| Security | ✓ | Path traversal, MIME spoofing, size and archive bombs, malformed files, CSV formulas, XSS (plain-text preview, `nosniff`, attachments, text-only rendering), cross-tenant 404s |
| Chat and memory | ✓ | Optional conversation link and status only; no automatic injection; no memories from documents |
| Out of scope | ✓ | No RAG, embeddings, semantic search, QA, summarization, agents or OCR in Phase 07 (RAG followed in Phase 08) |

## Tested

| Brief item | Tests |
| --- | --- |
| Models and lifecycle | `test_documents_api.py` (processing, retry, caps, stalled attempts, purge), lifecycle transitions through the worker |
| Upload | Text, PDF, DOCX; client type ignored; spoofed content and unsupported extensions refused; size limit; empty and missing files; sanitized names; quota; rate limit; viewers refused; invalid fields |
| Extraction | `test_document_extraction.py` (26): sniffing, every handler, normalization, encodings, limits, bombs, encryption, image-only PDFs, CSV formulas and ragged rows |
| Authorization | Private documents invisible to colleagues on every route, organization documents readable not editable, team documents, admins manage all, other organizations see nothing (identical 404s), conversation links only to your own conversations, no organization in the token |
| Celery and API | Duplicate tasks, mismatched tasks, deletion before processing, broker down, purge; content slices, plain-text preview, download bytes and headers, filters, search, pagination, update, delete |
| Storage | Keys cannot escape the root; existing files are never overwritten |
| Frontend | 71 new tests: API module (query building, upload errors, download), library (list, empty, client-side rejection, badges, retry, delete confirmation, polling), detail (text rendered literally, page deep links), labels |

### Results

| Suite | Count | Result |
| --- | --- | --- |
| Python: `apps/core/tests/test_document_extraction.py` | 26 | pass |
| Python: `apps/core/tests/test_documents_api.py` | 38 | pass |
| Python: whole suite (all phases, including Phase 08) | 639 | pass |
| Web: Vitest (all phases, including Phase 08) | 189 in 18 files | pass |
| `scripts/e2e_documents.py` against the running stack | 75 checks | 75 pass |
| ruff, ruff format, mypy (strict), ESLint, `tsc --noEmit`, `vite build` | | clean |
| `manage.py makemigrations --check` | | no changes |

The end-to-end run used the user's running Core server, a Runtime on 8001 and the Vite proxy,
with no Redis: processing ran through the inline fallback.

## Found and fixed during the phase

- The DOCX inflation guard rejected ordinary documents (XML compresses more than 10×): the
  ratio rule now applies only once more than 8 MB is inflated.
- A UTF-8 byte-order mark survived as U+FEFF: decoding tries `utf-8-sig` first.
- `GET /documents/?format=md` returned 404: Django REST framework reads `?format=` as a
  renderer choice. With JSON as the only renderer, `URL_FORMAT_OVERRIDE` is now off.
- The purge reported cascaded attempt rows as purged documents; it now counts documents only.

## Known limitations

- No malware scanning; files are never executed or served inline.
- No OCR: image-only PDFs end in `needs_ocr`.
- Uploads pass through the API servers (ADR-035); a reverse proxy must allow 50 MB bodies.
- No Celery beat: `core.documents.purge` must be scheduled by operations.
- Not exercised in a real browser: the web UI is tested in jsdom and the backend end to end.

## Deferred

The S3-compatible storage backend (the interface is ready), presigned uploads, scanning and
OCR.

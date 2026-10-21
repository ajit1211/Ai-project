# ADR-035: Document uploads through Core, private storage behind an interface, processing on a dedicated queue

Status: accepted

## Context

Phase 07 brings document upload and text extraction. The architecture document (section 15,
ADR-015) planned direct-to-S3 uploads with presigned URLs, a malware scan and processing in
Runtime's `ai.ingest` queue. The phase brief instead makes Django authoritative for the
upload itself (authorization, validation by extension and content, quotas, storage, records,
lifecycle) and asks for a storage abstraction that works locally now and with S3-compatible
storage later. The local environment has no object store, often no Redis, and no scanner.

## Decision

1. **Multipart upload to Core** (`POST /api/v1/documents/`). Core authenticates, authorizes,
   validates (allowlisted extension, size, content signature; the client's content type is
   ignored), checks the organization quota, writes the bytes to private storage under a
   server-made key, and records the document, its first attempt and an audit entry in one
   transaction. Uploads above 2 MB are streamed to a temporary file, not held in memory.
2. **`DocumentStorage` interface** with a local, private implementation
   (`.local/documents`, git-ignored, no static route) and an S3-compatible implementation
   selected by `DOCUMENTS_STORAGE_BACKEND` when deployed. Files are read only through
   authorized endpoints; downloads are attachments, previews are `text/plain`.
3. **Extraction in Core's Celery worker** on a dedicated `core.documents` queue with its own
   time limits, queued after commit; when the broker is unreachable, Core processes inline
   and skips the broker for 30 seconds. Attempts are rows (`document_ingestions`) claimed
   with a row lock, so tasks are idempotent.
4. **Sharing reuses the shared policy**: `private`, `team` (the closest thing to a project,
   as in ADR-034) or `organization`, plus `documents.manage_all`.

## Alternatives

- **Presigned direct-to-S3 uploads** (the Phase 01 plan). Keeps large bodies off the API
  servers and is the right shape at scale, but needs an object store in every environment
  and splits validation into an after-the-fact step. Deferred, not rejected: the storage
  interface and the record-first flow allow a "create upload, then confirm" variant later.
- **Processing in Runtime.** Runtime has no worker and no write access to these tables; Core
  owns the documents and already runs Celery.
- **Storing bytes in PostgreSQL.** Simple, but bloats backups and the shared database.

## Consequences

- API servers carry upload bodies (bounded by `DOCUMENTS_MAX_UPLOAD_BYTES`, 50 MB). A
  reverse proxy must allow that body size.
- No malware scanning in this phase; files are never executed or served inline, which limits
  but does not remove the risk. Scanning belongs with the object-store backend.
- A shared volume (or the object-store backend) is needed when the API and the worker run on
  different machines.
- No OCR: image-only PDFs end in `needs_ocr` and are not indexed.

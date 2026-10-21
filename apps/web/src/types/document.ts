/**
 * Types of the documents API (docs/api/documents.md). They mirror the server's shapes field for
 * field; `src/api/documents.ts` is the only caller.
 *
 * The record is called `DocumentRecord`, not `Document`, so it never shadows the DOM's
 * `Document` type in the files that use both.
 */

export type DocumentFormat = "pdf" | "docx" | "txt" | "md" | "csv";

/** Where a document is in text extraction (docs/architecture/documents.md#lifecycle). */
export type DocumentStatus =
  | "uploaded"
  | "queued"
  | "processing"
  | "completed"
  | "failed"
  | "needs_ocr"
  | "unsupported";

/** One processing attempt: the document's states, plus `cancelled` for an abandoned attempt. */
export type IngestionStatus = DocumentStatus | "cancelled";

export type DocumentVisibility = "private" | "team" | "organization";

/**
 * Where the search index stands, apart from extraction. "stale": built by an older chunking
 * strategy, still searchable, rebuilt by a re-index. "none": never indexed.
 */
export type IndexStatus = "none" | "pending" | "indexing" | "indexed" | "failed" | "stale";

/**
 * Where the semantic index (the passages' vectors for the active embedding model) stands.
 * "unavailable": semantic search does not run here (see `reason`). "none": never embedded for
 * the active model. "stale": the vectors are from an older keyword index; new ones are being
 * made.
 */
export type SemanticStatus =
  | "unavailable"
  | "none"
  | "pending"
  | "embedding"
  | "ready"
  | "failed"
  | "stale";

/** The semantic index of one document, part of its `index`. */
export interface SemanticIndex {
  status: SemanticStatus;
  /**
   * Why semantic search does not run here ("disabled", "pgvector_missing", "no_model",
   * "provider_not_configured"); null unless the status is "unavailable".
   */
  reason: string | null;
  /** The logical embedding model's key; null while unavailable. */
  model: string | null;
  /** Whether the document can be found by meaning now. */
  ready: boolean;
  // The fields below are absent while unavailable and for "none".
  chunk_count?: number;
  error_code?: string | null;
  /** Written for the user by the server; set when embedding failed. */
  error_message?: string | null;
  /** Whether embedding again may succeed (false for, say, a configuration problem). */
  retryable?: boolean;
  ready_at?: string | null;
}

/** The search index of one document (also GET /documents/{id}/index-status/). */
export interface DocumentIndex {
  status: IndexStatus;
  /** Whether its passages can be found now (a failed re-index keeps the previous ones). */
  searchable: boolean;
  stale: boolean;
  chunk_count: number;
  token_estimate: number;
  strategy_version: string | null;
  attempt_count: number;
  /** Warning codes such as "chunks_truncated" (see components/documents/labels.ts). */
  warnings: string[];
  error_code: string | null;
  /** Written for the user by the server; set when indexing failed. */
  error_message: string | null;
  requested_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  indexed_at: string | null;
  /** The semantic index, which follows the keyword index with its own status. */
  semantic: SemanticIndex;
}

export interface DocumentOwner {
  id: string;
  email: string;
}

export interface Ingestion {
  id: string;
  attempt: number;
  status: IngestionStatus;
  error_code: string | null;
  error_message: string | null;
  parser_version: string | null;
  page_count: number | null;
  word_count: number;
  char_count: number;
  warnings: string[];
  duration_ms: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface DocumentRecord {
  id: string;
  /** The uploaded file's name, sanitized by the server. */
  filename: string;
  /** "" when none was given. */
  title: string;
  /** A lowercase slug; "" when none. */
  category: string;
  format: DocumentFormat;
  media_type: string;
  size_bytes: number;
  sha256: string;
  visibility: DocumentVisibility;
  team_id: string | null;
  conversation_id: string | null;
  /** Null when the uploader's account is gone. */
  owner: DocumentOwner | null;
  status: DocumentStatus;
  error_code: string | null;
  /** Written for the user by the server; set when processing failed. */
  error_message: string | null;
  attempt_count: number;
  page_count: number | null;
  word_count: number;
  char_count: number;
  /** Parser warning codes such as "pages_without_text:3" (see components/documents/labels.ts). */
  warnings: string[];
  metadata: Record<string, unknown>;
  parser_version: string | null;
  created_at: string;
  updated_at: string;
  processing_started_at: string | null;
  processed_at: string | null;
  /** Whether the content endpoint has text to return. */
  has_text: boolean;
  /** The search index, which has its own status, apart from extraction's. */
  index: DocumentIndex;
  /** These are decided by the server for the caller. */
  can_retry: boolean;
  can_reindex: boolean;
  editable: boolean;
  deletable: boolean;
  /** The processing attempts, newest first; only on GET /documents/{id}/. */
  ingestions?: Ingestion[];
}

/** The light shape polled while a document is processed (GET /documents/{id}/status/). */
export interface DocumentStatusInfo {
  id: string;
  status: DocumentStatus;
  error_code: string | null;
  error_message: string | null;
  attempt_count: number;
  processing_started_at: string | null;
  processed_at: string | null;
  updated_at: string;
}

/** What the server accepts; it checks again whatever the client decided. */
export interface UploadLimits {
  max_upload_bytes: number;
  /** Lowercase, with the dot: ".pdf", ".markdown", ... */
  accepted_extensions: string[];
}

export interface DocumentListMeta {
  next_cursor: string | null;
  has_more: boolean;
  /** Null when the server left them out. */
  limits: UploadLimits | null;
}

export interface DocumentPage {
  data: DocumentRecord[];
  meta: DocumentListMeta;
}

export interface ListDocumentsParams {
  status?: DocumentStatus;
  format?: DocumentFormat;
  /** "me" for the caller's own uploads, or a user id. */
  owner?: string;
  category?: string;
  team_id?: string;
  conversation_id?: string;
  /** Only documents whose passages can (true) or cannot (false) be found by search. */
  searchable?: boolean;
  /** Matches the filename. */
  q?: string;
  cursor?: string;
  limit?: number;
}

/** The optional fields of an upload; empty ones are not sent. */
export interface UploadDocumentFields {
  title?: string;
  category?: string;
  visibility?: DocumentVisibility;
  team_id?: string;
  conversation_id?: string;
}

/** A partial update: absent fields stay as they are. */
export interface UpdateDocumentBody {
  title?: string;
  category?: string;
  visibility?: DocumentVisibility;
  team_id?: string | null;
  conversation_id?: string | null;
}

/** Where one page sits in the extracted text (character offsets, end exclusive). */
export interface PageSpan {
  page: number;
  char_start: number;
  char_end: number;
}

/** A slice of the extracted text (GET /documents/{id}/content/). */
export interface DocumentContent {
  /** Plain text. Untrusted: render it as text, never as HTML or Markdown. */
  content: string;
  offset: number;
  limit: number;
  total_chars: number;
  has_more: boolean;
  next_offset: number | null;
  /** The pages this slice overlaps; exactly one when a page was asked for. */
  pages: PageSpan[];
  /** The page asked for, when one was. */
  page?: number;
}

/** Either a character range (both optional: the server defaults to 0 and 20,000) or one page. */
export type ContentRequest = { offset?: number; limit?: number } | { page: number };

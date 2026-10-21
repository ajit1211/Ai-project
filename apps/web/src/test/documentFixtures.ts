/**
 * Test data for the document pages and API tests: records shaped exactly like the server's
 * (docs/api/documents.md), and a signed-in auth context to render the pages in.
 */

import type { AuthContextValue } from "../auth/AuthContext";
import type {
  DocumentIndex,
  DocumentPage,
  DocumentRecord,
  Ingestion,
  SemanticIndex,
  UploadLimits,
} from "../types/document";

/** A semantic index for the active model: ready unless overridden. */
export function semanticIndex(overrides: Partial<SemanticIndex> = {}): SemanticIndex {
  return {
    status: "ready",
    reason: null,
    model: "nexa-embed-small",
    ready: true,
    chunk_count: 14,
    error_code: null,
    error_message: null,
    retryable: false,
    ready_at: "2026-10-12T08:01:20+00:00",
    ...overrides,
  };
}

/** Never embedded for the active model: the server then sends only these fields. */
export const NOT_EMBEDDED: SemanticIndex = {
  status: "none",
  reason: null,
  model: "nexa-embed-small",
  ready: false,
};

/** Semantic search does not run in this deployment. */
export function semanticUnavailable(reason = "disabled"): SemanticIndex {
  return { status: "unavailable", reason, model: null, ready: false };
}

export function documentIndex(overrides: Partial<DocumentIndex> = {}): DocumentIndex {
  return {
    status: "indexed",
    searchable: true,
    stale: false,
    chunk_count: 14,
    token_estimate: 5200,
    strategy_version: "2026.10.1",
    attempt_count: 1,
    warnings: [],
    error_code: null,
    error_message: null,
    requested_at: "2026-10-12T08:01:00+00:00",
    started_at: "2026-10-12T08:01:02+00:00",
    finished_at: "2026-10-12T08:01:09+00:00",
    indexed_at: "2026-10-12T08:01:09+00:00",
    semantic: semanticIndex(),
    ...overrides,
  };
}

/** No index at all, as for a document whose text is not extracted yet. */
export const NO_INDEX = documentIndex({
  status: "none",
  searchable: false,
  chunk_count: 0,
  token_estimate: 0,
  strategy_version: null,
  attempt_count: 0,
  requested_at: null,
  started_at: null,
  finished_at: null,
  indexed_at: null,
  semantic: NOT_EMBEDDED,
});

export function documentRecord(overrides: Partial<DocumentRecord> = {}): DocumentRecord {
  return {
    id: "d1",
    filename: "q4-report.pdf",
    title: "",
    category: "",
    format: "pdf",
    media_type: "application/pdf",
    size_bytes: 2_516_582,
    sha256: "ab".repeat(32),
    visibility: "private",
    team_id: null,
    conversation_id: null,
    owner: { id: "u1", email: "ada@example.com" },
    status: "completed",
    error_code: null,
    error_message: null,
    attempt_count: 1,
    page_count: 12,
    word_count: 3400,
    char_count: 21_000,
    warnings: [],
    metadata: {},
    parser_version: "2026.10.1",
    created_at: "2026-10-12T08:00:00+00:00",
    updated_at: "2026-10-12T08:00:40+00:00",
    processing_started_at: "2026-10-12T08:00:05+00:00",
    processed_at: "2026-10-12T08:00:40+00:00",
    has_text: true,
    index: documentIndex(),
    can_retry: false,
    can_reindex: true,
    editable: true,
    deletable: true,
    ...overrides,
  };
}

/** A document the server has just accepted: queued, no text, no index. */
export function queuedRecord(overrides: Partial<DocumentRecord> = {}): DocumentRecord {
  return documentRecord({
    status: "queued",
    page_count: null,
    word_count: 0,
    char_count: 0,
    processing_started_at: null,
    processed_at: null,
    updated_at: "2026-10-12T08:00:00+00:00",
    has_text: false,
    index: NO_INDEX,
    can_reindex: false,
    ...overrides,
  });
}

export function ingestion(overrides: Partial<Ingestion> = {}): Ingestion {
  return {
    id: "i1",
    attempt: 1,
    status: "completed",
    error_code: null,
    error_message: null,
    parser_version: "2026.10.1",
    page_count: 12,
    word_count: 3400,
    char_count: 21_000,
    warnings: [],
    duration_ms: 2400,
    created_at: "2026-10-12T08:00:00+00:00",
    started_at: "2026-10-12T08:00:05+00:00",
    finished_at: "2026-10-12T08:00:40+00:00",
    ...overrides,
  };
}

export const LIMITS: UploadLimits = {
  max_upload_bytes: 50 * 1024 * 1024,
  accepted_extensions: [".csv", ".docx", ".markdown", ".md", ".pdf", ".txt"],
};

export function documentPage(
  items: DocumentRecord[],
  meta: Partial<DocumentPage["meta"]> = {},
): DocumentPage {
  return { data: items, meta: { next_cursor: null, has_more: false, limits: LIMITS, ...meta } };
}

const noop = () => Promise.resolve();

export const signedIn: AuthContextValue = {
  status: "authenticated",
  user: {
    id: "u1",
    email: "ada@example.com",
    full_name: "Ada Lovelace",
    is_verified: true,
    created_at: "2026-10-01T08:00:00+00:00",
    profile: { avatar_url: "", timezone: "UTC", locale: "en", preferences: {} },
  },
  organizations: [],
  activeOrganizationId: "o1",
  login: noop,
  logout: noop,
  switchOrganization: noop,
  reloadUser: noop,
  reloadOrganizations: () => Promise.resolve([]),
};

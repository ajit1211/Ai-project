/**
 * The words the document pages use for what the server reports: statuses, formats, parser
 * warnings, processing failures and refused requests. A server message is shown only where
 * the API writes it for users; anything unexpected gets fixed wording (and a request id).
 */

import { ApiError } from "../../api/client";
import type {
  DocumentFormat,
  DocumentStatus,
  DocumentVisibility,
  IndexStatus,
  IngestionStatus,
} from "../../types/document";

/** The badge colours of styles.css: `.badge-up`, `.badge-down`, `.badge-waiting`, `.badge-neutral`. */
export type StatusTone = "up" | "down" | "waiting" | "neutral";

const STATUS_LABELS = {
  uploaded: "Uploaded",
  queued: "Queued",
  processing: "Processing",
  completed: "Ready",
  failed: "Failed",
  needs_ocr: "Needs OCR",
  unsupported: "Unsupported",
  cancelled: "Cancelled",
} satisfies Record<IngestionStatus, string>;

const STATUS_TONES = {
  uploaded: "waiting",
  queued: "waiting",
  processing: "waiting",
  completed: "up",
  failed: "down",
  needs_ocr: "down",
  unsupported: "down",
  cancelled: "neutral",
} satisfies Record<IngestionStatus, StatusTone>;

const IN_FLIGHT: ReadonlySet<string> = new Set<DocumentStatus>(["uploaded", "queued", "processing"]);
const FAILURES: ReadonlySet<string> = new Set<DocumentStatus>(["failed", "needs_ocr", "unsupported"]);

/** The value under `key`, if the table has its own entry for it. */
function lookup<V>(table: Readonly<Record<string, V>>, key: string): V | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

/** Still being processed: the status will change on its own, so it is polled. */
export const isInFlight = (status: string): boolean => IN_FLIGHT.has(status);

/** Processing ended without text. */
export const isFailure = (status: string): boolean => FAILURES.has(status);

/** The label of a status; one this client does not know (a newer server) is shown as sent. */
export function statusLabel(status: string): string {
  return lookup(STATUS_LABELS, status) ?? status;
}

export function statusTone(status: string): StatusTone {
  return lookup(STATUS_TONES, status) ?? "neutral";
}

const INDEX_LABELS = {
  none: "Not indexed",
  pending: "Queued",
  indexing: "Indexing",
  indexed: "Indexed",
  failed: "Failed",
  stale: "Outdated",
} satisfies Record<IndexStatus, string>;

const INDEX_TONES = {
  none: "neutral",
  pending: "waiting",
  indexing: "waiting",
  indexed: "up",
  failed: "down",
  stale: "waiting",
} satisfies Record<IndexStatus, StatusTone>;

const INDEXING: ReadonlySet<string> = new Set<IndexStatus>(["pending", "indexing"]);

/** The search index is being built: its status will change on its own, so it is polled. */
export const isIndexing = (status: string): boolean => INDEXING.has(status);

export function indexLabel(status: string): string {
  return lookup(INDEX_LABELS, status) ?? status;
}

export function indexTone(status: string): StatusTone {
  return lookup(INDEX_TONES, status) ?? "neutral";
}

/** Why indexing failed: the server's message (written for users), else wording for its code. */
export function describeIndexFailure(index: {
  status: string;
  error_code: string | null;
  error_message: string | null;
}): string | null {
  if (index.status !== "failed") return null;
  if (index.error_message) return index.error_message;
  return index.error_code === "timeout"
    ? "Indexing took too long and was stopped."
    : "The document could not be indexed.";
}

const FORMAT_NAMES = {
  pdf: "PDF",
  docx: "Word document",
  txt: "Text",
  md: "Markdown",
  csv: "CSV",
} satisfies Record<DocumentFormat, string>;

export function formatName(format: string): string {
  return lookup(FORMAT_NAMES, format) ?? format.toUpperCase();
}

const VISIBILITY_LABELS = {
  private: "Private",
  team: "Team",
  organization: "Organization",
} satisfies Record<DocumentVisibility, string>;

export function visibilityLabel(visibility: string): string {
  return lookup(VISIBILITY_LABELS, visibility) ?? visibility;
}

/** What the library and the page heading call a document. */
export const displayName = (record: { title: string; filename: string }): string =>
  record.title.trim() || record.filename;

const ENCODINGS: Readonly<Record<string, string>> = {
  "utf-8-sig": "UTF-8 with a byte order mark",
  "utf-16": "UTF-16",
  cp1252: "Windows-1252",
};

function counted(count: number, one: string, many: string): string {
  return count === 1 ? one : many.replace("{n}", count.toLocaleString());
}

/**
 * A parser warning as a sentence. Codes carry an optional detail after a colon
 * ("pages_without_text:3", "encoding_fallback:cp1252"); unknown codes are shown as sent.
 */
export function describeWarning(warning: string): string {
  const colon = warning.indexOf(":");
  const code = colon === -1 ? warning : warning.slice(0, colon);
  const detail = colon === -1 ? "" : warning.slice(colon + 1);
  const count = /^\d+$/.test(detail) ? Number(detail) : null;
  switch (code) {
    case "pages_without_text":
      if (count === null) break;
      return counted(
        count,
        "1 page has no text; it may be a scanned image.",
        "{n} pages have no text; they may be scanned images.",
      );
    case "pages_unreadable":
      if (count === null) break;
      return counted(count, "1 page could not be read.", "{n} pages could not be read.");
    case "ragged_rows":
      if (count === null) break;
      return counted(
        count,
        "1 row has a different number of columns than the header; it was padded or cut to fit.",
        "{n} rows have a different number of columns than the header; they were padded or cut to fit.",
      );
    case "encoding_fallback":
      if (!detail) break;
      if (detail === "cp1252") {
        return "The file is not UTF-8, so it was read as Windows-1252, a best guess: some characters may be wrong.";
      }
      return `The file's text encoding was read as ${lookup(ENCODINGS, detail) ?? detail}.`;
    case "encoding_replaced":
      return "Some characters could not be decoded and were replaced with “�”.";
    case "text_truncated":
      return "The text is longer than NEXA keeps; only the beginning was extracted.";
    case "cell_truncated":
      return "Some table cells were very long and were shortened.";
    case "formula_cells_neutralized":
      return "Cells that looked like spreadsheet formulas are kept as plain text, so they can never run.";
    case "rows_truncated":
      return "The file has more rows than NEXA keeps; the rest were left out.";
    case "table_rows_truncated":
      return "A table has more rows than NEXA keeps; the rest of it was left out.";
    case "chunks_truncated":
      return "The document is longer than NEXA indexes; only its beginning can be found by search.";
    case "no_text":
      return "No text was found in this file.";
    case "no_rows":
      return "The file has a header row but no data rows.";
  }
  return warning;
}

const NEEDS_OCR_TEXT =
  "The PDF has no text layer; it is probably a scan. Text recognition (OCR) is not available yet.";

/** Wording for the processing error codes (the server's `error_code`), when it sent no message. */
const FAILURE_TEXT: Readonly<Record<string, string>> = {
  unsupported_format: "NEXA cannot read this kind of file.",
  type_mismatch: "The file's content does not match its extension.",
  empty_file: "The file is empty.",
  corrupted: "The file is damaged or could not be read.",
  encrypted: "The file is password protected.",
  too_many_pages: "The file has more pages than NEXA processes.",
  archive_too_large: "The file is too large to process.",
  needs_ocr: NEEDS_OCR_TEXT,
  unsupported_content: "NEXA found nothing in this file it can turn into text.",
  extraction_failed: "The file could not be processed.",
  timeout: "Processing took too long and was stopped.",
};

const STATUS_FAILURE_TEXT: Readonly<Record<string, string>> = {
  failed: "Processing failed.",
  needs_ocr: NEEDS_OCR_TEXT,
  unsupported: "NEXA could not extract text from this file.",
};

/**
 * Why processing ended without text, or null while it has not. The server's stored message is
 * written for users; without one, the code (then the status) decides the wording.
 */
export function describeFailure(item: {
  status: string;
  error_code: string | null;
  error_message: string | null;
}): string | null {
  if (!isFailure(item.status)) return null;
  if (item.error_message) return item.error_message;
  const byCode = item.error_code ? lookup(FAILURE_TEXT, item.error_code) : undefined;
  return byCode ?? lookup(STATUS_FAILURE_TEXT, item.status) ?? "Processing failed.";
}

/** Refusals whose message the API writes for the user: wrong type, too large, storage full, invalid, conflicting. */
const USER_WORDED_STATUSES: ReadonlySet<number> = new Set([402, 409, 413, 415, 422]);

const UNREACHABLE_TEXT = "NEXA could not be reached. Check your connection and try again.";

const ERROR_TEXT: Readonly<Record<string, string>> = {
  PERMISSION_DENIED: "You do not have permission to do that.",
  RESOURCE_NOT_FOUND: "This document no longer exists, or you no longer have access to it.",
  SERVICE_UNAVAILABLE: "Document storage is unavailable right now. Try again in a moment.",
};

/** "45 s", or whole minutes above 90 seconds. */
function wait(seconds: number): string {
  return seconds <= 90
    ? `${String(Math.max(1, seconds))} s`
    : `${String(Math.ceil(seconds / 60))} min`;
}

/** A sentence for a failed document request, safe to show whatever the server sent. */
export function describeDocumentError(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "Something went wrong. Check your connection and try again.";
  }
  // Not NEXA's envelope: a proxy answered. A 413 from it still means the file is too large.
  if (error.code === "NETWORK_ERROR") {
    return error.status === 413 ? "The file is too large for the server to accept." : UNREACHABLE_TEXT;
  }
  if (error.code === "VALIDATION_FAILED") {
    const details = error.details.map((detail) => detail.message).filter(Boolean);
    return details.length > 0 ? details.join(" ") : error.message;
  }
  if (USER_WORDED_STATUSES.has(error.status)) return error.message;
  if (error.status === 401) return "Your session has ended. Sign in again to continue.";
  if (error.code === "RATE_LIMITED") {
    return error.retryAfter === null
      ? "Too many requests. Try again in a moment."
      : `Too many requests. Try again in ${wait(error.retryAfter)}.`;
  }
  const fixed = lookup(ERROR_TEXT, error.code);
  if (fixed) return fixed;
  const reference = error.requestId ? ` (ref ${error.requestId})` : "";
  return `Something went wrong on NEXA's side. Try again in a moment.${reference}`;
}

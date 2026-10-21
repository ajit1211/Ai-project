/**
 * Answering from documents in the chat: what the user selected, the `grounding` a send
 * carries for it, and what to say when the server refuses a grounded send. The refusals of
 * these codes carry messages written for users, so they are shown, with what to do next.
 */

import type { ApiError } from "../../api/client";
import type { GroundingRequest } from "../../types/chat";

/** The server's limit on documents per question. */
export const MAX_DOCUMENTS = 20;
/** Questions about documents may be at most this long (ordinary messages may be longer). */
export const MAX_GROUNDED_CHARS = 4000;

export interface PickedDocument {
  id: string;
  name: string;
}

/** What to answer from: some documents, or everything the user can access. */
export type DocumentSelection =
  | { kind: "documents"; documents: PickedDocument[] }
  | { kind: "all" };

export function groundingFor(selection: DocumentSelection): GroundingRequest {
  return selection.kind === "all"
    ? { all_documents: true }
    : { document_ids: selection.documents.map((document) => document.id) };
}

export function selectionSummary(selection: DocumentSelection): string {
  if (selection.kind === "all") return "Answering from all your documents";
  const count = selection.documents.length;
  return count === 1 ? "Answering from 1 document" : `Answering from ${String(count)} documents`;
}

/** The codes of a grounded send's refusals that the composer explains (and the text goes back). */
const COMPOSER_CODES: ReadonlySet<string> = new Set([
  "DOCUMENT_UNAVAILABLE",
  "DOCUMENT_NOT_INDEXED",
  "RAG_INVALID_SCOPE",
  "VALIDATION_FAILED",
  "SERVICE_UNAVAILABLE",
  "RATE_LIMITED",
]);

export function isGroundingRefusal(error: ApiError): boolean {
  return COMPOSER_CODES.has(error.code);
}

/** "45 s", or whole minutes above 90 seconds. */
function wait(seconds: number): string {
  return seconds <= 90 ? `${String(Math.max(1, seconds))} s` : `${String(Math.ceil(seconds / 60))} min`;
}

/** What the composer says about a refused grounded send. */
export function describeGroundingRefusal(error: ApiError): string {
  switch (error.code) {
    case "DOCUMENT_UNAVAILABLE":
      return `${error.message} Choose the documents again.`;
    case "DOCUMENT_NOT_INDEXED":
      return error.message;
    case "RAG_INVALID_SCOPE":
      return error.message;
    case "VALIDATION_FAILED": {
      const tooLong = error.details.find((detail) => detail.field === "content" && detail.code === "too_long");
      if (tooLong) {
        return `Questions about documents are limited to ${MAX_GROUNDED_CHARS.toLocaleString()} characters. Shorten it, or switch to ordinary chat.`;
      }
      const messages = error.details.map((detail) => detail.message).filter(Boolean);
      return messages.length > 0 ? messages.join(" ") : error.message;
    }
    case "SERVICE_UNAVAILABLE":
      return `${error.message} Answers from documents are not available right now; switch to ordinary chat or try again later.`;
    case "RATE_LIMITED":
      return error.retryAfter === null
        ? "Too many questions in a short time. Try again in a moment."
        : `Too many questions in a short time. Try again in ${wait(error.retryAfter)}.`;
    default:
      return error.message;
  }
}

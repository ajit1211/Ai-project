/**
 * What to tell the user about an AI failure: retry hints for refusals and plain wording for
 * the error codes a stored reply can carry. Shared by the playground and the chat.
 */

const RETRY_LATER_CODES = new Set(["AI_RATE_LIMIT", "AI_TIMEOUT", "AI_PROVIDER_UNAVAILABLE"]);

/** Failures that may well succeed if the same request is simply made again. */
const TEMPORARY_CODES = new Set([
  ...RETRY_LATER_CODES,
  "AI_EMPTY_RESPONSE",
  "AI_INTERNAL_ERROR",
  "GENERATION_INTERRUPTED",
  "NETWORK_ERROR",
]);

/** What to do about a refusal, when its code says waiting helps (`retryAfter` in seconds). */
export function retryHintFor(code: string, retryAfter: number | null): string | null {
  if (retryAfter !== null && (code === "AI_QUOTA_EXCEEDED" || RETRY_LATER_CODES.has(code))) {
    return `Try again in ${String(retryAfter)} s.`;
  }
  if (code === "AI_QUOTA_EXCEEDED") return "Your quota resets later; try again then.";
  if (RETRY_LATER_CODES.has(code)) return "Try again in a moment.";
  return null;
}

const CODE_MESSAGES: Readonly<Record<string, string>> = {
  AI_TIMEOUT: "The AI provider took too long to answer.",
  AI_RATE_LIMIT: "The AI providers are too busy to answer right now.",
  AI_QUOTA_EXCEEDED: "An AI usage limit was reached.",
  AI_PROVIDER_UNAVAILABLE: "The AI provider is unavailable.",
  AI_CONTENT_REJECTED: "The model declined to answer.",
  AI_CONTEXT_TOO_LARGE: "The conversation is too long for this model.",
  AI_EMPTY_RESPONSE: "The model finished without any text.",
  AI_MODEL_UNAVAILABLE: "This conversation's model is not available.",
  AI_OUTPUT_INVALID: "The model's answer was invalid or cut off.",
  AI_AUTHENTICATION_ERROR: "NEXA could not authenticate with the AI provider.",
  AI_INTERNAL_ERROR: "Something went wrong while generating the reply.",
  GENERATION_INTERRUPTED: "The reply was interrupted before it finished.",
  NETWORK_ERROR: "The connection was lost before the reply finished.",
  // AI tool calling: refusals of a send or a decision, and why a paused reply was closed.
  TOOL_NOT_FOUND: "That tool is not available.",
  TOOL_APPROVAL_PENDING:
    "An action in this conversation is waiting for your approval. Approve or reject it first.",
  TOOL_APPROVAL_EXPIRED: "The action was not approved in time, so it expired and was not carried out.",
  TOOL_APPROVAL_STALE: "The action changed since it was shown to you. Review it again before deciding.",
  TOOL_APPROVAL_CLOSED: "This action is no longer waiting for a decision.",
};

/** A sentence for a stored failure, which carries only its `error_code`. */
export function describeErrorCode(code: string | null): string {
  return (code === null ? undefined : CODE_MESSAGES[code]) ?? "The reply could not be generated.";
}

export function isTemporaryCode(code: string | null): boolean {
  return code !== null && TEMPORARY_CODES.has(code);
}

/** The hint shown under a stored failure. */
export function hintForCode(code: string | null): string | null {
  if (code === "AI_QUOTA_EXCEEDED") return "Your quota resets later; try again then.";
  if (code === "AI_CONTEXT_TOO_LARGE") return "Start a new conversation to continue.";
  return isTemporaryCode(code) ? "This failure is temporary; try again." : null;
}

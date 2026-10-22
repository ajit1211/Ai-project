/**
 * Types of AI tool calling (docs/architecture/tools.md): the tools a reply may use, what a
 * reply did with them (`Message.tools`), and the approval a paused reply waits for. They
 * mirror the server's shapes field for field; `src/api/tools.ts` and
 * `src/api/conversations.ts` are the only callers.
 */

/** Where a tool call stands. Ordinary chat sees the last six; the rest are brief. */
export type ToolStatus =
  | "requested"
  | "validated"
  | "awaiting_approval"
  | "approved"
  | "executing"
  | "succeeded"
  | "rejected"
  | "failed"
  | "timed_out"
  | "cancelled";

/** How much harm a tool could do; "" when a call was refused before its tool was known. */
export type ToolRisk = "low" | "medium" | "high" | "";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired" | "invalidated";

export type ApprovalChoice = "approve" | "reject";

/**
 * An action waiting for (or past) the user's decision. `arguments_hash` names the exact action
 * shown: a decision sends it back, so an action that changed meanwhile is never approved.
 * `details` is written by the tool for the user (for save_memory: action, target, content,
 * kind, consequences); it is shown as plain text, never as HTML.
 */
export interface Approval {
  id: string;
  invocation_id: string;
  tool_id: string;
  tool_name: string;
  summary: string;
  details: Record<string, unknown>;
  risk: ToolRisk;
  status: ApprovalStatus;
  arguments_hash: string;
  expires_at: string;
  decided_at: string | null;
}

/** A document (and passage) a tool result came from. */
export interface ToolSourceRef {
  document_id: string;
  chunk_id: string | null;
}

/** One tool call of a reply, as its owner may see it. */
export interface ToolActivity {
  invocation_id: string;
  call_id: string;
  tool_id: string;
  tool_name: string;
  tool_version: string;
  /** The model step that asked for it (1, 2, ...). */
  step: number;
  /** Its place among the reply's calls. */
  position: number;
  status: ToolStatus;
  /** "Calculating 6*7", "Searching documents for “refund policy”", ... */
  summary: string;
  risk: ToolRisk;
  duration_ms: number | null;
  /** Why it was refused or failed: "unknown_tool", "permission_denied", "declined_by_user", ... */
  error_code: string | null;
  error_message: string | null;
  /** The validated data the model received; null when it failed or is hidden. */
  result: Record<string, unknown> | null;
  /** The result was shortened to fit the size limit before the model saw it. */
  result_truncated: boolean;
  /** A document the result came from is no longer readable by the viewer. */
  result_hidden: boolean;
  sources: ToolSourceRef[];
  approval: Approval | null;
}

/** The body of `POST /conversations/{id}/approvals/{approval_id}/`. */
export interface ApprovalDecisionBody {
  decision: ApprovalChoice;
  /** The `arguments_hash` of the approval the user was shown. */
  arguments_hash: string;
}

/** Whether a reply may use tools; "auto" (the default) lets ordinary chat use them. */
export type ToolUse = "auto" | "none";

/** A tool NEXA has (`GET /tools/`). */
export interface ToolInfo {
  id: string;
  name: string;
  description: string;
  version: string;
  category: string;
  risk: ToolRisk;
  read_only: boolean;
  /** Never runs before the user approves the exact action. */
  requires_approval: boolean;
  /** Switched on for the organization (and by the platform). */
  enabled: boolean;
  /** Enabled, and the caller may let AI use it. */
  available: boolean;
  /** Who switched it off, when it is off. */
  disabled_by: "platform" | "organization" | null;
  permissions: string[];
}

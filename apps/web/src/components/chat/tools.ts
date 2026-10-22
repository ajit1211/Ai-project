/**
 * Words for AI tool calls in the chat: where a call stands, why one was refused or failed,
 * what became of its approval, and how risky a tool is. The server's codes are mapped to
 * sentences written for the user; a code this client does not know falls back to the
 * server's own message, then to a general sentence.
 */

import type { Approval, ToolActivity, ToolRisk } from "../../types/tools";

/** How a call looks: in progress, done, failed, stopped or refused, waiting for the user. */
export type ToolTone = "running" | "done" | "failed" | "stopped" | "waiting";

export interface ToolState {
  label: string;
  tone: ToolTone;
}

/** The error code of an action the user declined. */
export const DECLINED = "declined_by_user";

/** The value under `key`, if the table has its own entry for it (never `constructor`, ...). */
export function lookup<V>(table: Readonly<Record<string, V>>, key: string): V | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

const ENDED: Readonly<Record<string, ToolState>> = {
  awaiting_approval: { label: "Needs your approval", tone: "waiting" },
  succeeded: { label: "Done", tone: "done" },
  failed: { label: "Failed", tone: "failed" },
  timed_out: { label: "Timed out", tone: "failed" },
  cancelled: { label: "Cancelled", tone: "stopped" },
};

/**
 * Where a call stands. `running`: its reply is still being generated, so a call that has not
 * ended (requested, validated, approved, executing) is running; once the reply is over, such
 * a call did not finish.
 */
export function toolState(activity: ToolActivity, running: boolean): ToolState {
  if (activity.status === "rejected") {
    return activity.error_code === DECLINED
      ? { label: "Declined", tone: "stopped" }
      : { label: "Rejected", tone: "stopped" };
  }
  const ended = lookup(ENDED, activity.status);
  if (ended) return ended;
  return running ? { label: "Running", tone: "running" } : { label: "Did not finish", tone: "stopped" };
}

/** Why the server refused a call before it ran. */
const REJECTION_REASONS: Readonly<Record<string, string>> = {
  [DECLINED]: "You declined this action, so it was not carried out.",
  unknown_tool: "The model asked for a tool that does not exist.",
  invalid_arguments: "The model asked for this tool with input that is not valid.",
  permission_denied: "You do not have permission to use this tool.",
  tool_disabled: "This tool is switched off for your organization.",
  too_many_calls: "The reply asked for more tools than one reply may use.",
  rate_limited: "Too many tool calls in a short time.",
  one_action_at_a_time: "Only one action that needs your approval can be proposed at a time.",
};

/**
 * Why a call was refused, failed or timed out, or null for calls that did not. Rejections
 * use this client's sentences for the codes it knows; failures carry the tool's own message
 * ("That time zone is not known."), which is more specific than any general sentence.
 */
export function toolReason(activity: ToolActivity): string | null {
  const { status, error_code: code, error_message: message } = activity;
  if (status === "rejected") {
    return (code !== null ? lookup(REJECTION_REASONS, code) : undefined) ?? message ?? "The tool call was refused.";
  }
  if (status === "failed") return message ?? "The tool could not complete this call.";
  if (status === "timed_out") return message ?? "The tool took too long and was stopped.";
  return null;
}

/** What became of a call's approval, when it was asked for and is no longer pending. */
export function approvalOutcome(approval: Approval | null): string | null {
  switch (approval?.status) {
    case "approved":
      return "You approved this action.";
    case "expired":
      return "The approval request expired before you decided.";
    case "invalidated":
      return "The approval request was withdrawn.";
    default:
      // Pending: the approval card asks. Rejected: the call's reason says so.
      return null;
  }
}

const RISK_LABELS: Readonly<Record<string, string>> = {
  low: "Low risk",
  medium: "Medium risk",
  high: "High risk",
};

/** "High risk", ...; null when the risk is not known. */
export function riskLabel(risk: ToolRisk): string | null {
  if (!risk) return null;
  return lookup(RISK_LABELS, risk) ?? `Risk: ${risk}`;
}

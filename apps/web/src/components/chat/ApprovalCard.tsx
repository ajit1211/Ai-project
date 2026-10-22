/**
 * The action a paused reply (or agent task) asks the user to approve: the tool, its risk, what
 * it would do (action, target, the exact content, consequences, and anything else the tool
 * describes), and until when the request stands. Nothing happens until the user presses
 * Approve; Reject (Decline, on a task) declines it. Both continue the same reply or task.
 * While a decision is being sent, both buttons are disabled and the one pressed says so.
 *
 * The details are written by the tool from the model's request, so they are untrusted text:
 * shown as plain text (line breaks kept), never as HTML or Markdown.
 */

import { useId } from "react";

import type { Approval, ApprovalChoice } from "../../types/tools";
import { Icon } from "./Icon";
import { riskLabel } from "./tools";

/** The details every tool may describe, in the order shown. Others follow, as sent. */
const KNOWN_DETAILS: readonly (readonly [key: string, label: string])[] = [
  ["action", "Action"],
  ["target", "Target"],
  ["content", "Content"],
  ["kind", "Kind"],
  ["consequences", "Consequences"],
];

/** A detail's value as text; null for nothing worth showing. */
function detailText(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

/** "affected_items" → "Affected items". */
function labelOf(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The rows of the details list: [key, label, text]. */
export function approvalRows(details: Readonly<Record<string, unknown>>): [string, string, string][] {
  const known = new Set(KNOWN_DETAILS.map(([key]) => key));
  const rows: [string, string, string][] = [];
  for (const [key, label] of KNOWN_DETAILS) {
    const value = Object.hasOwn(details, key) ? detailText(details[key]) : null;
    if (value !== null) rows.push([key, label, value]);
  }
  for (const [key, raw] of Object.entries(details)) {
    const value = known.has(key) ? null : detailText(raw);
    if (value !== null) rows.push([key, labelOf(key), value]);
  }
  return rows;
}

function formatExpiry(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export interface ApprovalCardProps {
  approval: Approval;
  /** A decision is being sent (or the reply is otherwise busy): the buttons are disabled. */
  busy: boolean;
  /** The decision being sent, if any. */
  deciding?: ApprovalChoice | undefined;
  /** Left out where nothing can be decided (an archived conversation): no buttons. */
  onDecide?: ((choice: ApprovalChoice) => void) | undefined;
  /** The word for turning the action down: "Reject" in the chat, "Decline" for agent tasks. */
  rejectVerb?: "Reject" | "Decline";
}

export function ApprovalCard({ approval, busy, deciding, onDecide, rejectVerb = "Reject" }: ApprovalCardProps) {
  const id = useId();
  const risk = riskLabel(approval.risk);
  const rows = approvalRows(approval.details);
  return (
    <section className={`approval-card approval-risk-${approval.risk || "unknown"}`} aria-labelledby={`${id}-title`}>
      <div className="approval-head">
        <Icon name="shield" />
        <p id={`${id}-title`} className="approval-title">
          Approval needed
        </p>
        {risk && (
          <span className={`badge ${approval.risk === "high" ? "badge-down" : "badge-waiting"}`}>{risk}</span>
        )}
      </div>
      <p id={`${id}-summary`} className="approval-summary">
        {approval.summary || approval.tool_name}
      </p>
      <dl className="approval-details">
        <div>
          <dt>Tool</dt>
          <dd>{approval.tool_name}</dd>
        </div>
        {rows.map(([key, label, value]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd className="approval-value">{value}</dd>
          </div>
        ))}
        <div>
          <dt>Expires</dt>
          <dd>
            <time dateTime={approval.expires_at}>{formatExpiry(approval.expires_at)}</time>
          </dd>
        </div>
      </dl>
      {onDecide && (
        <>
          <p className="approval-hint">Nothing is done unless you approve.</p>
          <div className="approval-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              aria-describedby={`${id}-summary`}
              onClick={() => {
                onDecide("approve");
              }}
            >
              {deciding === "approve" ? "Approving…" : "Approve"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy}
              aria-describedby={`${id}-summary`}
              onClick={() => {
                onDecide("reject");
              }}
            >
              {deciding === "reject" ? `${rejectVerb === "Decline" ? "Declining" : "Rejecting"}…` : rejectVerb}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

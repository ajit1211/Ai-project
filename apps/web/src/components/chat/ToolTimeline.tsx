/**
 * What a reply did with tools: one row per call the server reported (live events or the
 * stored record; nothing is shown for calls that did not happen). Each row says where the
 * call stands in words (Running, Done, Failed, Timed out, Cancelled, Rejected or Declined
 * with the reason, Needs your approval), with an icon (a spinner while it runs), its summary
 * and, once known, how long it took. A result folds out under "Show result" (ToolResult.tsx).
 */

import { useId } from "react";

import type { ToolActivity } from "../../types/tools";
import { formatDuration } from "./format";
import { Icon, type IconName } from "./Icon";
import { ToolResult } from "./ToolResult";
import { approvalOutcome, toolReason, toolState, type ToolTone } from "./tools";

const TONE_ICONS = {
  done: "check",
  failed: "alert",
  stopped: "ban",
  waiting: "shield",
} as const satisfies Record<Exclude<ToolTone, "running">, IconName>;

/** Said instead of a result the viewer may no longer see. */
export const RESULT_HIDDEN = "Result hidden: a source document is no longer available to you.";

function ToolRow({ activity, running }: { activity: ToolActivity; running: boolean }) {
  const state = toolState(activity, running);
  const reason = toolReason(activity);
  const outcome = approvalOutcome(activity.approval);
  const title = activity.summary || activity.tool_name || activity.tool_id;
  return (
    <li className={`tool-row tool-${state.tone}`}>
      <div className="tool-row-head">
        <span className="tool-icon" aria-hidden="true">
          {state.tone === "running" ? <span className="tool-spinner" /> : <Icon name={TONE_ICONS[state.tone]} />}
        </span>
        <span className="tool-summary">{title}</span>
        <span className="tool-status">{state.label}</span>
        {activity.duration_ms !== null && (
          <span className="tool-duration">{formatDuration(activity.duration_ms)}</span>
        )}
      </div>
      {reason && <p className="tool-reason">{reason}</p>}
      {outcome && <p className="tool-note">{outcome}</p>}
      {activity.result_hidden ? (
        <p className="tool-note">{RESULT_HIDDEN}</p>
      ) : (
        activity.result !== null && (
          <details className="tool-details">
            <summary>
              Show result<span className="visually-hidden"> of {title}</span>
            </summary>
            <ToolResult activity={activity} />
          </details>
        )
      )}
    </li>
  );
}

export interface ToolTimelineProps {
  tools: readonly ToolActivity[];
  /** The reply is still being generated: calls that have not ended are running. */
  running: boolean;
}

export function ToolTimeline({ tools, running }: ToolTimelineProps) {
  const id = useId();
  if (tools.length === 0) return null;
  return (
    <div className="tool-activity">
      <p id={id} className="tool-activity-title">
        Tool activity
      </p>
      <ol className="tool-timeline" aria-labelledby={id}>
        {tools.map((activity) => (
          <ToolRow key={activity.invocation_id} activity={activity} running={running} />
        ))}
      </ol>
    </div>
  );
}

/**
 * A task's history: one row per step, in order, as the server recorded it. Each row says what
 * the step was (the agent's decision, a tool call, progress, the answer, a problem), its
 * summary, where it stands in words (with an icon, a spinner while it runs) and how long it
 * took; then what is worth knowing about it: what the agent decided (and why it asked for an
 * approval), the tool call as the chat shows one (its reason, what became of its approval, its
 * result under "Show result"), the progress note, the answer's citations, the problem.
 *
 * Steps are partly written by the model: everything is shown as plain text, never as HTML or
 * Markdown.
 */

import { useId } from "react";

import type { Step } from "../../types/agents";
import { formatCount, formatDuration } from "../chat/format";
import { Icon, type IconName } from "../chat/Icon";
import { ToolResult } from "../chat/ToolResult";
import { RESULT_HIDDEN } from "../chat/ToolTimeline";
import { approvalOutcome, toolReason, toolState, type ToolState, type ToolTone } from "../chat/tools";
import { decisionLabel, stepKindLabel } from "./labels";

type Json = Record<string, unknown>;

const isRecord = (value: unknown): value is Json =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** A non-empty string, or null. */
const text = (value: unknown): string | null => (typeof value === "string" && value.trim() !== "" ? value : null);

/** The strings of a list, or an empty list. */
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

const KIND_ICONS: Readonly<Record<string, IconName>> = {
  decision: "spark",
  tool: "tool",
  approval: "shield",
  progress: "message",
  final: "check",
  error: "alert",
};

const STATE_ICONS = {
  failed: "alert",
  stopped: "ban",
  waiting: "shield",
} as const satisfies Partial<Record<ToolTone, IconName>>;

/** Where a step that is not a tool call stands. `running`: the task is still being carried out. */
function stepState(step: Step, running: boolean): ToolState {
  switch (step.status) {
    case "succeeded":
      return { label: "Done", tone: "done" };
    case "failed":
      return { label: "Failed", tone: "failed" };
    case "rejected":
      return { label: "Refused", tone: "stopped" };
    case "cancelled":
      return { label: "Cancelled", tone: "stopped" };
    case "waiting":
      return { label: "Waiting", tone: "waiting" };
    default:
      return running ? { label: "Running", tone: "running" } : { label: "Did not finish", tone: "stopped" };
  }
}

function StepIcon({ kind, tone }: { kind: string; tone: ToolTone }) {
  if (tone === "running") return <span className="tool-spinner" />;
  const name = tone === "done" ? (KIND_ICONS[kind] ?? "check") : STATE_ICONS[tone];
  return <Icon name={name} />;
}

/** The arguments the agent chose for a tool, folded away: they are its own words. */
function Arguments({ value, title }: { value: unknown; title: string }) {
  if (!isRecord(value) || Object.keys(value).length === 0) return null;
  return (
    <details className="tool-details">
      <summary>
        Show arguments<span className="visually-hidden"> of {title}</span>
      </summary>
      <pre className="tool-json run-step-json">
        <code>{JSON.stringify(value, null, 2)}</code>
      </pre>
    </details>
  );
}

function DecisionDetail({ step, title }: { step: Step; title: string }) {
  const decision = isRecord(step.detail.decision) ? step.detail.decision : {};
  const action = text(step.detail.action) ?? text(decision.action);
  const tool = text(decision.tool);
  const message = text(decision.message);
  const reason = text(decision.reason);
  const dropped = typeof step.detail.context_dropped === "number" ? step.detail.context_dropped : 0;
  return (
    <>
      {action && (
        <p className="tool-note run-step-decision">
          {decisionLabel(action)}
          {tool && (
            <>
              : <code>{tool}</code>
            </>
          )}
        </p>
      )}
      {action === "request_approval" && message && <p className="tool-note run-step-text">Why: {message}</p>}
      {action === "fail" && reason && <p className="tool-reason run-step-text">{reason}</p>}
      {(action === "call_tool" || action === "request_approval") && (
        <Arguments value={decision.arguments} title={title} />
      )}
      {dropped > 0 && (
        <p className="tool-note">
          {dropped === 1
            ? "1 earlier message was left out of the model's context to save space."
            : `${formatCount(dropped)} earlier messages were left out of the model's context to save space.`}
        </p>
      )}
    </>
  );
}

function ToolDetail({ step, running, title }: { step: Step; running: boolean; title: string }) {
  const activity = step.tool;
  if (!activity) {
    // Refused before it became a call: the reason is all there is.
    const error = isRecord(step.detail.error) ? text(step.detail.error.message) : null;
    return error && !step.summary.includes(error) ? <p className="tool-reason">{error}</p> : null;
  }
  const reason = toolReason(activity);
  const outcome = approvalOutcome(activity.approval);
  return (
    <>
      {reason && <p className="tool-reason">{reason}</p>}
      {outcome && <p className="tool-note">{outcome}</p>}
      {activity.status === "awaiting_approval" && running && (
        <p className="tool-note">Waiting for your decision, above.</p>
      )}
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
    </>
  );
}

function FinalDetail({ step }: { step: Step }) {
  const cited = strings(step.detail.cited);
  const invalid = strings(step.detail.invalid_citations);
  return (
    <>
      {cited.length > 0 && <p className="tool-note">Cites {cited.join(", ")}.</p>}
      {invalid.length > 0 && (
        <p className="tool-note">
          {invalid.length === 1
            ? `The citation ${invalid.join("")} could not be verified; it is shown as plain text.`
            : `The citations ${invalid.join(", ")} could not be verified; they are shown as plain text.`}
        </p>
      )}
      {step.detail.has_output === true && <p className="tool-note">With structured output.</p>}
    </>
  );
}

function StepDetail({ step, running, title }: { step: Step; running: boolean; title: string }) {
  switch (step.kind) {
    case "decision":
      return <DecisionDetail step={step} title={title} />;
    case "tool":
      return <ToolDetail step={step} running={running} title={title} />;
    case "progress": {
      // The summary holds the first 300 characters; a longer note is shown whole.
      const message = text(step.detail.message);
      return message && message !== step.summary ? <p className="tool-note run-step-text">{message}</p> : null;
    }
    case "final":
      return <FinalDetail step={step} />;
    case "error": {
      const reason = text(step.detail.reason);
      return reason && reason !== step.summary ? <p className="tool-reason run-step-text">{reason}</p> : null;
    }
    default:
      return null;
  }
}

function StepRow({ step, running }: { step: Step; running: boolean }) {
  const activity = step.kind === "tool" ? step.tool : null;
  const state = activity ? toolState(activity, running) : stepState(step, running);
  const title = step.summary || activity?.tool_name || stepKindLabel(step.kind);
  const duration = step.duration_ms ?? activity?.duration_ms ?? null;
  const tokens = step.input_tokens + step.output_tokens;
  return (
    <li className={`run-step run-step-${step.kind} tool-${state.tone}`}>
      <div className="run-step-head">
        <span className="tool-icon" aria-hidden="true">
          <StepIcon kind={step.kind} tone={state.tone} />
        </span>
        <span className="run-step-kind">{stepKindLabel(step.kind)}</span>
        <span className="run-step-summary">{title}</span>
        <span className="tool-status">{state.label}</span>
        {duration !== null && <span className="tool-duration">{formatDuration(duration)}</span>}
        {tokens > 0 && <span className="tool-duration">{formatCount(tokens)} tokens</span>}
      </div>
      <StepDetail step={step} running={running} title={title} />
      {step.retry_count > 0 && (
        <p className="tool-note">
          {step.retry_count === 1 ? "Retried once." : `Retried ${formatCount(step.retry_count)} times.`}
        </p>
      )}
    </li>
  );
}

export interface RunTimelineProps {
  steps: readonly Step[];
  /** The task is still being carried out: steps that have not ended are running. */
  running: boolean;
}

export function RunTimeline({ steps, running }: RunTimelineProps) {
  const id = useId();
  return (
    <section className="card section run-timeline-card" aria-labelledby={id}>
      <h2 id={id}>Steps</h2>
      {steps.length === 0 ? (
        <p className="muted small">
          {running ? "No steps yet: the agent has not started." : "No steps were recorded."}
        </p>
      ) : (
        <ol className="run-timeline" aria-labelledby={id}>
          {steps.map((step) => (
            <StepRow key={step.id} step={step} running={running} />
          ))}
        </ol>
      )}
    </section>
  );
}

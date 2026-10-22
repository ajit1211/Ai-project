/**
 * The words the agent pages use for what the server reports: where a task stands (and its
 * badge colour), why it ended without an answer, what each step was and what the agent
 * decided, limits and durations, and refused requests. A code this client does not know (a
 * newer server) is shown as sent, never dropped. A server message is shown only where the API
 * writes it for users; anything unexpected gets fixed wording (and a request id).
 */

import { ApiError, type FieldError } from "../../api/client";
import type {
  Agent,
  AgentLimits,
  DecisionAction,
  FailureReason,
  RunStatus,
  StepKind,
} from "../../types/agents";
import { formatCount } from "../chat/format";
import { lookup } from "../chat/tools";
import type { StatusTone } from "../documents/labels";

/** The platform agent to start with (nexa_core/agents/catalog.py). */
export const DOCUMENT_ASSISTANT_SLUG = "document-assistant";

/** NEXA's Document Assistant, which the library puts first. */
export const isDocumentAssistant = (agent: Pick<Agent, "scope" | "slug">): boolean =>
  agent.scope === "platform" && agent.slug === DOCUMENT_ASSISTANT_SLUG;

/** "Up to 8 steps, 6 tool calls, 5 min and 120,000 tokens per task." */
export function limitsSummary(limits: AgentLimits): string {
  const steps = `${formatCount(limits.max_steps)} ${limits.max_steps === 1 ? "step" : "steps"}`;
  const calls = `${formatCount(limits.max_tool_calls)} tool ${limits.max_tool_calls === 1 ? "call" : "calls"}`;
  const time = formatSeconds(limits.max_duration_seconds);
  return `Up to ${steps}, ${calls}, ${time} and ${formatCount(limits.max_tokens)} tokens per task.`;
}

/** The badge colours of styles.css: the document tones, and `.badge-info` for work in progress. */
export type RunTone = StatusTone | "info";

const RUN_STATUS_LABELS = {
  queued: "Queued",
  running: "Running",
  waiting_for_approval: "Needs your approval",
  paused: "Paused",
  cancel_requested: "Cancelling",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  timed_out: "Timed out",
} satisfies Record<RunStatus, string>;

const RUN_STATUS_TONES = {
  queued: "neutral",
  running: "info",
  waiting_for_approval: "waiting",
  paused: "waiting",
  cancel_requested: "waiting",
  completed: "up",
  failed: "down",
  cancelled: "neutral",
  timed_out: "down",
} satisfies Record<RunStatus, RunTone>;

const FINAL: ReadonlySet<string> = new Set<RunStatus>(["completed", "failed", "cancelled", "timed_out"]);

/** Over: nothing about the task changes any more, so it is no longer followed. */
export const isFinal = (status: string): boolean => FINAL.has(status);

export function runStatusLabel(status: string): string {
  return lookup(RUN_STATUS_LABELS, status) ?? status;
}

export function runStatusTone(status: string): RunTone {
  return lookup(RUN_STATUS_TONES, status) ?? "neutral";
}

const FAILURE_SENTENCES = {
  cancelled: "The task was cancelled.",
  timeout: "The task ran out of time before it finished.",
  step_limit: "The agent reached its step limit before it finished.",
  token_budget: "The task used up its token budget before it finished.",
  tool_call_limit: "The agent reached its limit of tool calls.",
  loop_detected: "The agent kept repeating the same action, so the task was stopped.",
  validation_failed: "The agent's answers did not have the form NEXA expects, so the task was stopped.",
  agent_reported: "The agent could not complete the task.",
  permission_denied: "You no longer have permission to run this agent or one of its tools.",
  approval_expired: "An action waited too long for your approval and expired.",
  provider_error: "The AI provider could not be reached.",
  quota_exceeded: "An AI usage limit was reached.",
  context_too_large: "The task grew too large for the model to handle.",
  model_unavailable: "The agent's model is no longer available.",
  interrupted: "The task was interrupted and could not safely continue.",
  invalid_configuration:
    "The agent's configuration is no longer valid (for example, its model or a tool is unavailable).",
  internal_error: "Something went wrong on NEXA's side.",
} satisfies Record<FailureReason, string>;

export interface RunFailure {
  /** Why the task ended without its answer, in a sentence. */
  summary: string;
  /** What the server added (for "agent_reported", the agent's own reason), when it says more. */
  detail: string | null;
}

/**
 * Why a task ended without its answer. A reason this client knows gets its sentence, with the
 * server's message as the detail; for any other, the server's message is the sentence, or the
 * status decides.
 */
export function describeRunFailure(run: {
  status: string;
  failure_reason: string | null;
  error_message: string | null;
}): RunFailure {
  const message = run.error_message?.trim() || null;
  const known = run.failure_reason === null ? undefined : lookup(FAILURE_SENTENCES, run.failure_reason);
  if (known) return { summary: known, detail: message === known ? null : message };
  if (message) return { summary: message, detail: null };
  if (run.status === "cancelled") return { summary: FAILURE_SENTENCES.cancelled, detail: null };
  if (run.status === "timed_out") return { summary: FAILURE_SENTENCES.timeout, detail: null };
  return { summary: "The task did not finish.", detail: null };
}

const STEP_KIND_LABELS = {
  decision: "Decision",
  tool: "Tool",
  approval: "Approval",
  progress: "Progress",
  final: "Answer",
  error: "Problem",
} satisfies Record<StepKind, string>;

export function stepKindLabel(kind: string): string {
  return lookup(STEP_KIND_LABELS, kind) ?? kind;
}

const DECISION_LABELS = {
  call_tool: "Chose a tool",
  request_approval: "Asked for approval",
  report_progress: "Reported progress",
  complete: "Finished the task",
  fail: "Gave up",
} satisfies Record<DecisionAction, string>;

/** What the agent decided, in words; an action this client does not know is shown as sent. */
export function decisionLabel(action: string): string {
  return lookup(DECISION_LABELS, action) ?? action;
}

/** Seconds as "45 s", "4 min 32 s", "10 min", "1 h 5 min". */
export function formatSeconds(total: number): string {
  const seconds = Math.max(0, Math.round(total));
  if (seconds < 60) return `${String(seconds)} s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  const parts: string[] = [];
  if (hours > 0) parts.push(`${String(hours)} h`);
  if (minutes > 0) parts.push(`${String(minutes)} min`);
  if (rest > 0 && hours === 0) parts.push(`${String(rest)} s`);
  return parts.join(" ");
}

/** "Oct 22, 2026, 9:05 AM", or the text as sent when it is not a date. */
export function formatDateTime(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** A task's goal on one line, cut at `max` characters. */
export function goalExcerpt(goal: string, max = 140): string {
  const line = goal.replace(/\s+/g, " ").trim();
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line;
}

/** What the JSON Schema validator's keywords mean, for a task input the server refused. */
const INPUT_PROBLEMS: Readonly<Record<string, string>> = {
  required: "A required value is missing.",
  type: "This value has the wrong type.",
  enum: "Choose one of the listed values.",
  const: "This value is not the one expected.",
  minimum: "This number is too small.",
  exclusiveMinimum: "This number is too small.",
  maximum: "This number is too large.",
  exclusiveMaximum: "This number is too large.",
  multipleOf: "This number is not allowed.",
  minLength: "This is too short.",
  maxLength: "This is too long.",
  pattern: "This does not have the expected form.",
  format: "This does not have the expected form.",
  additionalProperties: "This input has a value the agent does not expect.",
};

/** A refused task input as a sentence: the server sends the schema keyword that failed. */
export function describeInputProblem(detail: FieldError): string {
  return lookup(INPUT_PROBLEMS, detail.code) ?? detail.message;
}

/** Refusals whose message the API writes for the user. */
const USER_WORDED_STATUSES: ReadonlySet<number> = new Set([404, 409, 422]);

/** Said for a 403 when the caller has nothing more specific to say. */
export const FORBIDDEN_TEXT = "You do not have permission to do that.";

/** The words for "too many tasks in progress" (429 RUN_LIMIT_REACHED). */
export const RUN_LIMIT_TEXT =
  "There are too many tasks in progress. Wait for one to finish (or cancel one), then try again.";

const ERROR_TEXT: Readonly<Record<string, string>> = {
  RUN_LIMIT_REACHED: RUN_LIMIT_TEXT,
  AGENT_UNAVAILABLE: "This agent is not available: it may have been switched off or deleted.",
  SERVICE_UNAVAILABLE: "Agents are not available right now. Try again in a moment.",
};

/** "45 s", or whole minutes above 90 seconds. */
function wait(seconds: number): string {
  return seconds <= 90 ? `${String(Math.max(1, seconds))} s` : `${String(Math.ceil(seconds / 60))} min`;
}

/**
 * A sentence for a failed agent or task request, safe to show whatever the server sent.
 * `forbidden` is what a 403 means where the request was made ("You cannot create agents
 * here.").
 */
export function describeAgentError(error: unknown, forbidden: string = FORBIDDEN_TEXT): string {
  if (!(error instanceof ApiError)) {
    return "Something went wrong. Check your connection and try again.";
  }
  if (error.code === "NETWORK_ERROR") return "NEXA could not be reached. Check your connection and try again.";
  const fixed = lookup(ERROR_TEXT, error.code);
  if (fixed) return fixed;
  if (error.code === "VALIDATION_FAILED") {
    const details = error.details.map((detail) => detail.message).filter(Boolean);
    return details.length > 0 ? details.join(" ") : error.message;
  }
  if (USER_WORDED_STATUSES.has(error.status)) return error.message;
  if (error.status === 401) return "Your session has ended. Sign in again to continue.";
  if (error.status === 403) return forbidden;
  if (error.status === 429) {
    return error.retryAfter === null
      ? "Too many requests. Try again in a moment."
      : `Too many requests. Try again in ${wait(error.retryAfter)}.`;
  }
  const reference = error.requestId ? ` (ref ${error.requestId})` : "";
  return `Something went wrong on NEXA's side. Try again in a moment.${reference}`;
}

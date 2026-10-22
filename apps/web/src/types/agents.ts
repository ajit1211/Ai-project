/**
 * Types of AI agents and their tasks (docs/api/agents.md). An agent is a definition served by
 * Core (`/agents/`): instructions, the tools it may use, its limits. A task (a "run") is one
 * agent working on one goal, served by Runtime (`/runs/`): the run, its steps, its live events
 * and the answers to its controls. They mirror the server's shapes field for field;
 * `src/api/agents.ts` and `src/api/runs.ts` are the only callers.
 */

import type { Approval, ApprovalChoice, ToolActivity, ToolRisk } from "./tools";

/** A JSON Schema document, an object at the top. */
export type JsonSchema = Record<string, unknown>;

/** Where an agent comes from: shipped with NEXA (never editable here) or made in the organization. */
export type AgentScope = "platform" | "organization";

/**
 * Which tool calls wait for the user: "registry" asks only for the tools that always require
 * approval; "all_tool_calls" asks before every call.
 */
export type ApprovalPolicy = "registry" | "all_tool_calls";

/** A tool an agent may use, as the agent list describes it. */
export interface AgentTool {
  id: string;
  name: string;
  /** "" for a tool this deployment no longer has. */
  risk: ToolRisk;
  requires_approval: boolean;
}

/** The budget of one task: it stops when any of them is used up. */
export interface AgentLimits {
  max_steps: number;
  max_tool_calls: number;
  max_duration_seconds: number;
  max_tokens: number;
}

/** An agent as listed (GET /agents/). */
export interface Agent {
  id: string;
  name: string;
  slug: string;
  description: string;
  /** Goes up with every change to how it behaves; a task keeps the version it started with. */
  version: number;
  enabled: boolean;
  is_system: boolean;
  scope: AgentScope;
  /** A logical model name; "" for the organization's default model. */
  model: string;
  tools: AgentTool[];
  limits: AgentLimits;
  approval_policy: ApprovalPolicy;
  has_input_schema: boolean;
  has_output_schema: boolean;
  /** Null for the platform's agents. */
  created_by: string | null;
  /** These are decided by the server for the caller. */
  can_edit: boolean;
  can_delete: boolean;
  created_at: string;
  updated_at: string;
}

/** One agent with its configuration (GET /agents/{id}/, and the answer to a create or update). */
export interface AgentDetail extends Agent {
  instructions: string;
  /** What a task's `input` must match; null when the agent takes a goal only. */
  input_schema: JsonSchema | null;
  /** What a finished task's `output` must match; null when the answer is text only. */
  output_schema: JsonSchema | null;
}

/** The fields of a new agent (POST /agents/); the ones left out take the server's defaults. */
export interface AgentInput {
  /** At most 80 characters. */
  name: string;
  /** At most 500 characters. */
  description?: string;
  /** At most 20,000 characters. */
  instructions: string;
  /** "" for the organization's default model. */
  model?: string;
  /** Tool ids, at most 32. */
  allowed_tools?: string[];
  input_schema?: JsonSchema | null;
  output_schema?: JsonSchema | null;
  /** 1 to 50 (default 12). */
  max_steps?: number;
  /** 0 to 100 (default 20). */
  max_tool_calls?: number;
  /** 10 to 3600 (default 600). */
  max_duration_seconds?: number;
  /** 1,000 to 1,000,000 (default 200,000). */
  max_tokens?: number;
  approval_policy?: ApprovalPolicy;
  enabled?: boolean;
}

/** A partial update (PATCH /agents/{id}/): absent fields stay as they are. */
export type UpdateAgentBody = Partial<AgentInput>;

export interface ListAgentsParams {
  /** Only the agents switched on (the ones a task can be started with). */
  enabled?: boolean;
}

/** Where a task stands. The last four are final: nothing changes after them. */
export type RunStatus =
  | "queued"
  | "running"
  | "waiting_for_approval"
  | "paused"
  | "cancel_requested"
  | "completed"
  | "failed"
  | "cancelled"
  | "timed_out";

/** The task list's filter: one status, or "active" (the first five) or "finished" (the last four). */
export type RunStatusFilter = RunStatus | "active" | "finished";

/**
 * Why a task ended without its answer (components/agents/labels.ts has the words). `Run`
 * types it as a string: a newer server may send a reason this client does not know.
 */
export type FailureReason =
  | "cancelled"
  | "timeout"
  | "step_limit"
  | "token_budget"
  | "tool_call_limit"
  | "loop_detected"
  | "validation_failed"
  | "agent_reported"
  | "permission_denied"
  | "approval_expired"
  | "provider_error"
  | "quota_exceeded"
  | "context_too_large"
  | "model_unavailable"
  | "interrupted"
  | "invalid_configuration"
  | "internal_error";

/** Measured, never estimated. `cost_usd` is a decimal string, or null when a model has no price. */
export interface RunUsage {
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string | null;
}

/** A passage the answer cites as [R1], re-authorized for the reader now. */
export interface RunSource {
  /** "R1", "R2", ...: what the answer cites. */
  ref: string;
  document_id: string;
  chunk_id: string | null;
  title: string;
  /** False once the document is deleted or no longer shared with the reader. */
  available: boolean;
}

/** A task: one agent working on one goal (GET /runs/{id}/). */
export interface Run {
  id: string;
  agent_id: string;
  agent_name: string;
  agent_version: number;
  status: RunStatus;
  /** The user's own words, plain text. */
  goal: string;
  input: Record<string, unknown>;
  conversation_id: string | null;
  model: string;
  /** What the task is doing now ("Waiting for approval: …", "Done"); "" before it starts. */
  progress_message: string;
  step_count: number;
  tool_call_count: number;
  /** The agent's limits as this task applies them (capped by the platform's). */
  limits: AgentLimits;
  usage: RunUsage;
  /** The answer in Markdown, possibly citing [R1]-style references; "" until it is completed. */
  result: string;
  /** Structured output matching the agent's output schema, when it has one. */
  output: Record<string, unknown> | null;
  sources: RunSource[];
  /** A `FailureReason`, or null while the task has not failed. */
  failure_reason: string | null;
  /** Written for the user by the server; for "agent_reported", the agent's own reason. */
  error_message: string | null;
  cancel_requested: boolean;
  pause_requested: boolean;
  /** The action the task waits for while it is "waiting_for_approval". */
  pending_approval: Approval | null;
  /** What the caller may do now. */
  can_pause: boolean;
  can_resume: boolean;
  can_cancel: boolean;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  /** While running: when its time budget ends. Null while its clock is stopped. */
  deadline_at: string | null;
  /** While paused or waiting for approval: the seconds its time budget has left. */
  time_left_seconds: number | null;
}

export type StepKind = "decision" | "tool" | "approval" | "progress" | "final" | "error";

export type StepStatus = "running" | "succeeded" | "failed" | "rejected" | "cancelled" | "waiting";

/** What the agent chose to do next, as a decision step records it. */
export type DecisionAction =
  | "call_tool"
  | "request_approval"
  | "report_progress"
  | "complete"
  | "fail";

/**
 * One entry of a task's history (GET /runs/{id}/steps/). A step can change after it is first
 * seen (a tool call that was running ends): it is identified by `id`.
 *
 * `detail` depends on the kind; every field may be missing:
 * - decision: `{action, decision: {action, summary?, tool?, arguments?, message?, answer?,
 *   output?, reason?}, context_dropped}` (`context_dropped`: how many earlier messages were
 *   left out of the model's context to save space);
 * - tool: `{tool_id, status?, truncated?, error?: {code, message}}`;
 * - progress: `{message}`;
 * - final: `{answer_chars, cited, invalid_citations, has_output}`;
 * - error: `{reason}`.
 * Part of it is written by the model: it is shown as plain text, never as HTML or Markdown.
 */
export interface Step {
  id: string;
  /** Its place in the task: 1, 2, ... */
  sequence: number;
  kind: StepKind;
  status: StepStatus;
  /** One line for the user: "Searching documents for “refund policy”", "Completed", ... */
  summary: string;
  detail: Record<string, unknown>;
  /** The call of a tool step, as the chat shows tool calls; null for other kinds. */
  tool: ToolActivity | null;
  error_code: string | null;
  retry_count: number;
  input_tokens: number;
  output_tokens: number;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  created_at: string;
}

/**
 * An event of a task's progress stream (GET /runs/{id}/events/), named by its SSE event:
 * `run` whenever the task changes, `step` for each new or changed step, and `end` (the final
 * task) once it is over, after which the stream closes.
 */
export type RunEvent =
  | { type: "run"; run: Run }
  | { type: "step"; step: Step }
  | { type: "end"; run: Run };

/** The answer to pause, resume and cancel: where the task stands now, and what happens next. */
export interface RunControl {
  id: string;
  status: RunStatus;
  cancel_requested: boolean;
  pause_requested: boolean;
  /** "Paused.", "The run will pause at its next checkpoint.", ... */
  message: string;
}

/** The body of POST /runs/. */
export interface SubmitRunBody {
  agent_id: string;
  /** 1 to 10,000 characters. */
  goal: string;
  /** Checked against the agent's input schema. */
  input?: Record<string, unknown>;
  conversation_id?: string;
  /**
   * Sent again with a retried submission, so it starts no second task: the server answers
   * with the task the key already started. Letters, digits and `_.:-`, at most 128.
   */
  idempotency_key?: string;
}

export interface ListRunsParams {
  status?: RunStatusFilter;
  agent_id?: string;
  /** 1 to 100 (default 30). */
  limit?: number;
  cursor?: string;
}

/** One page of tasks, newest first. */
export interface RunPage {
  runs: Run[];
  nextCursor: string | null;
  hasMore: boolean;
}

export interface ListStepsParams {
  /** Only the steps after this sequence number. */
  after?: number;
  limit?: number;
}

/** The body of POST /runs/{id}/approvals/{approval_id}/. */
export interface RunDecisionBody {
  decision: ApprovalChoice;
  /** The `arguments_hash` of the approval the user was shown. */
  arguments_hash: string;
  /** At most 500 characters. */
  comment?: string;
}

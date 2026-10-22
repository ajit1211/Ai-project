/**
 * Test data for the agent pages and API tests: agents, tasks (runs) and their steps shaped
 * exactly like the server's (docs/api/agents.md). The platform's Document Assistant is the
 * default agent; `orgAgent` is one made in the organization, which its author may change.
 */

import type { Agent, AgentDetail, Run, RunSource, Step } from "../types/agents";
import { approval, toolActivity } from "./chatFixtures";

export const ASSISTANT_ID = "agent-1";
export const WRITER_ID = "agent-2";
export const RUN_ID = "run-1";

/** The platform's Document Assistant, unless overridden. */
export function agent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: ASSISTANT_ID,
    name: "Document Assistant",
    slug: "document-assistant",
    description: "Answers a question from the documents you can access, citing the passages it used.",
    version: 1,
    enabled: true,
    is_system: true,
    scope: "platform",
    model: "",
    tools: [
      { id: "search_documents", name: "Document search", risk: "medium", requires_approval: false },
      { id: "document_metadata", name: "Document details", risk: "low", requires_approval: false },
    ],
    limits: { max_steps: 8, max_tool_calls: 6, max_duration_seconds: 300, max_tokens: 120_000 },
    approval_policy: "registry",
    has_input_schema: false,
    has_output_schema: true,
    created_by: null,
    can_edit: false,
    can_delete: false,
    created_at: "2026-10-20T08:00:00+00:00",
    updated_at: "2026-10-20T08:00:00+00:00",
    ...overrides,
  };
}

/** An agent of the organization that its author may change and delete. */
export function orgAgent(overrides: Partial<Agent> = {}): Agent {
  return agent({
    id: WRITER_ID,
    name: "Release notes writer",
    slug: "release-notes-writer",
    description: "Drafts release notes and remembers the house style.",
    version: 3,
    is_system: false,
    scope: "organization",
    model: "nexa-fast",
    tools: [
      { id: "calculator", name: "Calculator", risk: "low", requires_approval: false },
      { id: "save_memory", name: "Save to memory", risk: "high", requires_approval: true },
    ],
    limits: { max_steps: 12, max_tool_calls: 20, max_duration_seconds: 600, max_tokens: 200_000 },
    has_output_schema: false,
    created_by: "u1",
    can_edit: true,
    can_delete: true,
    ...overrides,
  });
}

export function agentDetail(base: Agent = agent(), overrides: Partial<AgentDetail> = {}): AgentDetail {
  return {
    ...base,
    instructions: "Search the documents, then answer with citations like [R1].",
    input_schema: null,
    output_schema: base.has_output_schema
      ? {
          type: "object",
          properties: { answer: { type: "string" }, answered: { type: "boolean" } },
          required: ["answer", "answered"],
        }
      : null,
    ...overrides,
  };
}

/** A running task of the Document Assistant, unless overridden. */
export function run(overrides: Partial<Run> = {}): Run {
  return {
    id: RUN_ID,
    agent_id: ASSISTANT_ID,
    agent_name: "Document Assistant",
    agent_version: 1,
    status: "running",
    goal: "What is our refund policy?",
    input: {},
    conversation_id: null,
    model: "",
    progress_message: "Searching the documents",
    step_count: 2,
    tool_call_count: 1,
    limits: { max_steps: 8, max_tool_calls: 6, max_duration_seconds: 300, max_tokens: 120_000 },
    usage: { model_calls: 2, input_tokens: 812, output_tokens: 95, cost_usd: "0.000123" },
    result: "",
    output: null,
    sources: [],
    failure_reason: null,
    error_message: null,
    cancel_requested: false,
    pause_requested: false,
    pending_approval: null,
    can_pause: true,
    can_resume: false,
    can_cancel: true,
    created_at: "2026-10-22T09:00:00+00:00",
    started_at: "2026-10-22T09:00:01+00:00",
    updated_at: "2026-10-22T09:00:05+00:00",
    completed_at: null,
    deadline_at: "2026-10-22T09:05:01+00:00",
    time_left_seconds: null,
    ...overrides,
  };
}

export function source(overrides: Partial<RunSource> = {}): RunSource {
  return {
    ref: "R1",
    document_id: "doc-1",
    chunk_id: "chunk-1",
    title: "refunds.md",
    available: true,
    ...overrides,
  };
}

/** The task once it completed, with an answer citing two sources (the second one gone since). */
export function completedRun(overrides: Partial<Run> = {}): Run {
  return run({
    status: "completed",
    progress_message: "Done",
    step_count: 4,
    result: "Refunds are possible within **30 days** [R1]. Annual plans are prorated [R2].",
    output: { answer: "Refunds within 30 days.", answered: true },
    sources: [
      source(),
      source({ ref: "R2", document_id: "doc-2", chunk_id: null, title: "plans.md", available: false }),
    ],
    can_pause: false,
    can_cancel: false,
    updated_at: "2026-10-22T09:00:20+00:00",
    completed_at: "2026-10-22T09:00:20+00:00",
    deadline_at: null,
    ...overrides,
  });
}

/** The task once it ran out of steps. */
export function failedRun(overrides: Partial<Run> = {}): Run {
  return run({
    status: "failed",
    progress_message: "",
    step_count: 8,
    failure_reason: "step_limit",
    error_message: "The agent did not finish within its step limit.",
    can_pause: false,
    can_cancel: false,
    updated_at: "2026-10-22T09:01:00+00:00",
    completed_at: "2026-10-22T09:01:00+00:00",
    deadline_at: null,
    ...overrides,
  });
}

/** The task waiting for the user to approve saving a memory. */
export function waitingRun(overrides: Partial<Run> = {}): Run {
  return run({
    status: "waiting_for_approval",
    progress_message: "Waiting for approval: Saving to your memories",
    tool_call_count: 2,
    pending_approval: approval(),
    can_pause: false,
    updated_at: "2026-10-22T09:00:10+00:00",
    deadline_at: null,
    time_left_seconds: 240,
    ...overrides,
  });
}

/** A decision step choosing a tool, unless overridden. */
export function step(overrides: Partial<Step> = {}): Step {
  return {
    id: "s1",
    sequence: 1,
    kind: "decision",
    status: "succeeded",
    summary: "Search the refund policy",
    detail: {
      action: "call_tool",
      decision: {
        action: "call_tool",
        summary: "Search the refund policy",
        tool: "search_documents",
        arguments: { query: "refund policy" },
      },
      context_dropped: 0,
    },
    tool: null,
    error_code: null,
    retry_count: 0,
    input_tokens: 400,
    output_tokens: 40,
    started_at: "2026-10-22T09:00:01+00:00",
    finished_at: "2026-10-22T09:00:02+00:00",
    duration_ms: 900,
    created_at: "2026-10-22T09:00:01+00:00",
    ...overrides,
  };
}

/** The search the decision above asked for, done. */
export function toolStep(overrides: Partial<Step> = {}): Step {
  return step({
    id: "s2",
    sequence: 2,
    kind: "tool",
    summary: "Searching documents for “refund policy”",
    detail: { tool_id: "search_documents", status: "succeeded", truncated: false },
    tool: toolActivity({
      invocation_id: "inv-5",
      tool_id: "search_documents",
      tool_name: "Document search",
      summary: "Searching documents for “refund policy”",
      risk: "medium",
      duration_ms: 12,
      result: {
        query: "refund policy",
        searched_documents: 2,
        results: [
          {
            document_id: "doc-1",
            chunk_id: "chunk-1",
            title: "refunds.md",
            excerpt: "Refunds are possible within 30 days.",
            page_start: null,
          },
        ],
      },
    }),
    input_tokens: 0,
    output_tokens: 0,
    duration_ms: 12,
    ...overrides,
  });
}

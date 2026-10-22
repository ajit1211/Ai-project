/**
 * Test data for the chat: messages shaped like the server's, the grounding and sources a
 * reply answered from documents carries (docs/api/conversations.md), and the tool calls and
 * approvals of a reply that used tools (docs/architecture/tools.md).
 */

import type { ChatItem } from "../hooks/chatThread";
import type { Grounding, Message, Source } from "../types/chat";
import type { Approval, ToolActivity, ToolInfo } from "../types/tools";

/** An assistant message of conversation c1, completed unless overridden. */
export function chatMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: "m4",
    conversation_id: "c1",
    sequence: 4,
    role: "assistant",
    content: "",
    status: "completed",
    client_message_id: "",
    model: "nexa-balanced",
    provider: "mock",
    provider_model: "mock-1",
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: null,
    latency_ms: 0,
    ttft_ms: null,
    finish_reason: "stop",
    error_code: null,
    request_id: "req_1",
    created_at: "2026-10-20T09:00:00+00:00",
    completed_at: "2026-10-20T09:00:03+00:00",
    ...overrides,
  };
}

export function grounding(overrides: Partial<Grounding> = {}): Grounding {
  return {
    mode: "documents",
    scope: "documents",
    document_ids: ["doc-1"],
    team_id: null,
    outcome: "answered",
    searched_documents: 1,
    retrieved: 3,
    supplied: 2,
    cited: 1,
    invalid_citations: [],
    retrieval_ms: 42,
    strategy: "hybrid-v1",
    retrieval_requested: "auto",
    retrieval_mode: "hybrid",
    fallback: null,
    ...overrides,
  };
}

export function source(overrides: Partial<Source> = {}): Source {
  return {
    label: "S1",
    rank: 1,
    cited: true,
    available: true,
    score: 0.82,
    document_id: "doc-1",
    chunk_id: "chunk-1",
    document_title: "Q4 report",
    filename: "q4-report.pdf",
    format: "pdf",
    section: null,
    page_start: 3,
    page_end: 3,
    row_start: null,
    row_end: null,
    char_start: 4200,
    char_end: 4800,
    excerpt: "Revenue grew 12% in the fourth quarter.",
    excerpt_truncated: false,
    ...overrides,
  };
}

/** A source whose document was deleted or is no longer shared: only these fields remain. */
export function unavailableSource(label: string, cited = true): Source {
  return {
    label,
    rank: Number(label.slice(1)),
    cited,
    available: false,
    score: null,
    document_id: null,
    chunk_id: null,
    document_title: null,
    filename: null,
    format: null,
    section: null,
    page_start: null,
    page_end: null,
    row_start: null,
    row_end: null,
    char_start: null,
    char_end: null,
    excerpt: null,
    excerpt_truncated: false,
  };
}

export function chatItem(message: Message, overrides: Partial<ChatItem> = {}): ChatItem {
  return { key: message.id, message, streaming: false, delivery: "sent", error: null, ...overrides };
}

/** A calculator call that succeeded, unless overridden. */
export function toolActivity(overrides: Partial<ToolActivity> = {}): ToolActivity {
  return {
    invocation_id: "inv-1",
    call_id: "call_1",
    tool_id: "calculator",
    tool_name: "Calculator",
    tool_version: "1",
    step: 1,
    position: 0,
    status: "succeeded",
    summary: "Calculating 6*7",
    risk: "low",
    duration_ms: 3,
    error_code: null,
    error_message: null,
    result: { expression: "6*7", result: 42, result_text: "42" },
    result_truncated: false,
    result_hidden: false,
    sources: [],
    approval: null,
    ...overrides,
  };
}

/** The exact text save_memory would save: two spaces and a line break that must survive. */
export const MEMORY_TEXT = "I prefer  metric units.\nAlways <b>bold</b>.";

/** A pending approval to save a memory, unless overridden. */
export function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "ap-1",
    invocation_id: "inv-2",
    tool_id: "save_memory",
    tool_name: "Save to memory",
    summary: "Saving to your memories",
    details: {
      action: "Save a memory",
      target: "Your private memories",
      content: MEMORY_TEXT,
      kind: "preference",
      consequences:
        "NEXA will use this in your future conversations. Only you can see it, and you can delete it at any time on the Memories page.",
    },
    risk: "high",
    status: "pending",
    arguments_hash: "a".repeat(64),
    expires_at: "2026-10-20T09:15:00+00:00",
    decided_at: null,
    ...overrides,
  };
}

/** The save_memory call a paused reply waits on. */
export function awaitingActivity(pending: Approval = approval()): ToolActivity {
  return toolActivity({
    invocation_id: pending.invocation_id,
    call_id: "call_2",
    tool_id: "save_memory",
    tool_name: "Save to memory",
    position: 1,
    status: "awaiting_approval",
    summary: "Saving to your memories",
    risk: "high",
    duration_ms: null,
    result: null,
    approval: pending,
  });
}

export function toolInfo(overrides: Partial<ToolInfo> = {}): ToolInfo {
  return {
    id: "calculator",
    name: "Calculator",
    description: "Exact arithmetic.",
    version: "1",
    category: "math",
    risk: "low",
    read_only: true,
    requires_approval: false,
    enabled: true,
    available: true,
    disabled_by: null,
    permissions: [],
    ...overrides,
  };
}

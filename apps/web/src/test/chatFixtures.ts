/**
 * Test data for the chat: messages shaped like the server's, and the grounding and sources a
 * reply answered from documents carries (docs/api/conversations.md).
 */

import type { ChatItem } from "../hooks/chatThread";
import type { Grounding, Message, Source } from "../types/chat";

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

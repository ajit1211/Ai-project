/**
 * Types of the conversation API (docs/api/conversations.md). They mirror the server's shapes
 * field for field; `src/api/conversations.ts` is the only caller.
 */

import type { Usage } from "./ai";

export type ConversationStatus = "active" | "archived";

export interface Conversation {
  id: string;
  /** Empty until the first user message names it (or the user renames it). */
  title: string;
  /** A logical model name from `GET /ai/models/`; "" means the organization's default. */
  model: string;
  status: ConversationStatus;
  message_count: number;
  /** True while a reply is being generated. */
  generating: boolean;
  created_at: string;
  updated_at: string;
  /** Null until the first message. Activity is this, or `created_at` without messages. */
  last_message_at: string | null;
}

export type MessageRole = "user" | "assistant";

/** Always `completed` for user messages; any of these for assistant messages. */
export type MessageStatus = "pending" | "streaming" | "completed" | "failed" | "cancelled";

/**
 * How a grounded reply went. "uncited": answered without citing the documents (unverified);
 * "no_evidence": nothing relevant was found, so the model was not asked.
 */
export type GroundingOutcome = "pending" | "answered" | "uncited" | "no_evidence" | "failed" | "cancelled";

/** How passages are found: by their keywords, by meaning (semantic), or both (hybrid). */
export type RetrievalMode = "lexical" | "semantic" | "hybrid";

/** What a grounded question may ask for; "auto" is the deployment's default. */
export type RetrievalChoice = "auto" | RetrievalMode;

/** The ways of searching documents this deployment supports (`GET /ai/retrieval/`). */
export interface RetrievalOptions {
  /** Only the modes that can run here; just "lexical" when semantic search does not. */
  modes: RetrievalMode[];
  /** What "auto" uses. */
  default_mode: RetrievalMode;
  semantic_available: boolean;
  /** A code ("disabled", "pgvector_missing", ...) when semantic search does not run here. */
  semantic_unavailable_reason: string | null;
  /** The logical embedding model's key, when semantic search runs. */
  embedding_model: string | null;
}

/** How a reply was answered from documents (null on ordinary messages). */
export interface Grounding {
  mode: "documents";
  scope: "documents" | "team" | "library";
  document_ids: string[];
  team_id: string | null;
  outcome: GroundingOutcome;
  searched_documents: number;
  retrieved: number;
  supplied: number;
  cited: number;
  /** Citation labels in the answer that match none of its sources. */
  invalid_citations: string[];
  retrieval_ms: number | null;
  strategy: string;
  // Replies from before semantic search (Phase 09) lack the three fields below: they were
  // found by keywords, with no fallback.
  /** The mode the question asked for. */
  retrieval_requested?: RetrievalChoice;
  /** The mode that ran. */
  retrieval_mode?: RetrievalMode;
  /**
   * Why keyword search ran instead of what was asked for ("query_embedding_failed",
   * "vector_search_failed", "semantic_disabled", ...); null when nothing fell back.
   */
  fallback?: string | null;
}

/**
 * A passage given to the model. When `available` is false (its document was deleted or is no
 * longer shared) only `label`, `rank` and `cited` are set. The excerpt is untrusted text.
 */
export interface Source {
  /** "S1", "S2", ...: what the answer cites as [S1]. */
  label: string;
  rank: number;
  cited: boolean;
  available: boolean;
  score: number | null;
  document_id: string | null;
  chunk_id: string | null;
  document_title: string | null;
  filename: string | null;
  format: string | null;
  /** A heading path, when the document has headings. */
  section: string | null;
  page_start: number | null;
  page_end: number | null;
  row_start: number | null;
  row_end: number | null;
  char_start: number | null;
  char_end: number | null;
  excerpt: string | null;
  excerpt_truncated: boolean;
}

/** What to answer from. Exactly one of the three scopes. */
export type GroundingRequest = (
  | { document_ids: string[] }
  | { team_id: string }
  | { all_documents: true }
) & {
  /** 1 to 12. */
  max_sources?: number;
  /** How to search; left out for "auto" (the deployment's default). */
  retrieval_mode?: RetrievalChoice;
};

export interface Message {
  id: string;
  conversation_id: string;
  sequence: number;
  role: MessageRole;
  /** A failed or cancelled reply keeps whatever text had arrived. */
  content: string;
  status: MessageStatus;
  client_message_id: string;
  // The generation fields, from here to `request_id`, are empty or zero on user messages.
  model: string;
  provider: string;
  provider_model: string;
  input_tokens: number;
  output_tokens: number;
  /** A decimal string, or null when the model has no price configured. */
  cost_usd: string | null;
  latency_ms: number;
  ttft_ms: number | null;
  /** "stop", "length" (cut off by the output limit), "content_filter", or "" until done. */
  finish_reason: string;
  /** Names the failure of a failed or cancelled reply. */
  error_code: string | null;
  request_id: string;
  created_at: string;
  completed_at: string | null;
  /** How a reply was answered from documents; null (or absent) for ordinary messages. */
  grounding?: Grounding | null;
  /** The passages of a grounded reply; empty (or absent) for ordinary messages. */
  sources?: Source[];
}

/** `meta` of `GET /conversations/`. */
export interface ConversationListMeta {
  next_cursor: string | null;
  has_more: boolean;
}

/** `meta` of `GET /conversations/{id}/messages/`. */
export interface MessageListMeta {
  /** A message `sequence`: pass it as `before` to get the page of older messages. */
  next_before: number | null;
  has_more: boolean;
}

export interface ConversationPage {
  data: Conversation[];
  meta: ConversationListMeta;
}

export interface MessagePage {
  /** Ordered by `sequence`, oldest first. */
  data: Message[];
  meta: MessageListMeta;
}

export interface ListConversationsParams {
  /** Default `active`. */
  status?: ConversationStatus;
  /** Title search. */
  q?: string;
  /** 1 to 100, default 30. */
  limit?: number;
  /** `meta.next_cursor` of the previous page. */
  cursor?: string | null;
}

export interface ListMessagesParams {
  /** 1 to 200, default 50. */
  limit?: number;
  /** Only messages older than this `sequence`; the newest ones without it. */
  before?: number | null;
}

export interface CreateConversationBody {
  title?: string;
  model?: string;
}

export interface UpdateConversationBody {
  /** An empty title restores the automatic title. */
  title?: string;
  model?: string;
  status?: ConversationStatus;
}

export interface SendMessageBody {
  /** 1 to 100 000 characters. */
  content: string;
  /** Makes the submission idempotent: the same id again replays the original exchange. */
  client_message_id?: string;
  /** Changes the conversation's model for this and later messages. */
  model?: string;
  /** Answer from documents. Left out for ordinary chat. Grounded questions: 4 000 characters. */
  grounding?: GroundingRequest;
}

export interface RegenerateBody {
  model?: string;
}

/** A user message and its reply: the JSON answer to a send, and what a replay returns. */
export interface Exchange {
  user_message: Message;
  assistant_message: Message;
}

export interface CancelResult {
  cancelled: boolean;
  /** The reply being stopped, or null when nothing was running. */
  message_id: string | null;
}

/** The `X-NEXA-Stream-Protocol` of the event stream below. */
export const CHAT_PROTOCOL = "nexa.chat.v1";

/* Events of the `nexa.chat.v1` stream, tagged with the SSE event name as `type`. */

export interface MessageStartEvent {
  type: "message_start";
  protocol: string;
  conversation_id: string;
  request_id: string | null;
  /** The message being answered (for a regeneration, the existing one). */
  user_message: Message;
  /** The reply, status `pending`. Every later event names its id. */
  assistant_message: Message;
}

/** A grounded reply only: what the search found, after `message_start` and before any text. */
export interface SourcesEvent {
  type: "sources";
  message_id: string;
  searched_documents: number;
  /** May be empty: then a fixed "nothing found" sentence follows. */
  sources: Source[];
}

export interface TextDeltaEvent {
  type: "text_delta";
  message_id: string;
  text: string;
}

/** The Usage object of the reply, flattened into the event payload. */
export interface ChatUsageEvent extends Usage {
  type: "usage";
  message_id: string;
}

export interface MessageCompleteEvent {
  type: "message_complete";
  /** Status `completed`. */
  message: Message;
}

export interface MessageCancelledEvent {
  type: "message_cancelled";
  /** Status `cancelled`, with the text that had arrived. */
  message: Message;
}

export interface ChatErrorEvent {
  type: "error";
  message_id: string;
  code: string;
  message: string;
  retryable: boolean;
  /** Seconds to wait before retrying, when the failure says so. */
  retry_after: number | null;
  request_id: string | null;
  /** Status `failed`, with the text that had arrived. */
  assistant_message: Message;
}

/** Exactly one of these ends every stream. */
export type ChatTerminalEvent = MessageCompleteEvent | MessageCancelledEvent | ChatErrorEvent;

export type ChatStreamEvent =
  | MessageStartEvent
  | SourcesEvent
  | TextDeltaEvent
  | ChatUsageEvent
  | ChatTerminalEvent;

export type ChatStreamEventName = ChatStreamEvent["type"];

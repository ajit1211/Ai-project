/**
 * Types of the AI Gateway API (docs/architecture/ai-gateway.md, "API" and "Streaming").
 * They mirror the server's shapes field for field; `src/api/ai.ts` is the only caller.
 */

export interface ModelInfo {
  name: string;
  display_name: string;
  description: string;
  is_default: boolean;
  capabilities: string[];
  context_window: number;
  max_output_tokens: number;
}

export type ChatRole = "system" | "user" | "assistant";

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface JsonSchemaFormat {
  type: "json_schema";
  name?: string;
  schema: Record<string, unknown>;
}

export interface ChatRequest {
  model?: string;
  messages: ChatMessage[];
  temperature?: number;
  max_output_tokens?: number;
  response_format?: JsonSchemaFormat;
  stop?: string[];
  feature: "playground";
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
  cache_write_input_tokens: number;
  /** A decimal string, or null when the model has no price configured. */
  cost_usd: string | null;
}

export type FinishReason = "stop" | "length" | "content_filter" | "tool_calls" | "error";

export interface ChatResponse {
  id: string;
  model: string;
  provider: string;
  provider_model: string;
  content: string;
  parsed: unknown;
  finish_reason: FinishReason;
  usage: Usage;
  latency_ms: number;
  attempts: number;
  fallback_used: boolean;
}

/* Events of POST /ai/chat/stream/, tagged with the SSE event name as `type`. */

export interface MessageStartEvent {
  type: "message.start";
  id: string;
  model: string;
  provider: string;
  provider_model: string;
  request_id: string;
}

export interface MessageDeltaEvent {
  type: "message.delta";
  text: string;
}

/** The Usage object, flattened into the event payload. */
export interface UsageEvent extends Usage {
  type: "usage";
}

export interface MessageEndEvent {
  type: "message.end";
  finish_reason: FinishReason;
  latency_ms: number;
  attempts: number;
  fallback_used: boolean;
}

/** The last event of a stream that failed after it started. */
export interface StreamErrorEvent {
  type: "error";
  code: string;
  message: string;
  retryable: boolean;
  request_id?: string | null;
}

export type StreamEvent =
  | MessageStartEvent
  | MessageDeltaEvent
  | UsageEvent
  | MessageEndEvent
  | StreamErrorEvent;

export type StreamEventName = StreamEvent["type"];

/* GET /ai/usage/ */

export interface UsageRow {
  requests: number;
  errors: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: string | null;
  unpriced_requests: number;
}

export interface UsageByModel extends UsageRow {
  alias: string;
  provider: string;
  provider_model: string;
}

export interface UsageByDay extends UsageRow {
  date: string;
}

export interface RecentEvent {
  id: string;
  created_at: string;
  user_id: string;
  feature: string;
  alias: string;
  provider: string;
  provider_model: string;
  status: string;
  error_code: string | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: string | null;
  latency_ms: number | null;
  ttft_ms: number | null;
  stream: boolean;
  fallback_used: boolean;
}

export type UsageScope = "me" | "organization";

export interface UsageSummary {
  scope: UsageScope;
  organization_id: string;
  period: { days: number; since: string };
  totals: UsageRow;
  by_model: UsageByModel[];
  by_day: UsageByDay[];
  recent: RecentEvent[];
}

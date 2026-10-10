/**
 * The AI Gateway endpoints (docs/architecture/ai-gateway.md, "API"). This is the only
 * module that calls `/ai/...`; pages go through the playground hook.
 */

import type { SseFrame } from "../services/ai/sse";
import type {
  ChatRequest,
  ChatResponse,
  ModelInfo,
  StreamErrorEvent,
  StreamEvent,
  StreamEventName,
  UsageSummary,
} from "../types/ai";
import { ApiError, api } from "./client";
import { frameObject, notAStream, postForStream } from "./stream";

/** A stream that failed after it started: the server's `error` event. */
export class AiStreamError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId: string | null;

  constructor(event: Omit<StreamErrorEvent, "type">) {
    super(event.message);
    this.name = "AiStreamError";
    this.code = event.code;
    this.retryable = event.retryable;
    this.requestId = event.request_id ?? null;
  }
}

export interface RequestControl {
  signal?: AbortSignal;
}

export async function listModels(): Promise<ModelInfo[]> {
  const { data } = await api<ModelInfo[]>("/ai/models/");
  return data;
}

export async function chat(request: ChatRequest, control: RequestControl = {}): Promise<ChatResponse> {
  const { data } = await api<ChatResponse>("/ai/chat/", {
    method: "POST",
    body: request,
    signal: control.signal,
  });
  return data;
}

/** Usage of the signed-in user in the active organization over the last `days` days. */
export async function getMyUsage(days = 30): Promise<UsageSummary> {
  const { data } = await api<UsageSummary>(`/ai/usage/?scope=me&days=${String(days)}`);
  return data;
}

const STREAM_EVENT_NAMES: ReadonlySet<string> = new Set<StreamEventName>([
  "message.start",
  "message.delta",
  "usage",
  "message.end",
  "error",
]);

function isStreamEventName(name: string): name is StreamEventName {
  return STREAM_EVENT_NAMES.has(name);
}

/**
 * Type one SSE frame by its event name. Frames with an unknown name or a body that is not
 * a JSON object are dropped (null), so a newer server never breaks an older client.
 */
export function parseStreamEvent(frame: SseFrame): StreamEvent | null {
  if (!isStreamEventName(frame.event)) return null;
  const payload = frameObject(frame);
  if (!payload) return null;
  // The server owns these shapes (architecture doc, "Streaming"); the client trusts them.
  return { ...payload, type: frame.event } as unknown as StreamEvent;
}

/**
 * One streamed completion. Yields the typed events of the stream in order and ends after
 * `message.end`. Throws `ApiError` when the request is refused before the stream starts
 * (a JSON error envelope with a real HTTP status: 401, 403, 422, 429, ...), `AiStreamError`
 * when the server sends an `error` event mid-stream, and the fetch's AbortError when
 * `signal` is aborted. Leaving the loop early cancels the request.
 */
export async function* streamChat(
  request: ChatRequest,
  control: RequestControl = {},
): AsyncGenerator<StreamEvent, void, undefined> {
  // Refused before the stream began: postForStream throws the envelope's ApiError.
  const reply = await postForStream<unknown>("/ai/chat/stream/", request, {
    accept: "text/event-stream, application/json",
    signal: control.signal,
  });
  // A success envelope instead of a stream means the server misbehaved.
  if (reply.kind === "json") throw notAStream(reply.status, reply.requestId);

  for await (const frame of reply.frames) {
    const event = parseStreamEvent(frame);
    if (!event) continue;
    if (event.type === "error") throw new AiStreamError(event);
    yield event;
    if (event.type === "message.end") return;
  }
  // Cut off (a proxy timeout, a dropped connection) before the answer was complete.
  throw new ApiError(
    0,
    { code: "NETWORK_ERROR", message: "The connection closed before the answer finished." },
    null,
  );
}

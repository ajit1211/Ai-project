/**
 * The request half of the streaming endpoints (`/ai/chat/stream/` and the conversation sends):
 * POST a JSON body asking for `text/event-stream`, then hand back either the SSE frames or,
 * when the server answered with JSON instead, the unwrapped envelope. A JSON error envelope
 * (refused before the stream began: 401, 404, 409, 422, 429, ...) throws its `ApiError` here,
 * so callers only see the two successful shapes. Typing the frames is the caller's job.
 */

import { parseSSE, type SseFrame } from "../services/ai/sse";
import { ApiError, authorizedFetch, parseEnvelope } from "./client";

export type StreamReply<T> =
  | { kind: "stream"; frames: AsyncGenerator<SseFrame, void, undefined> }
  | { kind: "json"; data: T; status: number; requestId: string | null };

export interface StreamRequest {
  /** The Accept header: `text/event-stream`, optionally with a JSON fallback. */
  accept: string;
  /** Cancels the request and, once it started, the stream. */
  signal?: AbortSignal;
}

export async function postForStream<T>(
  path: string,
  body: unknown,
  { accept, signal }: StreamRequest,
): Promise<StreamReply<T>> {
  const response = await authorizedFetch(path, {
    method: "POST",
    headers: { Accept: accept, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  const contentType = response.headers.get("Content-Type") ?? "";
  if (contentType.startsWith("text/event-stream") && response.body) {
    return { kind: "stream", frames: parseSSE(response.body) };
  }
  // Not a stream: a JSON envelope. parseEnvelope throws the ApiError of a refusal.
  const { data } = await parseEnvelope<T>(response);
  return {
    kind: "json",
    data,
    status: response.status,
    requestId: response.headers.get("X-Request-ID"),
  };
}

/** The error for a server that answered a stream request with something unexpected. */
export function notAStream(status: number, requestId: string | null): ApiError {
  return new ApiError(status, { code: "NETWORK_ERROR", message: "Expected an event stream." }, requestId);
}

/**
 * The JSON object a frame carries, or null when its data is not one. Callers drop such
 * frames, as they drop unknown event names, so a newer server never breaks an older client.
 */
export function frameObject(frame: SseFrame): Record<string, unknown> | null {
  let payload: unknown;
  try {
    payload = JSON.parse(frame.data);
  } catch {
    return null;
  }
  return typeof payload === "object" && payload !== null && !Array.isArray(payload)
    ? (payload as Record<string, unknown>)
    : null;
}

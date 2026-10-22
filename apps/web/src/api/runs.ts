/**
 * The task endpoints (docs/api/agents.md#runs), served by Runtime, which carries tasks out:
 * starting one, the caller's tasks, one task with its steps, its live progress, pausing,
 * resuming and cancelling it, and deciding on the action it waits for. This is the only module
 * that calls `/runs/...`; the task pages go through `src/hooks/useRun.ts` (one task) and
 * `src/hooks/useRuns.ts` (the history), and the new-task form calls `submitRun`.
 *
 * Starting a task is idempotent: the form sends one `idempotency_key` however often it is
 * submitted, and the server answers a repeat with the task the key already started.
 */

import { parseSSE, type SseFrame } from "../services/ai/sse";
import type {
  ListRunsParams,
  ListStepsParams,
  Run,
  RunControl,
  RunDecisionBody,
  RunEvent,
  RunPage,
  Step,
  SubmitRunBody,
} from "../types/agents";
import { ApiError, api, authorizedFetch, parseEnvelope } from "./client";
import { frameObject, notAStream } from "./stream";

export interface RequestControl {
  /** Cancels the request; for the progress stream, closes it. */
  signal?: AbortSignal;
}

const runPath = (id: string): string => `/runs/${encodeURIComponent(id)}/`;

/**
 * A fresh idempotency key for one submission form. crypto.randomUUID needs a secure context;
 * plain-http LAN access has only getRandomValues.
 */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * Starts a task; it comes back queued (202), or as the task the same idempotency key already
 * started (200). Refused with 404 AGENT_UNAVAILABLE (no such agent, or it is switched off),
 * 403, 422 (the goal, or `input` against the agent's input schema: fields "input" or
 * "input.<property>"), and 429 RUN_LIMIT_REACHED while too many tasks are in progress.
 */
export async function submitRun(body: SubmitRunBody): Promise<Run> {
  const { data } = await api<Run>("/runs/", { method: "POST", body });
  return data;
}

/** The caller's tasks, newest first, one page at a time. */
export async function listRuns(params: ListRunsParams = {}, control: RequestControl = {}): Promise<RunPage> {
  const query = new URLSearchParams();
  if (params.status) query.set("status", params.status);
  if (params.agent_id) query.set("agent_id", params.agent_id);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.cursor) query.set("cursor", params.cursor);
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const { data, meta } = await api<Run[]>(`/runs/${suffix}`, { signal: control.signal });
  const nextCursor = typeof meta?.next_cursor === "string" && meta.next_cursor !== "" ? meta.next_cursor : null;
  return { runs: data, nextCursor, hasMore: meta?.has_more === true && nextCursor !== null };
}

export async function getRun(id: string, control: RequestControl = {}): Promise<Run> {
  const { data } = await api<Run>(runPath(id), { signal: control.signal });
  return data;
}

/** The task's steps, in order; `after` a sequence number for the ones after it. */
export async function listSteps(
  id: string,
  params: ListStepsParams = {},
  control: RequestControl = {},
): Promise<Step[]> {
  const query = new URLSearchParams();
  if (params.after !== undefined) query.set("after", String(params.after));
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const { data } = await api<Step[]>(`${runPath(id)}steps/${suffix}`, { signal: control.signal });
  return data;
}

/**
 * Pauses a queued task at once, or a running one at its next checkpoint. 409 RUN_NOT_ACTIVE
 * when the task cannot be paused.
 */
export async function pauseRun(id: string): Promise<RunControl> {
  const { data } = await api<RunControl>(`${runPath(id)}pause/`, { method: "POST" });
  return data;
}

/** Puts a paused task back in the queue. 409 RUN_NOT_ACTIVE when it is not paused. */
export async function resumeRun(id: string): Promise<RunControl> {
  const { data } = await api<RunControl>(`${runPath(id)}resume/`, { method: "POST" });
  return data;
}

/**
 * Cancels a task: at once unless it is running, then at its next checkpoint. 409
 * RUN_NOT_ACTIVE once it has ended.
 */
export async function cancelRun(id: string): Promise<RunControl> {
  const { data } = await api<RunControl>(`${runPath(id)}cancel/`, { method: "POST" });
  return data;
}

/**
 * Approves or declines the action a task waits for; resolves to the task, queued again. The
 * body carries the `arguments_hash` of the approval shown to the user. Refused with 409
 * TOOL_APPROVAL_STALE (the action changed), TOOL_APPROVAL_EXPIRED (too late: the task has
 * failed), TOOL_APPROVAL_CLOSED (already decided), and 404.
 */
export async function decideRunApproval(
  runId: string,
  approvalId: string,
  body: RunDecisionBody,
): Promise<Run> {
  const { data } = await api<Run>(`${runPath(runId)}approvals/${encodeURIComponent(approvalId)}/`, {
    method: "POST",
    body,
  });
  return data;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * Type one SSE frame by its event name. Frames with an unknown name or without the object
 * their name promises are dropped (null), so a newer server never breaks an older client.
 */
export function parseRunEvent(frame: SseFrame): RunEvent | null {
  const payload = frameObject(frame);
  if (!payload) return null;
  // The server owns these shapes (docs/api/agents.md); the client trusts them.
  switch (frame.event) {
    case "run":
    case "end":
      return isObject(payload.run) ? { type: frame.event, run: payload.run as unknown as Run } : null;
    case "step":
      return isObject(payload.step) ? { type: "step", step: payload.step as unknown as Step } : null;
    default:
      return null;
  }
}

/**
 * A task's progress as it happens. Yields `run` whenever the task changes, `step` for each new
 * or changed step (the first events describe the task and every step so far), and ends after
 * `end`, which carries the final task. Comment lines (the server's keep-alives) are skipped.
 *
 * Throws `ApiError` when the stream is refused (404 for a task that is not the caller's, ...),
 * `ApiError` NETWORK_ERROR when the stream closes before `end`, and the fetch's AbortError
 * when `signal` is aborted. Aborting, or leaving the loop early, closes the stream.
 */
export async function* streamRunEvents(
  id: string,
  control: RequestControl = {},
): AsyncGenerator<RunEvent, void, undefined> {
  const response = await authorizedFetch(`${runPath(id)}events/`, {
    headers: { Accept: "text/event-stream" },
    signal: control.signal,
  });
  const contentType = response.headers.get("Content-Type") ?? "";
  if (!contentType.startsWith("text/event-stream") || !response.body) {
    await parseEnvelope(response); // throws the refusal's ApiError
    throw notAStream(response.status, response.headers.get("X-Request-ID"));
  }
  for await (const frame of parseSSE(response.body)) {
    const event = parseRunEvent(frame);
    if (!event) continue;
    yield event;
    if (event.type === "end") return;
  }
  throw new ApiError(
    0,
    { code: "NETWORK_ERROR", message: "The progress stream closed before the task finished." },
    null,
  );
}

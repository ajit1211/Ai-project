/**
 * The conversation endpoints (docs/api/conversations.md). This is the only module that calls
 * `/conversations/...`; pages go through the chat hooks in `src/hooks/`.
 */

import type { SseFrame } from "../services/ai/sse";
import { describeErrorCode, isTemporaryCode } from "../services/ai/errors";
import {
  CHAT_PROTOCOL,
  type CancelResult,
  type ChatStreamEvent,
  type ChatStreamEventName,
  type Conversation,
  type ConversationPage,
  type CreateConversationBody,
  type Exchange,
  type ListConversationsParams,
  type ListMessagesParams,
  type Message,
  type MessagePage,
  type RegenerateBody,
  type SendMessageBody,
  type Source,
  type UpdateConversationBody,
} from "../types/chat";
import type { Approval, ApprovalDecisionBody } from "../types/tools";
import { ApiError, api } from "./client";
import { frameObject, notAStream, postForStream } from "./stream";

export interface RequestControl {
  /** Cancels the request; for a generation, aborting also stops the reply on the server. */
  signal?: AbortSignal;
}

const conversationPath = (id: string): string => `/conversations/${encodeURIComponent(id)}/`;

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

/** Your conversations, newest activity first, one page at a time. */
export async function listConversations(
  params: ListConversationsParams = {},
  control: RequestControl = {},
): Promise<ConversationPage> {
  const query = new URLSearchParams({ status: params.status ?? "active" });
  if (params.q) query.set("q", params.q);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.cursor) query.set("cursor", params.cursor);
  const { data, meta } = await api<Conversation[]>(`/conversations/?${query.toString()}`, {
    signal: control.signal,
  });
  return {
    data,
    meta: { next_cursor: stringOrNull(meta?.next_cursor), has_more: meta?.has_more === true },
  };
}

export async function createConversation(body: CreateConversationBody = {}): Promise<Conversation> {
  const { data } = await api<Conversation>("/conversations/", { method: "POST", body });
  return data;
}

export async function getConversation(
  id: string,
  control: RequestControl = {},
): Promise<Conversation> {
  const { data } = await api<Conversation>(conversationPath(id), { signal: control.signal });
  return data;
}

/** Rename, change the model, archive or restore. */
export async function updateConversation(
  id: string,
  body: UpdateConversationBody,
): Promise<Conversation> {
  const { data } = await api<Conversation>(conversationPath(id), { method: "PATCH", body });
  return data;
}

/** Deletes the conversation and all of its messages. */
export async function deleteConversation(id: string): Promise<void> {
  await api(conversationPath(id), { method: "DELETE" });
}

/** One page of messages, oldest first: the newest ones, or those older than `before`. */
export async function listMessages(
  id: string,
  params: ListMessagesParams = {},
  control: RequestControl = {},
): Promise<MessagePage> {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.before !== undefined && params.before !== null) {
    query.set("before", String(params.before));
  }
  const search = query.toString();
  const { data, meta } = await api<Message[]>(`${conversationPath(id)}messages/${search && `?${search}`}`, {
    signal: control.signal,
  });
  return {
    data,
    meta: { next_before: numberOrNull(meta?.next_before), has_more: meta?.has_more === true },
  };
}

/** Stops the reply being generated; its stream then ends with `message_cancelled`. */
export async function cancelGeneration(id: string): Promise<CancelResult> {
  const { data } = await api<CancelResult>(`${conversationPath(id)}cancel/`, { method: "POST" });
  return data;
}

/**
 * Send a message and stream the reply. Yields the typed events of `nexa.chat.v1` in order:
 * `message_start`, then (grounded sends only) `sources`, then `text_delta` and `usage` (and,
 * when the reply uses tools, `tool_call` and `tool_result` between them), then exactly one
 * terminal event (`message_complete`, `message_cancelled`, `error`, or `approval_required`
 * when the reply pauses for the user's decision on an action), after which the generator
 * ends. `body.grounding` answers from documents; leave it out otherwise.
 *
 * Throws `ApiError` when the send is refused before anything is stored (a JSON error
 * envelope with its real status: 404, 409 GENERATION_IN_PROGRESS, 409 CONFLICT, 409
 * TOOL_APPROVAL_PENDING while an earlier reply waits for a decision, 422, 429;
 * with grounding also 404 DOCUMENT_UNAVAILABLE, 409 DOCUMENT_NOT_INDEXED, 422
 * RAG_INVALID_SCOPE and 503 when answers from documents are switched off),
 * `ApiError` NETWORK_ERROR when the connection closes before a terminal event, and the
 * fetch's AbortError when `signal` is aborted. Aborting, or leaving the loop early, closes
 * the stream, which also stops the reply on the server.
 *
 * Sending a `client_message_id` that was sent before makes the server answer
 * `200 application/json` with the earlier exchange instead of a stream. That replay is
 * yielded as the events a stream would have produced: a synthetic `message_start` (the
 * reply shown as `pending`), then the terminal event matching the stored reply:
 * `message_complete` when it completed (the usual case), `message_cancelled` when it was
 * stopped, `approval_required` when it is paused, `error` (from its `error_code`) when it
 * failed. No `text_delta`, `usage` or tool events: the terminal event's message carries the
 * text, the token counts and the tool calls.
 */
export function sendMessage(
  id: string,
  body: SendMessageBody,
  control: RequestControl = {},
): AsyncGenerator<ChatStreamEvent, void, undefined> {
  return generation(`${conversationPath(id)}messages/`, body, control);
}

/**
 * Generate the last reply again (the previous one, whatever its state, is replaced). Same
 * events and errors as `sendMessage`; 409 CONFLICT when there is nothing to regenerate.
 */
export function regenerate(
  id: string,
  body: RegenerateBody = {},
  control: RequestControl = {},
): AsyncGenerator<ChatStreamEvent, void, undefined> {
  return generation(`${conversationPath(id)}regenerate/`, body, control);
}

/**
 * Approve or reject the action a paused reply (`awaiting_approval`) proposed, and stream the
 * same reply as it continues: `message_start` (that reply, status `streaming`, with its text
 * so far), `tool_call` and `tool_result` for the decided action, more text, then one terminal
 * event (possibly `approval_required` again). The body must carry the `arguments_hash` of the
 * approval shown to the user.
 *
 * Throws `ApiError` 409 TOOL_APPROVAL_STALE (the action changed: reload the messages),
 * TOOL_APPROVAL_EXPIRED (too late: the reply is now cancelled), TOOL_APPROVAL_CLOSED (already
 * decided), and 404 for an approval that is not the caller's. Aborting works as for a send.
 */
export function decideApproval(
  conversationId: string,
  approvalId: string,
  body: ApprovalDecisionBody,
  control: RequestControl = {},
): AsyncGenerator<ChatStreamEvent, void, undefined> {
  return generation(
    `${conversationPath(conversationId)}approvals/${encodeURIComponent(approvalId)}/`,
    body,
    control,
  );
}

/**
 * The passages of a grounded reply, re-authorized now, with whole passages (excerpts of up to
 * 4 000 characters) rather than the stream's short ones.
 */
export async function listMessageSources(
  conversationId: string,
  messageId: string,
  control: RequestControl = {},
): Promise<Source[]> {
  const { data } = await api<Source[]>(
    `${conversationPath(conversationId)}messages/${encodeURIComponent(messageId)}/sources/`,
    { signal: control.signal },
  );
  return data;
}

const EVENT_NAMES: ReadonlySet<string> = new Set<ChatStreamEventName>([
  "message_start",
  "sources",
  "text_delta",
  "usage",
  "tool_call",
  "tool_result",
  "message_complete",
  "message_cancelled",
  "error",
  "approval_required",
]);

function isEventName(name: string): name is ChatStreamEventName {
  return EVENT_NAMES.has(name);
}

/**
 * Type one SSE frame by its event name. Frames with an unknown name or a body that is not a
 * JSON object are dropped (null), so a newer server never breaks an older client.
 */
export function parseChatEvent(frame: SseFrame): ChatStreamEvent | null {
  if (!isEventName(frame.event)) return null;
  const payload = frameObject(frame);
  if (!payload) return null;
  // The server owns these shapes (docs/api/conversations.md); the client trusts them.
  return { ...payload, type: frame.event } as unknown as ChatStreamEvent;
}

function isTerminal(event: ChatStreamEvent): boolean {
  return (
    event.type === "message_complete" ||
    event.type === "message_cancelled" ||
    event.type === "error" ||
    event.type === "approval_required"
  );
}

function isExchange(value: unknown): value is Exchange {
  return (
    typeof value === "object" &&
    value !== null &&
    "user_message" in value &&
    "assistant_message" in value
  );
}

async function* generation(
  path: string,
  body: SendMessageBody | RegenerateBody | ApprovalDecisionBody,
  control: RequestControl,
): AsyncGenerator<ChatStreamEvent, void, undefined> {
  const reply = await postForStream<unknown>(path, body, {
    accept: "text/event-stream",
    signal: control.signal,
  });
  if (reply.kind === "json") {
    if (!isExchange(reply.data)) throw notAStream(reply.status, reply.requestId);
    yield* replayEvents(reply.data);
    return;
  }
  for await (const frame of reply.frames) {
    const event = parseChatEvent(frame);
    if (!event) continue;
    yield event;
    // Exactly one terminal event per stream: nothing after it is read.
    if (isTerminal(event)) return;
  }
  throw new ApiError(
    0,
    { code: "NETWORK_ERROR", message: "The connection closed before the reply finished." },
    null,
  );
}

/** The approval a paused reply waits for: the pending one among its tool calls. */
function pendingApproval(reply: Message): Approval | null {
  const found = (reply.tools ?? []).find((tool) => tool.approval?.status === "pending");
  return found?.approval ?? null;
}

/** The replay of an earlier submission as stream events (see `sendMessage`). */
function* replayEvents(exchange: Exchange): Generator<ChatStreamEvent, void, undefined> {
  const { user_message: user, assistant_message: reply } = exchange;
  const requestId = reply.request_id || null;
  yield {
    type: "message_start",
    protocol: CHAT_PROTOCOL,
    conversation_id: reply.conversation_id,
    request_id: requestId,
    user_message: user,
    assistant_message: { ...reply, content: "", status: "pending" },
  };
  const approval = reply.status === "awaiting_approval" ? pendingApproval(reply) : null;
  if (reply.status === "completed") {
    yield { type: "message_complete", message: reply };
  } else if (reply.status === "cancelled") {
    yield { type: "message_cancelled", message: reply };
  } else if (approval) {
    yield { type: "approval_required", message: reply, approval };
  } else {
    // Failed (or, if the server lost track of it, never finished or paused without an
    // approval to decide): report it as a failure.
    const code = reply.error_code ?? "AI_INTERNAL_ERROR";
    yield {
      type: "error",
      message_id: reply.id,
      code,
      message: describeErrorCode(code),
      retryable: isTemporaryCode(code),
      retry_after: null,
      request_id: requestId,
      assistant_message: { ...reply, status: "failed" },
    };
  }
}

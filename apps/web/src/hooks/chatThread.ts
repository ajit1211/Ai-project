/**
 * The chat's message state as pure functions: merging pages, committing a finished reply,
 * and the rows the message list renders. `useChat` owns the React state around them.
 */

import { describeErrorCode, hintForCode } from "../services/ai/errors";
import type { Usage } from "../types/ai";
import type {
  Conversation,
  Grounding,
  GroundingRequest,
  Message,
  MessageStatus,
  Source,
} from "../types/chat";

/** What the UI says about a failure. */
export interface ChatError {
  code: string | null;
  message: string;
  /** What to do about it, when retrying can help. */
  retryHint: string | null;
}

/** The failure as one sentence, with its hint. */
export function describeChatError(error: ChatError): string {
  return error.retryHint ? `${error.message} ${error.retryHint}` : error.message;
}

/** A send's text and idempotency key, kept so that the same submission can be retried. */
export interface Submission {
  content: string;
  clientMessageId: string;
  /** ISO time of the first attempt. */
  submittedAt: string;
  /** The documents to answer from; absent for ordinary chat. */
  grounding?: GroundingRequest;
}

/**
 * Where the document search of a grounded reply stands while it streams: searching (how many
 * documents, when known), then what it found, until the answer's text arrives.
 */
export type Retrieval =
  | { state: "searching"; documents: number | null }
  | { state: "found"; searched: number; found: number };

/** A submission the server refused or never received: an unsent bubble with Retry. */
export interface UnsentSubmission extends Submission {
  error: ChatError;
}

/** The open conversation as the server last described it, plus what this session added. */
export interface Thread {
  conversationId: string;
  conversation: Conversation | null;
  /** In sequence order. */
  messages: Message[];
  nextBefore: number | null;
  hasOlder: boolean;
  /** Why the conversation could not be loaded. */
  error: string | null;
}

/** A reply being generated. It is shown over the thread until its terminal event. */
export interface LiveReply {
  conversationId: string;
  kind: "send" | "regenerate";
  /** The send being answered (null for a regeneration). */
  submission: Submission | null;
  /** From `message_start`. */
  userMessage: Message | null;
  reply: Message | null;
  /** The text so far, from `text_delta`. */
  text: string;
  /** From the `usage` event; the terminal event's message has the final numbers. */
  usage: Usage | null;
  /** From the `sources` event of a grounded reply. */
  found: { searched: number; sources: Source[] } | null;
  stopping: boolean;
}

/** `sending`: not yet acknowledged by the server; `unsent`: it never accepted it. */
export type Delivery = "sent" | "sending" | "unsent";

/** One row of the message list. */
export interface ChatItem {
  key: string;
  message: Message;
  /** The reply being generated right now. */
  streaming: boolean;
  delivery: Delivery;
  error: ChatError | null;
  /** Set on a grounded reply while it streams and has no text yet. */
  retrieval?: Retrieval;
}

/** How many documents a grounded send names, or null when its scope is wider. */
function documentCount(grounding: GroundingRequest | Grounding | null | undefined): number | null {
  if (!grounding) return null;
  if ("scope" in grounding) return grounding.scope === "documents" ? grounding.document_ids.length : null;
  return "document_ids" in grounding ? grounding.document_ids.length : null;
}

const UNFINISHED: ReadonlySet<MessageStatus> = new Set<MessageStatus>(["pending", "streaming"]);

export function isUnfinished(message: Message): boolean {
  return UNFINISHED.has(message.status);
}

export const unsentKey = (conversationId: string): string => `unsent:${conversationId}`;

const bySequence = (a: Message, b: Message): number => a.sequence - b.sequence;

/** `messages` with `incoming` added or replaced by id, in sequence order. */
export function upsertMessages(messages: readonly Message[], incoming: readonly Message[]): Message[] {
  const byId = new Map(messages.map((message) => [message.id, message]));
  for (const message of incoming) byId.set(message.id, message);
  return [...byId.values()].sort(bySequence);
}

/**
 * The thread once `user` is answered by `reply`: the messages before the user message, then
 * the two. Anything after the user message is a reply being replaced by a regeneration.
 */
export function withExchange(messages: readonly Message[], user: Message, reply: Message): Message[] {
  const before = messages.filter(
    (message) => message.sequence < user.sequence && message.id !== user.id && message.id !== reply.id,
  );
  return [...before, user, reply];
}

/** The thread with a finished reply committed in place of the one it answered or replaced. */
export function commitReply(thread: Thread, user: Message, reply: Message): Thread {
  return {
    ...thread,
    messages: withExchange(thread.messages, user, reply),
    conversation: thread.conversation && { ...thread.conversation, generating: false },
  };
}

/** A stand-in for a message the server has not described yet. */
function localMessage(
  conversationId: string,
  fields: Pick<Message, "id" | "role" | "content" | "status" | "sequence" | "created_at"> &
    Partial<Message>,
): Message {
  return {
    conversation_id: conversationId,
    client_message_id: "",
    model: "",
    provider: "",
    provider_model: "",
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: null,
    latency_ms: 0,
    ttft_ms: null,
    finish_reason: "",
    error_code: null,
    request_id: "",
    completed_at: null,
    ...fields,
  };
}

function submissionItem(
  conversationId: string,
  submission: Submission,
  sequence: number,
  delivery: Delivery,
  error: ChatError | null,
): ChatItem {
  const key = delivery === "unsent" ? unsentKey(conversationId) : `sending:${submission.clientMessageId}`;
  return {
    key,
    message: localMessage(conversationId, {
      id: key,
      role: "user",
      content: submission.content,
      status: "completed",
      sequence,
      created_at: submission.submittedAt,
      client_message_id: submission.clientMessageId,
    }),
    streaming: false,
    delivery,
    error,
  };
}

/** The failure to show under a reply: from its `error` event if seen, else its error code. */
function failureOf(message: Message, failures: Readonly<Record<string, ChatError>>): ChatError | null {
  if (message.role !== "assistant") return null;
  const seen = failures[message.id];
  if (seen) return seen;
  if (message.status !== "failed") return null;
  return {
    code: message.error_code,
    message: describeErrorCode(message.error_code),
    retryHint: hintForCode(message.error_code),
  };
}

export interface ItemSources {
  thread: Thread;
  /** The reply being generated in this conversation, if any. */
  live?: LiveReply | undefined;
  /** The last submission of this conversation that never got through, if any. */
  unsent?: UnsentSubmission | undefined;
  /** Failures reported by `error` events, by reply id. */
  failures: Readonly<Record<string, ChatError>>;
  /**
   * Replies that finished in this session, by id. A page loaded while one was still running
   * may describe it as unfinished; the finished version wins.
   */
  finished: Readonly<Record<string, Message>>;
}

/** The rows of the message list: the thread, the reply streaming over it, unsent drafts. */
export function buildItems({ thread, live, unsent, failures, finished }: ItemSources): ChatItem[] {
  let messages: readonly Message[] = thread.messages.map((message) =>
    isUnfinished(message) ? (finished[message.id] ?? message) : message,
  );
  let streamingId: string | null = null;
  let retrieval: Retrieval | undefined;
  const tail: ChatItem[] = [];

  if (live?.userMessage && live.reply) {
    const reply: Message = {
      ...live.reply,
      content: live.text,
      status: live.text ? "streaming" : "pending",
      ...(live.found ? { sources: live.found.sources } : {}),
    };
    messages = withExchange(messages, live.userMessage, reply);
    streamingId = reply.id;
    if (reply.grounding && !live.text) {
      retrieval = live.found
        ? { state: "found", searched: live.found.searched, found: live.found.sources.length }
        : { state: "searching", documents: documentCount(reply.grounding) };
    }
  } else if (live) {
    // Until message_start: the submission as typed and a placeholder for the reply.
    if (live.kind === "regenerate" && messages.at(-1)?.role === "assistant") {
      messages = messages.slice(0, -1);
    }
    const last = messages.at(-1)?.sequence ?? 0;
    if (live.submission) {
      tail.push(submissionItem(thread.conversationId, live.submission, last + 1, "sending", null));
    }
    const key = `pending-reply:${thread.conversationId}`;
    const grounding = live.submission?.grounding;
    const searching: Retrieval | undefined = grounding
      ? { state: "searching", documents: documentCount(grounding) }
      : undefined;
    tail.push({
      key,
      message: localMessage(thread.conversationId, {
        id: key,
        role: "assistant",
        content: "",
        status: "pending",
        sequence: last + 2,
        created_at: live.submission?.submittedAt ?? "",
      }),
      streaming: true,
      delivery: "sent",
      error: null,
      ...(searching ? { retrieval: searching } : {}),
    });
  }

  const items: ChatItem[] = messages.map((message) => ({
    key: message.id,
    message,
    streaming: message.id === streamingId,
    delivery: "sent",
    error: failureOf(message, failures),
    ...(message.id === streamingId && retrieval ? { retrieval } : {}),
  }));
  // An unsent submission the server turns out to have stored is shown by its stored copy.
  const stored = (submission: Submission) =>
    messages.some((message) => message.client_message_id === submission.clientMessageId);
  if (unsent && !live && !stored(unsent)) {
    const last = messages.at(-1)?.sequence ?? 0;
    items.push(submissionItem(thread.conversationId, unsent, last + 1, "unsent", unsent.error));
  }
  return [...items, ...tail];
}

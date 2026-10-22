/**
 * The open conversation: its messages with "load older" paging, and sending, regenerating,
 * stopping and retrying replies. A send may answer from documents (`grounding`); its reply's
 * `sources` event is shown as it streams, and a refusal about the documents can be handed to
 * the page (`onGroundedSendRefused`). Without grounding everything works exactly as before.
 *
 * Replies are tracked per conversation. One keeps streaming when the user opens another
 * conversation, but only its own conversation shows it: its events are applied to the
 * thread only while that conversation is open, and after `message_start` an event naming any
 * other message than the reply is ignored. Leaving the chat page stops every running reply
 * (aborting the request is how the server learns to stop).
 *
 * Every new submission gets a fresh `client_message_id`; retrying that same submission after
 * it failed reuses it, so a send that did reach the server is replayed, not duplicated.
 *
 * A reply may use tools: its `tool_call` and `tool_result` events build its activity as it
 * streams. A reply that needs the user's approval for an action ends its stream paused
 * (`approval_required`); `decide` sends the user's choice, and the same reply continues as a
 * new stream, shown in place. A refused decision (the action changed, expired or was already
 * decided) reloads the messages, since the server's copy now says what happened.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import {
  cancelGeneration,
  decideApproval,
  getConversation,
  listMessages,
  regenerate,
  sendMessage,
  updateConversation,
} from "../api/conversations";
import { describeError } from "../components/forms";
import { retryHintFor } from "../services/ai/errors";
import type { Usage } from "../types/ai";
import type {
  ChatErrorEvent,
  ChatStreamEvent,
  Conversation,
  GroundingRequest,
  Message,
  MessageStartEvent,
} from "../types/chat";
import type { Approval, ApprovalChoice, ToolActivity } from "../types/tools";
import {
  buildItems,
  commitReply,
  describeChatError,
  unsentKey,
  upsertMessages,
  type ChatError,
  type ChatItem,
  type Decision,
  type LiveReply,
  type Submission,
  type Thread,
  type UnsentSubmission,
} from "./chatThread";
import { applyToolCall, applyToolResult, pendingApproval, withApproval } from "./toolActivity";

const PAGE_SIZE = 50;
/** How long a reply may keep streaming after the server accepted a stop. */
const STOP_GRACE_MS = 5000;
/** Refusals of a decision after which the server's copy of the paused reply is reloaded. */
const STALE_DECISION_CODES: ReadonlySet<string> = new Set([
  "TOOL_APPROVAL_STALE",
  "TOOL_APPROVAL_EXPIRED",
  "TOOL_APPROVAL_CLOSED",
]);

/** A request in flight, by conversation. */
interface Run {
  controller: AbortController;
  stopping: boolean;
  graceTimer: number | null;
  /** `message_start` arrived: the server has stored the messages and claimed the reply. */
  started: boolean;
  /** Stop was pressed before that: ask the server again as soon as the reply starts. */
  cancelOnStart: boolean;
}

export interface ChatOptions {
  /** Told whenever the hook learns something new about a conversation: title, model, activity. */
  onConversationChange?: (conversation: Conversation) => void;
  /**
   * A send that answers from documents was refused before anything was stored. Return true
   * when the page shows the refusal itself (in the composer, with the text put back); the
   * submission is then dropped instead of becoming an unsent message with Retry.
   */
  onGroundedSendRefused?: (conversationId: string, submission: Submission, error: ApiError) => boolean;
}

export interface ChatState {
  conversation: Conversation | null;
  items: ChatItem[];
  /** The conversation and its newest messages are being fetched. */
  loading: boolean;
  loadError: string | null;
  reload: () => void;
  hasOlder: boolean;
  loadingOlder: boolean;
  loadOlder: () => void;
  /** A reply is being generated in the open conversation. */
  streaming: boolean;
  stopping: boolean;
  /**
   * Send to the open conversation, or to `conversationId`; false when it cannot start. With
   * `grounding` the reply answers from documents; without it, it is an ordinary message.
   */
  send: (content: string, conversationId?: string, grounding?: GroundingRequest) => boolean;
  regenerate: () => void;
  /** Retry the row with this key: an unsent submission is resent, a failed reply regenerated. */
  retry: (key: string) => void;
  /** Approve or reject the action the paused reply `replyId` proposed; that reply continues. */
  decide: (replyId: string, approval: Approval, choice: ApprovalChoice) => void;
  /** The action the open conversation's last reply is paused on, until the user decides. */
  pendingApproval: Approval | null;
  stop: () => void;
  savingModel: boolean;
  setModel: (model: string) => void;
  /** Take a newer description of a conversation (renamed, archived elsewhere on the page). */
  syncConversation: (conversation: Conversation) => void;
  /** A failure not tied to one message (a refused regeneration, a model change, a page). */
  notice: ChatError | null;
  dismissNotice: () => void;
  /** For the polite live region: "Generating reply", "Reply stopped", or the failure. */
  announcement: string;
}

/** A fresh idempotency key. crypto.randomUUID needs a secure context; plain-http LAN access has only getRandomValues. */
function newClientMessageId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function toChatError(err: unknown): ChatError {
  if (err instanceof ApiError) {
    return { code: err.code, message: describeError(err), retryHint: retryHintFor(err.code, err.retryAfter) };
  }
  return { code: "NETWORK_ERROR", message: describeError(err), retryHint: null };
}

function eventError(event: ChatErrorEvent): ChatError {
  let retryHint: string | null = null;
  if (typeof event.retry_after === "number") retryHint = `Try again in ${String(event.retry_after)} s.`;
  else if (event.retryable) retryHint = "This failure is temporary; try again.";
  return {
    code: event.code,
    message: event.request_id ? `${event.message} (ref ${event.request_id})` : event.message,
    retryHint,
  };
}

/** The reply an event after `message_start` is about. */
function replyIdOf(event: Exclude<ChatStreamEvent, MessageStartEvent>): string {
  return event.type === "message_complete" ||
    event.type === "message_cancelled" ||
    event.type === "approval_required"
    ? event.message.id
    : event.message_id;
}

/** What the live region says when a decision is sent. */
function decisionAnnouncement(choice: ApprovalChoice): string {
  return choice === "approve" ? "Action approved; continuing the reply" : "Action rejected; continuing the reply";
}

function pickUsage(event: Usage): Usage {
  return {
    input_tokens: event.input_tokens,
    output_tokens: event.output_tokens,
    total_tokens: event.total_tokens,
    cached_input_tokens: event.cached_input_tokens,
    cache_write_input_tokens: event.cache_write_input_tokens,
    cost_usd: event.cost_usd,
  };
}

function omit<T>(record: Readonly<Record<string, T>>, key: string): Readonly<Record<string, T>> {
  if (!(key in record)) return record;
  return Object.fromEntries(Object.entries(record).filter(([name]) => name !== key));
}

export function useChat(
  conversationId: string | undefined,
  { onConversationChange, onGroundedSendRefused }: ChatOptions = {},
): ChatState {
  const [thread, setThread] = useState<Thread | null>(null);
  const [reloads, setReloads] = useState(0);
  const [olderFor, setOlderFor] = useState<string | null>(null);
  const [live, setLive] = useState<Readonly<Record<string, LiveReply>>>({});
  const [unsent, setUnsent] = useState<Readonly<Record<string, UnsentSubmission>>>({});
  const [failures, setFailures] = useState<Readonly<Record<string, ChatError>>>({});
  const [finished, setFinished] = useState<Readonly<Record<string, Message>>>({});
  const [notice, setNotice] = useState<{ conversationId: string; error: ChatError } | null>(null);
  const [savingModel, setSavingModel] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const runs = useRef(new Map<string, Run>());
  const openId = useRef(conversationId);

  useEffect(() => {
    openId.current = conversationId;
  }, [conversationId]);

  // Load the conversation and its newest messages whenever another one is opened.
  useEffect(() => {
    if (!conversationId) return;
    const controller = new AbortController();
    const { signal } = controller;
    Promise.all([
      getConversation(conversationId, { signal }),
      listMessages(conversationId, { limit: PAGE_SIZE }, { signal }),
    ])
      .then(([conversation, page]) => {
        setThread({
          conversationId,
          conversation,
          messages: page.data,
          nextBefore: page.meta.next_before,
          hasOlder: page.meta.has_more,
          error: null,
        });
      })
      .catch((err: unknown) => {
        if (signal.aborted) return;
        setThread({
          conversationId,
          conversation: null,
          messages: [],
          nextBefore: null,
          hasOlder: false,
          error: describeError(err),
        });
      });
    return () => {
      controller.abort();
    };
  }, [conversationId, reloads]);

  // Leaving the page stops every reply still streaming.
  useEffect(() => {
    const active = runs.current;
    return () => {
      for (const run of active.values()) run.controller.abort();
    };
  }, []);

  const syncConversation = useCallback((conversation: Conversation): void => {
    setThread((current) =>
      current?.conversationId === conversation.id ? { ...current, conversation } : current,
    );
  }, []);

  /** Announce only what happens in the conversation on screen. */
  const announce = (id: string, text: string): void => {
    if (openId.current === id) setAnnouncement(text);
  };

  /** The server names the conversation from its first message: fetch the new title. */
  const refresh = (id: string): void => {
    getConversation(id)
      .then((conversation) => {
        syncConversation(conversation);
        onConversationChange?.(conversation);
      })
      .catch(() => undefined); // Cosmetic: the next load brings the title anyway.
  };

  const generate = async (
    id: string,
    submission: Submission | null,
    decision: Decision | null = null,
  ): Promise<void> => {
    const controller = new AbortController();
    const run: Run = { controller, stopping: false, graceTimer: null, started: false, cancelOnStart: false };
    runs.current.set(id, run);
    let kind: LiveReply["kind"] = "regenerate";
    if (submission) kind = "send";
    else if (decision) kind = "decide";
    setLive((all) => ({
      ...all,
      [id]: {
        conversationId: id,
        kind,
        submission,
        decision,
        userMessage: null,
        reply: null,
        text: "",
        tools: [],
        usage: null,
        found: null,
        stopping: false,
      },
    }));
    // A decision continues an older reply: a submission refused meanwhile stays, to retry.
    if (decision) setFailures((all) => omit(all, decision.replyId));
    else setUnsent((all) => omit(all, id));
    setNotice((current) => (current?.conversationId === id ? null : current));
    // Always the user's own action in the conversation on screen (or the one being opened).
    setAnnouncement(decision ? decisionAnnouncement(decision.choice) : "Generating reply");

    const patchLive = (patch: (reply: LiveReply) => LiveReply): void => {
      setLive((all) => {
        const current = all[id];
        return current ? { ...all, [id]: patch(current) } : all;
      });
    };

    let start: MessageStartEvent | null = null;
    let text = "";
    let tools: ToolActivity[] = [];
    /** Commit the final reply to the thread (if it is still open) and end the live view. */
    const finish = (reply: Message, failure: ChatError | null): void => {
      const user = start?.user_message;
      setFinished((all) => ({ ...all, [reply.id]: reply }));
      if (failure) setFailures((all) => ({ ...all, [reply.id]: failure }));
      if (user) {
        setThread((current) =>
          current?.conversationId === id ? commitReply(current, user, reply) : current,
        );
      }
      setLive((all) => omit(all, id));
    };

    try {
      const { signal } = controller;
      let events: AsyncGenerator<ChatStreamEvent, void, undefined>;
      if (submission) {
        events = sendMessage(
          id,
          {
            content: submission.content,
            client_message_id: submission.clientMessageId,
            // Ordinary chat sends exactly what it always did: no grounding key at all.
            ...(submission.grounding ? { grounding: submission.grounding } : {}),
          },
          { signal },
        );
      } else if (decision) {
        events = decideApproval(
          id,
          decision.approval.id,
          { decision: decision.choice, arguments_hash: decision.approval.arguments_hash },
          { signal },
        );
      } else {
        events = regenerate(id, {}, { signal });
      }
      for await (const event of events) {
        if (event.type === "message_start") {
          start = event;
          run.started = true;
          if (run.cancelOnStart) {
            run.cancelOnStart = false;
            cancelGeneration(id).catch(() => {
              run.controller.abort();
            });
          }
          // A continued reply starts with its own text and tool calls; a new one with none.
          const startText = event.assistant_message.content;
          const startTools = event.assistant_message.tools ?? [];
          text = startText;
          tools = startTools;
          patchLive((current) => ({
            ...current,
            userMessage: event.user_message,
            reply: event.assistant_message,
            text: startText,
            tools: startTools,
          }));
          refresh(id);
          continue;
        }
        if (start === null || replyIdOf(event) !== start.assistant_message.id) continue;
        switch (event.type) {
          case "sources": {
            const found = { searched: event.searched_documents, sources: event.sources };
            patchLive((current) => ({ ...current, found }));
            break;
          }
          case "text_delta":
            text += event.text;
            patchLive((current) => ({ ...current, text: current.text + event.text }));
            break;
          case "usage": {
            const usage = pickUsage(event);
            patchLive((current) => ({ ...current, usage }));
            break;
          }
          case "tool_call":
            tools = applyToolCall(tools, event);
            patchLive((current) => ({ ...current, tools: applyToolCall(current.tools, event) }));
            break;
          case "tool_result":
            tools = applyToolResult(tools, event);
            patchLive((current) => ({ ...current, tools: applyToolResult(current.tools, event) }));
            break;
          case "message_complete":
            finish(event.message, null);
            // Screen-reader users hear that the answer is there and they can write again.
            announce(id, "Reply ready");
            return;
          case "message_cancelled":
            finish(event.message, null);
            announce(id, "Reply stopped");
            return;
          case "error": {
            const failure = eventError(event);
            finish({ ...event.assistant_message, status: "failed" }, failure);
            announce(id, describeChatError(failure));
            return;
          }
          case "approval_required":
            // Paused, not over: the reply waits for the user's decision on the action.
            finish(withApproval(event.message, event.approval), null);
            announce(id, `Waiting for your approval: ${event.approval.summary || event.approval.tool_name}`);
            return;
        }
      }
      // The stream ended without a terminal event for this reply.
      throw new ApiError(
        0,
        { code: "NETWORK_ERROR", message: "The connection closed before the reply finished." },
        null,
      );
    } catch (err) {
      if (controller.signal.aborted) {
        if (!run.stopping) return; // The page went away.
        if (start) finish({ ...start.assistant_message, content: text, tools, status: "cancelled" }, null);
        else {
          // Stopped before the server said what it stored. Keep the text to retry, and reload:
          // if the server did store the message, the list shows its copy instead.
          setLive((all) => omit(all, id));
          if (submission) {
            const error: ChatError = { code: null, message: "Stopped before it was sent.", retryHint: null };
            setUnsent((all) => ({ ...all, [id]: { ...submission, error } }));
          }
          setReloads((count) => count + 1);
        }
        announce(id, "Reply stopped");
        return;
      }
      const failure = toChatError(err);
      if (start) {
        // The stream broke after the messages were stored; the server keeps the text as
        // cancelled. Show the reply as failed with Retry (a regeneration).
        finish(
          { ...start.assistant_message, content: text, tools, status: "failed", error_code: failure.code },
          failure,
        );
      } else {
        setLive((all) => omit(all, id));
        const handled =
          submission?.grounding !== undefined &&
          err instanceof ApiError &&
          onGroundedSendRefused?.(id, submission, err) === true;
        if (handled) {
          setAnnouncement(failure.message);
          return;
        }
        if (submission) setUnsent((all) => ({ ...all, [id]: { ...submission, error: failure } }));
        else setNotice({ conversationId: id, error: failure });
        // A refused decision: the action changed, expired, was decided already, or is gone.
        // The server's copy of the paused reply now says which.
        if (decision && err instanceof ApiError && (STALE_DECISION_CODES.has(err.code) || err.status === 404)) {
          setReloads((count) => count + 1);
        }
      }
      announce(id, describeChatError(failure));
    } finally {
      if (run.graceTimer !== null) window.clearTimeout(run.graceTimer);
      if (runs.current.get(id) === run) runs.current.delete(id);
    }
  };

  const send = (
    content: string,
    target: string | undefined = conversationId,
    grounding?: GroundingRequest,
  ): boolean => {
    if (!target || runs.current.has(target) || !content.trim()) return false;
    const submission: Submission = {
      content,
      clientMessageId: newClientMessageId(),
      submittedAt: new Date().toISOString(),
      ...(grounding ? { grounding } : {}),
    };
    // Sending is activity: the conversation moves to the top of the list right away.
    if (thread?.conversationId === target && thread.conversation) {
      onConversationChange?.({ ...thread.conversation, last_message_at: submission.submittedAt });
    }
    void generate(target, submission);
    return true;
  };

  const regenerateReply = (): void => {
    if (conversationId && !runs.current.has(conversationId)) void generate(conversationId, null);
  };

  const retry = (key: string): void => {
    if (!conversationId || runs.current.has(conversationId)) return;
    const failed = unsent[conversationId];
    if (failed && key === unsentKey(conversationId)) {
      // The same client_message_id: if the first attempt got through, the server replays it.
      const { content, clientMessageId, submittedAt, grounding } = failed;
      void generate(conversationId, {
        content,
        clientMessageId,
        submittedAt,
        ...(grounding ? { grounding } : {}),
      });
    } else {
      void generate(conversationId, null);
    }
  };

  const decide = (replyId: string, approval: Approval, choice: ApprovalChoice): void => {
    if (!conversationId || runs.current.has(conversationId)) return;
    void generate(conversationId, null, { approval, choice, replyId });
  };

  const stop = (): void => {
    const id = conversationId;
    const run = id ? runs.current.get(id) : undefined;
    if (!id || !run || run.stopping) return;
    run.stopping = true;
    setLive((all) => {
      const current = all[id];
      return current ? { ...all, [id]: { ...current, stopping: true } } : all;
    });
    cancelGeneration(id)
      .then((result) => {
        if (runs.current.get(id) !== run) return; // Already over.
        if (!result.cancelled) {
          // Nothing was running on the server. Either the messages are still being stored (the
          // send goes through even if the request is dropped, so stop the reply once it starts),
          // or the reply has just finished and its final event is on its way.
          if (run.started) cancelGeneration(id).catch(() => undefined);
          else run.cancelOnStart = true;
        }
        // The stream ends within moments; stop reading if it does not.
        run.graceTimer = window.setTimeout(() => {
          run.controller.abort();
        }, STOP_GRACE_MS);
      })
      .catch(() => {
        // The server could not be told; closing the stream stops the reply too.
        run.controller.abort();
      });
  };

  const current = thread !== null && thread.conversationId === conversationId ? thread : null;

  const loadOlder = (): void => {
    if (!current?.hasOlder || current.nextBefore === null || olderFor === current.conversationId) {
      return;
    }
    const id = current.conversationId;
    setOlderFor(id);
    listMessages(id, { limit: PAGE_SIZE, before: current.nextBefore })
      .then((page) => {
        setThread((latest) =>
          latest?.conversationId === id
            ? {
                ...latest,
                messages: upsertMessages(latest.messages, page.data),
                nextBefore: page.meta.next_before,
                hasOlder: page.meta.has_more,
              }
            : latest,
        );
      })
      .catch((err: unknown) => {
        setNotice({ conversationId: id, error: toChatError(err) });
      })
      .finally(() => {
        setOlderFor((loading) => (loading === id ? null : loading));
      });
  };

  const setModel = (model: string): void => {
    const id = conversationId;
    if (!id || savingModel) return;
    setSavingModel(true);
    updateConversation(id, { model })
      .then((conversation) => {
        syncConversation(conversation);
        onConversationChange?.(conversation);
      })
      .catch((err: unknown) => {
        setNotice({ conversationId: id, error: toChatError(err) });
      })
      .finally(() => {
        setSavingModel(false);
      });
  };

  const liveReply = conversationId ? live[conversationId] : undefined;
  const items = current
    ? buildItems({
        thread: current,
        live: liveReply,
        unsent: conversationId ? unsent[conversationId] : undefined,
        failures,
        finished,
      })
    : [];

  // Only the last reply can be paused; while a decision on it is being sent it is not.
  const lastReply = items.findLast((item) => item.message.role === "assistant" && item.delivery === "sent");
  const paused = lastReply && !lastReply.streaming ? pendingApproval(lastReply.message) : null;

  return {
    conversation: current?.conversation ?? null,
    items,
    loading: Boolean(conversationId) && current === null,
    loadError: current?.error ?? null,
    reload: () => {
      setReloads((count) => count + 1);
    },
    hasOlder: current?.hasOlder ?? false,
    loadingOlder: olderFor !== null && olderFor === conversationId,
    loadOlder,
    streaming: liveReply !== undefined,
    stopping: liveReply?.stopping ?? false,
    send,
    regenerate: regenerateReply,
    retry,
    decide,
    pendingApproval: paused,
    stop,
    savingModel,
    setModel,
    syncConversation,
    notice: notice !== null && notice.conversationId === conversationId ? notice.error : null,
    dismissNotice: () => {
      setNotice(null);
    },
    announcement,
  };
}

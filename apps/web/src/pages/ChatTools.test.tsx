/**
 * The chat with AI tools: a reply's tool calls as they stream and as stored, a reply paused
 * for approval, deciding on it (the same reply continues in place), refused decisions, and
 * the composer while an action waits.
 */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getRetrievalOptions, listModels } from "../api/ai";
import { ApiError } from "../api/client";
import {
  cancelGeneration,
  decideApproval,
  getConversation,
  listConversations,
  listMessages,
  sendMessage,
  type RequestControl,
} from "../api/conversations";
import { AuthContext } from "../auth/AuthContext";
import { approval, awaitingActivity, chatMessage, toolActivity } from "../test/chatFixtures";
import { signedIn } from "../test/documentFixtures";
import type { ChatStreamEvent, Conversation, Message } from "../types/chat";
import type { Approval, ToolActivity } from "../types/tools";
import { ChatPage, PENDING_APPROVAL_NOTE } from "./ChatPage";

vi.mock("../api/conversations", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/conversations")>();
  return {
    ...actual,
    listConversations: vi.fn(),
    createConversation: vi.fn(),
    getConversation: vi.fn(),
    updateConversation: vi.fn(),
    deleteConversation: vi.fn(),
    listMessages: vi.fn(),
    sendMessage: vi.fn(),
    regenerate: vi.fn(),
    cancelGeneration: vi.fn(),
    decideApproval: vi.fn(),
  };
});

vi.mock("../api/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/ai")>();
  return { ...actual, listModels: vi.fn(), getRetrievalOptions: vi.fn() };
});

const conversation: Conversation = {
  id: "c1",
  title: "Units",
  model: "",
  status: "active",
  message_count: 2,
  generating: false,
  created_at: "2026-10-20T08:00:00+00:00",
  updated_at: "2026-10-20T08:00:00+00:00",
  last_message_at: "2026-10-20T08:00:00+00:00",
};

const question = chatMessage({ id: "m3", sequence: 3, role: "user", content: "Remember that I prefer metric", model: "" });
const pendingReply = chatMessage({ status: "pending", content: "", completed_at: null, finish_reason: "" });
const pending = approval();
/** The stored reply, paused on saving a memory. */
const paused = chatMessage({
  status: "awaiting_approval",
  content: "I can save that for you.",
  completed_at: null,
  finish_reason: "",
  tools: [awaitingActivity(pending)],
});

const TERMINAL = new Set<string>(["message_complete", "message_cancelled", "error", "approval_required"]);

/** A reply stream the test feeds event by event; it ends after a terminal event or on abort. */
function controlledStream() {
  const queue: ChatStreamEvent[] = [];
  let wake: () => void = () => undefined;
  let signal: AbortSignal | undefined;
  return {
    push(...events: ChatStreamEvent[]) {
      queue.push(...events);
      wake();
    },
    get signal() {
      return signal;
    },
    async *events(control?: RequestControl): AsyncGenerator<ChatStreamEvent, void, undefined> {
      signal = control?.signal;
      for (;;) {
        const next = queue.shift();
        if (next) {
          yield next;
          if (TERMINAL.has(next.type)) return;
          continue;
        }
        await new Promise<void>((resolve, reject) => {
          wake = resolve;
          signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      }
    },
  };
}

type ControlledStream = ReturnType<typeof controlledStream>;

async function* refused(error: Error): AsyncGenerator<ChatStreamEvent, void, undefined> {
  await Promise.resolve();
  throw error;
  yield* []; // never reached; keeps the function a generator
}

function started(reply: Message, user: Message = question): ChatStreamEvent {
  return {
    type: "message_start",
    protocol: "nexa.chat.v1",
    conversation_id: "c1",
    request_id: "req_1",
    user_message: user,
    assistant_message: reply,
  };
}

function toolCall(activity: ToolActivity): ChatStreamEvent {
  return {
    type: "tool_call",
    message_id: "m4",
    invocation_id: activity.invocation_id,
    call_id: activity.call_id,
    tool_id: activity.tool_id,
    tool_name: activity.tool_name,
    step: activity.step,
    status: activity.status,
    summary: activity.summary,
    risk: activity.risk,
    error_code: activity.error_code,
    error_message: activity.error_message,
  };
}

function toolResult(activity: ToolActivity): ChatStreamEvent {
  return {
    type: "tool_result",
    message_id: "m4",
    invocation_id: activity.invocation_id,
    call_id: activity.call_id,
    tool_id: activity.tool_id,
    status: activity.status,
    duration_ms: activity.duration_ms,
    result: activity.result,
    result_truncated: activity.result_truncated,
    error_code: activity.error_code,
    error_message: activity.error_message,
    sources: activity.sources,
  };
}

const delta = (text: string): ChatStreamEvent => ({ type: "text_delta", message_id: "m4", text });

function completed(content: string, tools: ToolActivity[]): ChatStreamEvent {
  return {
    type: "message_complete",
    message: chatMessage({ content, tools, input_tokens: 40, output_tokens: 12, latency_ms: 900 }),
  };
}

function refusal(code: string, message: string, status = 409): ApiError {
  return new ApiError(status, { code, message }, "req_9");
}

function renderChat() {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:conversationId?" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const composer = () => screen.getByLabelText<HTMLTextAreaElement>("Message");
const messages = () => screen.getByRole("list", { name: "Messages" });
const liveRegion = () => document.querySelector('[aria-live="polite"]')?.textContent;
const card = () => screen.getByRole("region", { name: "Approval needed" });
const replies = () => [...messages().querySelectorAll(".message-assistant")];
/** The rows of the (only) tool timeline: summary, state and duration. */
const toolRows = () =>
  [...document.querySelectorAll(".tool-row-head")].map((row) => row.textContent.replace(/\s+/g, " ").trim());

async function openChat(): Promise<void> {
  renderChat();
  await waitFor(() => {
    expect(composer().getAttribute("aria-disabled")).toBe("false");
  });
}

function sendText(text: string): void {
  fireEvent.change(composer(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

async function feed(stream: ControlledStream, ...events: ChatStreamEvent[]): Promise<void> {
  await act(async () => {
    stream.push(...events);
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

/** The conversation opens on the paused reply, as stored. */
function storedPaused(reply: Message = paused): void {
  vi.mocked(listMessages).mockResolvedValue({ data: [question, reply], meta: { next_before: null, has_more: false } });
}

describe("ChatPage with AI tools", () => {
  beforeEach(() => {
    vi.mocked(listModels).mockResolvedValue([]);
    vi.mocked(getRetrievalOptions).mockRejectedValue(new Error("not used"));
    vi.mocked(listConversations).mockResolvedValue({ data: [conversation], meta: { next_cursor: null, has_more: false } });
    vi.mocked(getConversation).mockResolvedValue(conversation);
    vi.mocked(listMessages).mockResolvedValue({ data: [], meta: { next_before: null, has_more: false } });
    vi.mocked(cancelGeneration).mockResolvedValue({ cancelled: true, message_id: "m4" });
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("shows each tool call as it runs, ends, or is refused, then the answer", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    await openChat();
    sendText("What is 6*7?");
    // Ordinary chat sends what it always did: tools are on by default.
    expect(Object.keys(vi.mocked(sendMessage).mock.calls[0]?.[1] ?? {})).toEqual(["content", "client_message_id"]);

    const calculation = toolActivity();
    const running: ToolActivity = { ...calculation, status: "executing", duration_ms: null, result: null };
    const denied = toolActivity({
      invocation_id: "inv-9",
      call_id: "call_9",
      tool_id: "workspace_info",
      tool_name: "Workspace info",
      summary: "Looking up your workspace",
      status: "rejected",
      duration_ms: null,
      result: null,
      error_code: "permission_denied",
      error_message: "Not allowed.",
    });
    await feed(stream, started(pendingReply), toolCall(running));
    expect(toolRows()).toEqual(["Calculating 6*7Running"]);
    expect(document.querySelector(".tool-spinner")).not.toBeNull();

    await feed(stream, toolResult(calculation), toolCall(denied));
    expect(toolRows()).toEqual(["Calculating 6*7Done3 ms", "Looking up your workspaceRejected"]);
    expect(within(messages()).getByText("You do not have permission to use this tool.")).toBeTruthy();
    expect(document.querySelector(".tool-spinner")).toBeNull();

    await feed(stream, delta("6*7 is 42."));
    expect(within(messages()).getByText("6*7 is 42.")).toBeTruthy();
    await feed(stream, completed("6*7 is 42.", [calculation, denied]));
    expect(toolRows()).toEqual(["Calculating 6*7Done3 ms", "Looking up your workspaceRejected"]);
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeTruthy();
    expect(liveRegion()).toBe("Reply ready");
  });

  it("stops a reply while a tool runs", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    const running = toolActivity({ status: "executing", duration_ms: null, result: null });
    vi.mocked(cancelGeneration).mockImplementation(() => {
      stream.push({
        type: "message_cancelled",
        message: chatMessage({ status: "cancelled", tools: [{ ...running, status: "cancelled" }] }),
      });
      return Promise.resolve({ cancelled: true, message_id: "m4" });
    });
    await openChat();
    sendText("What is 6*7?");
    await feed(stream, started(pendingReply), toolCall(running));

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await settle();
    expect(cancelGeneration).toHaveBeenCalledWith("c1");
    expect(toolRows()).toEqual(["Calculating 6*7Cancelled"]);
    expect(within(messages()).getByText("Cancelled", { selector: ".badge" })).toBeTruthy();
  });

  it("pauses for approval, then approves and continues the same reply in place", async () => {
    const first = controlledStream();
    const second = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => first.events(control));
    vi.mocked(decideApproval).mockImplementation((_id, _approval, _body, control) => second.events(control));
    await openChat();
    sendText("Remember that I prefer metric");

    const waiting = awaitingActivity(pending);
    await feed(first, started(pendingReply), delta("I can save that for you."), toolCall(waiting));
    expect(toolRows()).toEqual(["Saving to your memoriesNeeds your approval"]);
    await feed(first, { type: "approval_required", message: paused, approval: pending });

    expect(within(card()).getByText("Saving to your memories")).toBeTruthy();
    expect(within(card()).getByText("High risk")).toBeTruthy();
    expect(liveRegion()).toBe("Waiting for your approval: Saving to your memories");
    // Nothing is running: the composer offers Send, and says what the conversation waits for.
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(screen.getByText(PENDING_APPROVAL_NOTE)).toBeTruthy();
    expect(decideApproval).not.toHaveBeenCalled();

    fireEvent.click(within(card()).getByRole("button", { name: "Approve" }));
    expect(decideApproval).toHaveBeenCalledWith(
      "c1",
      "ap-1",
      { decision: "approve", arguments_hash: "a".repeat(64) },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );
    expect(within(card()).getByRole<HTMLButtonElement>("button", { name: "Approving…" }).disabled).toBe(true);
    expect(within(card()).getByRole<HTMLButtonElement>("button", { name: "Reject" }).disabled).toBe(true);
    expect(screen.queryByText(PENDING_APPROVAL_NOTE)).toBeNull();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(document.activeElement).toBe(composer());
    expect(liveRegion()).toBe("Action approved; continuing the reply");

    const approved = approval({ status: "approved", decided_at: "2026-10-20T09:01:00+00:00" });
    const resumed: ToolActivity = { ...waiting, status: "approved", approval: approved };
    const saved: ToolActivity = {
      ...resumed,
      status: "succeeded",
      duration_ms: 12,
      result: { memory_id: "mem-1", content: "I prefer metric units.", kind: "preference", status: "active" },
    };
    await feed(second, started({ ...paused, status: "streaming", tools: [resumed] }));
    // The same reply, with its text so far; no second reply appears.
    expect(replies()).toHaveLength(1);
    expect(within(messages()).getByText("I can save that for you.")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();

    await feed(second, toolCall({ ...resumed, status: "executing" }));
    expect(toolRows()).toEqual(["Saving to your memoriesRunning"]);
    await feed(second, toolResult(saved), delta("\n\nSaved."));
    expect(toolRows()).toEqual(["Saving to your memoriesDone12 ms"]);
    expect(within(messages()).getByText("Saved.")).toBeTruthy();
    expect(within(messages()).getByText("I can save that for you.")).toBeTruthy();

    await feed(second, completed("I can save that for you.\n\nSaved.", [saved]));
    expect(replies()).toHaveLength(1);
    expect(within(messages()).getByText("You approved this action.")).toBeTruthy();
    expect(screen.queryByText(PENDING_APPROVAL_NOTE)).toBeNull();
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(liveRegion()).toBe("Reply ready");
  });

  it("shows a paused reply from the stored messages, and rejects it", async () => {
    storedPaused();
    const stream = controlledStream();
    vi.mocked(decideApproval).mockImplementation((_id, _approval, _body, control) => stream.events(control));
    await openChat();

    // After a reload: the card, the timeline and the composer note come from the stored reply.
    expect(within(card()).getByText("Save a memory")).toBeTruthy();
    expect(toolRows()).toEqual(["Saving to your memoriesNeeds your approval"]);
    expect(screen.getByText(PENDING_APPROVAL_NOTE)).toBeTruthy();

    fireEvent.click(within(card()).getByRole("button", { name: "Reject" }));
    expect(decideApproval).toHaveBeenCalledWith(
      "c1",
      "ap-1",
      { decision: "reject", arguments_hash: "a".repeat(64) },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );
    expect(within(card()).getByRole("button", { name: "Rejecting…" })).toBeTruthy();

    const declined: ToolActivity = {
      ...awaitingActivity(approval({ status: "rejected", decided_at: "2026-10-20T09:01:00+00:00" })),
      status: "rejected",
      error_code: "declined_by_user",
      error_message: "The user declined this action.",
    };
    await feed(
      stream,
      started({ ...paused, status: "streaming", tools: [declined] }),
      delta("\n\nOK, I will not save it."),
      completed("I can save that for you.\n\nOK, I will not save it.", [declined]),
    );
    expect(replies()).toHaveLength(1);
    expect(toolRows()).toEqual(["Saving to your memoriesDeclined"]);
    expect(within(messages()).getByText("You declined this action, so it was not carried out.")).toBeTruthy();
    expect(within(messages()).getByText("OK, I will not save it.")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();
  });

  it("reloads after a decision on an action that changed, and decides on the new one", async () => {
    const changed: Approval = approval({ id: "ap-2", arguments_hash: "c".repeat(64) });
    vi.mocked(listMessages)
      .mockResolvedValueOnce({ data: [question, paused], meta: { next_before: null, has_more: false } })
      .mockResolvedValue({
        data: [question, { ...paused, tools: [awaitingActivity(changed)] }],
        meta: { next_before: null, has_more: false },
      });
    const stream = controlledStream();
    vi.mocked(decideApproval)
      .mockImplementationOnce(() =>
        refused(
          refusal("TOOL_APPROVAL_STALE", "The action changed since it was shown to you. Review it again before deciding."),
        ),
      )
      .mockImplementation((_id, _approval, _body, control) => stream.events(control));
    await openChat();

    fireEvent.click(within(card()).getByRole("button", { name: "Approve" }));
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toBe(
        "The action changed since it was shown to you. Review it again before deciding. (ref req_9)",
      );
    });
    await waitFor(() => {
      expect(listMessages).toHaveBeenCalledTimes(2);
    });
    await settle();
    expect(liveRegion()).toBe(
      "The action changed since it was shown to you. Review it again before deciding. (ref req_9)",
    );

    // The action as it is now, decided with its own hash.
    fireEvent.click(within(card()).getByRole("button", { name: "Approve" }));
    expect(decideApproval).toHaveBeenLastCalledWith(
      "c1",
      "ap-2",
      { decision: "approve", arguments_hash: "c".repeat(64) },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );
    // The notice about the refused decision goes once a new one is sent.
    expect(screen.queryByRole("alert")).toBeNull();
    await feed(stream, started({ ...paused, status: "streaming" }), completed("Done.", []));
  });

  it("shows a reply whose approval expired as cancelled", async () => {
    const expired = refusal("TOOL_APPROVAL_EXPIRED", "This action was not approved in time and has expired.");
    vi.mocked(listMessages)
      .mockResolvedValueOnce({ data: [question, paused], meta: { next_before: null, has_more: false } })
      .mockResolvedValue({
        data: [
          question,
          {
            ...paused,
            status: "cancelled",
            error_code: "TOOL_APPROVAL_EXPIRED",
            completed_at: "2026-10-20T09:16:00+00:00",
            tools: [{ ...awaitingActivity(approval({ status: "expired" })), status: "cancelled" }],
          },
        ],
        meta: { next_before: null, has_more: false },
      });
    vi.mocked(decideApproval).mockImplementation(() => refused(expired));
    await openChat();

    fireEvent.click(within(card()).getByRole("button", { name: "Approve" }));
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toBe(
        "This action was not approved in time and has expired. (ref req_9)",
      );
    });
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();
    });
    expect(within(messages()).getByText("Cancelled", { selector: ".badge" })).toBeTruthy();
    expect(
      within(messages()).getByText("The action was not approved in time, so it expired and was not carried out."),
    ).toBeTruthy();
    expect(within(messages()).getByText("The approval request expired before you decided.")).toBeTruthy();
    expect(toolRows()).toEqual(["Saving to your memoriesCancelled"]);
    expect(screen.queryByText(PENDING_APPROVAL_NOTE)).toBeNull();
    // A stopped reply can be retried, as before.
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("keeps the card when a decision cannot reach the server", async () => {
    storedPaused();
    vi.mocked(decideApproval).mockImplementation(() => refused(new TypeError("Failed to fetch")));
    await openChat();

    fireEvent.click(within(card()).getByRole("button", { name: "Approve" }));
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toBe("Something went wrong. Check your connection and try again.");
    });
    expect(listMessages).toHaveBeenCalledTimes(1);
    expect(within(card()).getByRole<HTMLButtonElement>("button", { name: "Approve" }).disabled).toBe(false);
    expect(screen.getByText(PENDING_APPROVAL_NOTE)).toBeTruthy();
  });

  it("explains a new message refused while an action waits for approval", async () => {
    storedPaused();
    vi.mocked(sendMessage).mockImplementation(() =>
      refused(
        refusal(
          "TOOL_APPROVAL_PENDING",
          "An action in this conversation is waiting for your approval. Approve or reject it first.",
        ),
      ),
    );
    await openChat();
    expect(screen.getByText(PENDING_APPROVAL_NOTE)).toBeTruthy();

    sendText("Something else");
    await settle();
    expect(within(messages()).getByText("Not sent")).toBeTruthy();
    expect(
      within(messages()).getByText(
        "An action in this conversation is waiting for your approval. Approve or reject it first. (ref req_9)",
      ),
    ).toBeTruthy();
    // The paused reply still waits, with its card.
    expect(within(card()).getByRole("button", { name: "Approve" })).toBeTruthy();
  });
});

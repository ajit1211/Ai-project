import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listModels } from "../api/ai";
import { ApiError } from "../api/client";
import {
  cancelGeneration,
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  listMessages,
  regenerate,
  sendMessage,
  updateConversation,
  type RequestControl,
} from "../api/conversations";
import { AuthContext, type AuthContextValue } from "../auth/AuthContext";
import { EXAMPLE_PROMPTS } from "../components/chat/EmptyState";
import type { ModelInfo } from "../types/ai";
import type {
  ChatStreamEvent,
  Conversation,
  ConversationPage,
  Message,
  MessageListMeta,
  MessagePage,
} from "../types/chat";
import { ChatPage } from "./ChatPage";
import { ChatRoute } from "./ChatRoute";

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
  };
});

vi.mock("../api/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/ai")>();
  return { ...actual, listModels: vi.fn() };
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "c1",
    title: "Explain Django middleware",
    model: "",
    status: "active",
    message_count: 2,
    generating: false,
    created_at: "2026-10-09T08:00:00+00:00",
    updated_at: "2026-10-09T08:05:12+00:00",
    last_message_at: "2026-10-09T08:05:12+00:00",
    ...overrides,
  };
}

const django = conversation();
const trip = conversation({
  id: "c2",
  title: "Plan a trip",
  created_at: "2026-10-08T09:00:00+00:00",
  updated_at: "2026-10-08T10:00:00+00:00",
  last_message_at: "2026-10-08T10:00:00+00:00",
});

/** An assistant message of conversation c1, pending unless overridden. */
function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "m4",
    conversation_id: "c1",
    sequence: 4,
    role: "assistant",
    content: "",
    status: "pending",
    client_message_id: "",
    model: "nexa-balanced",
    provider: "",
    provider_model: "",
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: null,
    latency_ms: 0,
    ttft_ms: null,
    finish_reason: "",
    error_code: null,
    request_id: "req_1",
    created_at: "2026-10-10T09:00:00+00:00",
    completed_at: null,
    ...overrides,
  };
}

function userMessage(overrides: Partial<Message>): Message {
  return message({ role: "user", status: "completed", model: "", request_id: "", ...overrides });
}

const earlierQuestion = userMessage({ id: "m1", sequence: 1, content: "What is middleware?" });
const earlierAnswer = message({
  id: "m2",
  sequence: 2,
  content: "Middleware wraps every request.",
  status: "completed",
  input_tokens: 5,
  output_tokens: 7,
  latency_ms: 300,
  finish_reason: "stop",
});
const question = userMessage({ id: "m3", sequence: 3, content: "Explain SSE" });
const pendingReply = message();

function started(user: Message = question, reply: Message = pendingReply): ChatStreamEvent {
  return {
    type: "message_start",
    protocol: "nexa.chat.v1",
    conversation_id: user.conversation_id,
    request_id: "req_1",
    user_message: user,
    assistant_message: reply,
  };
}

function delta(text: string, messageId = "m4"): ChatStreamEvent {
  return { type: "text_delta", message_id: messageId, text };
}

const usage: ChatStreamEvent = {
  type: "usage",
  message_id: "m4",
  input_tokens: 12,
  output_tokens: 34,
  total_tokens: 46,
  cached_input_tokens: 0,
  cache_write_input_tokens: 0,
  cost_usd: "0.000120",
};

function completed(content: string, overrides: Partial<Message> = {}): ChatStreamEvent {
  return {
    type: "message_complete",
    message: message({
      content,
      status: "completed",
      provider: "mock",
      provider_model: "mock-1",
      input_tokens: 12,
      output_tokens: 34,
      cost_usd: "0.000120",
      latency_ms: 2210,
      ttft_ms: 480,
      finish_reason: "stop",
      completed_at: "2026-10-10T09:00:03+00:00",
      ...overrides,
    }),
  };
}

const models: ModelInfo[] = [
  {
    name: "nexa-fast",
    display_name: "NEXA Fast",
    description: "Quick answers.",
    is_default: false,
    capabilities: ["chat", "streaming"],
    context_window: 128000,
    max_output_tokens: 4096,
  },
  {
    name: "nexa-balanced",
    display_name: "NEXA Balanced",
    description: "The default.",
    is_default: true,
    capabilities: ["chat", "streaming", "structured_output"],
    context_window: 200000,
    max_output_tokens: 8192,
  },
];

function page(items: Conversation[]): ConversationPage {
  return { data: items, meta: { next_cursor: null, has_more: false } };
}

function thread(items: Message[], meta: MessageListMeta = { next_before: null, has_more: false }): MessagePage {
  return { data: items, meta };
}

const TERMINAL = new Set<string>(["message_complete", "message_cancelled", "error"]);

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
          signal?.addEventListener(
            "abort",
            () => {
              reject(new DOMException("The operation was aborted.", "AbortError"));
            },
            { once: true },
          );
        });
      }
    },
  };
}

type ControlledStream = ReturnType<typeof controlledStream>;

async function* failing(error: Error): AsyncGenerator<ChatStreamEvent, void, undefined> {
  await Promise.resolve();
  throw error;
  yield* []; // never reached; keeps the function a generator for require-yield
}

const sleep = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

/** Let pending promises, stream events and renders settle. */
async function settle(): Promise<void> {
  await act(async () => {
    await sleep(20);
  });
}

/** Deliver stream events and let the page render them. */
async function feed(stream: ControlledStream, ...events: ChatStreamEvent[]): Promise<void> {
  await act(async () => {
    stream.push(...events);
    await sleep(20);
  });
}

const noop = () => Promise.resolve();
const auth: AuthContextValue = {
  status: "authenticated",
  user: null,
  organizations: [],
  activeOrganizationId: null,
  login: noop,
  logout: noop,
  switchOrganization: noop,
  reloadUser: noop,
  reloadOrganizations: () => Promise.resolve([]),
};

function CurrentPath() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function renderChat(path = "/chat") {
  return render(
    <AuthContext.Provider value={auth}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/chat/:conversationId?" element={<ChatPage />} />
        </Routes>
        <CurrentPath />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const location = () => screen.getByTestId("location").textContent;
const composer = () => screen.getByLabelText<HTMLTextAreaElement>("Message");
const liveRegion = () => document.querySelector('[aria-live="polite"]')?.textContent;
const sidebar = () => screen.getByRole("navigation", { name: "Conversations" });

/** Render a conversation and wait until it has loaded. */
async function openChat(path: string): Promise<void> {
  renderChat(path);
  await waitFor(() => {
    expect(composer().getAttribute("aria-disabled")).toBe("false");
  });
}

function write(text: string): void {
  fireEvent.change(composer(), { target: { value: text } });
}

function sendText(text: string): void {
  write(text);
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

function lastOf<T>(items: T[]): T {
  const last = items.at(-1);
  if (last === undefined) throw new Error("empty list");
  return last;
}

function mockNarrowScreen(): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query === "(max-width: 800px)",
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
}

describe("ChatPage", () => {
  beforeEach(() => {
    vi.mocked(listModels).mockResolvedValue(models);
    vi.mocked(listConversations).mockResolvedValue(page([django, trip]));
    vi.mocked(getConversation).mockImplementation((id) => Promise.resolve(id === "c2" ? trip : django));
    vi.mocked(listMessages).mockResolvedValue(thread([]));
    vi.mocked(cancelGeneration).mockResolvedValue({ cancelled: true, message_id: "m4" });
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("starts a fresh workspace when the organization changes", async () => {
    const workspace = (organization: string) => (
      <AuthContext.Provider value={{ ...auth, activeOrganizationId: organization }}>
        <MemoryRouter initialEntries={["/chat/c1"]}>
          <Routes>
            <Route path="/chat/:conversationId?" element={<ChatRoute />} />
          </Routes>
          <CurrentPath />
        </MemoryRouter>
      </AuthContext.Provider>
    );
    const loaded = () => screen.findByRole("navigation", { name: "Conversations" });
    const { rerender } = render(workspace("org-a"));
    await within(await loaded()).findByRole("link", { name: "Explain Django middleware" });
    expect(location()).toBe("/chat/c1");

    // The other organization has its own conversations; the open one belongs to the first.
    vi.mocked(listConversations).mockResolvedValue(page([trip]));
    rerender(workspace("org-b"));
    await waitFor(() => {
      expect(location()).toBe("/chat");
    });
    await within(await loaded()).findByRole("link", { name: "Plan a trip" });
    expect(within(sidebar()).queryByRole("link", { name: "Explain Django middleware" })).toBeNull();
  });

  it("lists conversations by activity and searches them", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([trip, django]));
    renderChat();

    await within(sidebar()).findByRole("link", { name: "Explain Django middleware" });
    expect(within(sidebar()).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "Explain Django middleware",
      "Plan a trip",
    ]);
    expect(listConversations).toHaveBeenCalledWith(
      { status: "active", q: "", limit: 30 },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );

    vi.mocked(listConversations).mockResolvedValue(page([trip]));
    fireEvent.change(screen.getByLabelText("Search conversations"), { target: { value: "trip" } });
    // The current results stay until the search answers, then only the match remains.
    expect(within(sidebar()).getByRole("link", { name: "Explain Django middleware" })).toBeTruthy();
    await waitFor(() => {
      expect(within(sidebar()).queryByRole("link", { name: "Explain Django middleware" })).toBeNull();
    });
    expect(listConversations).toHaveBeenLastCalledWith(
      { status: "active", q: "trip", limit: 30 },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );
    expect(within(sidebar()).getByRole("link", { name: "Plan a trip" })).toBeTruthy();

    vi.mocked(listConversations).mockResolvedValue(page([]));
    fireEvent.change(screen.getByLabelText("Search conversations"), { target: { value: "lisbon" } });
    expect(await within(sidebar()).findByText("No conversations match “lisbon”.")).toBeTruthy();
  });

  it("creates a conversation and opens it", async () => {
    const fresh = conversation({
      id: "c3",
      title: "",
      message_count: 0,
      created_at: "2026-10-10T09:00:00+00:00",
      last_message_at: null,
    });
    vi.mocked(createConversation).mockResolvedValue(fresh);
    vi.mocked(getConversation).mockResolvedValue(fresh);
    renderChat();
    await within(sidebar()).findByRole("link", { name: "Explain Django middleware" });

    fireEvent.click(within(sidebar()).getByRole("button", { name: "New conversation" }));

    await waitFor(() => {
      expect(location()).toBe("/chat/c3");
    });
    expect(createConversation).toHaveBeenCalledOnce();
    expect(await screen.findByRole("heading", { name: "Ask anything" })).toBeTruthy();
    const newest = within(sidebar()).getAllByRole("link")[0];
    expect(newest?.textContent).toBe("New conversation");
    expect(newest?.getAttribute("aria-current")).toBe("page");
  });

  it("starts a conversation from a message written with none open", async () => {
    const fresh = conversation({
      id: "c3",
      title: "",
      message_count: 0,
      created_at: "2026-10-10T08:59:00+00:00",
      last_message_at: null,
    });
    vi.mocked(createConversation).mockResolvedValue(fresh);
    vi.mocked(getConversation).mockResolvedValue({
      ...fresh,
      title: "Explain SSE",
      message_count: 2,
      last_message_at: "2026-10-10T09:00:00+00:00",
    });
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    renderChat();
    expect(await screen.findByRole("heading", { name: "Start a conversation" })).toBeTruthy();

    sendText("Explain SSE");

    await waitFor(() => {
      expect(location()).toBe("/chat/c3");
    });
    expect(sendMessage).toHaveBeenCalledWith(
      "c3",
      { content: "Explain SSE", client_message_id: expect.stringMatching(UUID) as string },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );
    const inC3 = (m: Message) => ({ ...m, conversation_id: "c3" });
    await feed(stream, started(inC3(question), inC3(pendingReply)), completed("Hello!", { conversation_id: "c3" }));
    expect(screen.getByText("Hello!")).toBeTruthy();
    // The server named the conversation after its first message.
    expect(screen.getByRole("heading", { level: 1, name: "Explain SSE" })).toBeTruthy();
    expect(within(sidebar()).getAllByRole("link")[0]?.textContent).toBe("Explain SSE");
  });

  it("sends a message and streams the reply into the conversation", async () => {
    vi.mocked(listMessages).mockResolvedValue(thread([earlierQuestion, earlierAnswer]));
    vi.mocked(listConversations).mockResolvedValue(page([trip, django]));
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    await openChat("/chat/c1");

    sendText("Explain SSE");

    // The user's message shows at once, the composer empties and offers Stop.
    const list = screen.getByRole("list", { name: "Messages" });
    expect(within(list).getByText("Explain SSE")).toBeTruthy();
    expect(composer().value).toBe("");
    expect(composer().getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(liveRegion()).toBe("Generating reply");
    expect(sendMessage).toHaveBeenCalledWith(
      "c1",
      { content: "Explain SSE", client_message_id: expect.stringMatching(UUID) as string },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );

    // The reply grows with each text_delta.
    await feed(stream, started(), delta("Server-Sent "));
    expect(within(list).getByText("Server-Sent")).toBeTruthy();
    await feed(stream, delta("Events **push**"));
    expect(within(list).getByText("push").tagName).toBe("STRONG");
    await feed(stream, delta(" text."));
    expect(within(list).getByText(/^Server-Sent Events/).textContent).toBe("Server-Sent Events push text.");
    expect(within(list).getAllByRole("list", { name: "Reply details" })).toHaveLength(1); // the earlier reply's

    // The terminal event brings the usage and timing footer.
    await feed(stream, usage, completed("Server-Sent Events **push** text."));
    const details = lastOf(within(list).getAllByRole("list", { name: "Reply details" }));
    expect(within(details).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "12 in · 34 out tokens",
      "$0.000120",
      "latency 2.2 s",
      "first token 480 ms",
    ]);
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeTruthy();
    expect(composer().getAttribute("aria-disabled")).toBe("false");
    expect(liveRegion()).toBe("Reply ready");
    // Sending moved the conversation to the top of the list.
    expect(within(sidebar()).getAllByRole("link")[0]?.textContent).toBe("Explain Django middleware");
  });

  it("stops a reply through the API and shows it as cancelled", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    vi.mocked(cancelGeneration).mockImplementation(() => {
      // The server stops the generation; the stream ends with message_cancelled.
      stream.push({
        type: "message_cancelled",
        message: message({ content: "Partial answer", status: "cancelled", output_tokens: 3, latency_ms: 900 }),
      });
      return Promise.resolve({ cancelled: true, message_id: "m4" });
    });
    await openChat("/chat/c1");
    sendText("Explain SSE");
    await feed(stream, started(), delta("Partial answer"));

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(document.activeElement).toBe(composer()); // Stop disables itself; typing goes on
    await settle();

    expect(cancelGeneration).toHaveBeenCalledWith("c1");
    expect(stream.signal?.aborted).toBe(false); // stopped by the server, not by dropping the stream
    expect(screen.getByText("Cancelled")).toBeTruthy();
    expect(screen.getByText("Partial answer")).toBeTruthy();
    expect(liveRegion()).toBe("Reply stopped");
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
  });

  it("keeps a message stopped before the server answered, to retry", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    vi.mocked(cancelGeneration).mockResolvedValue({ cancelled: false, message_id: null });
    await openChat("/chat/c1");
    sendText("Explain SSE");

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await settle();

    // Nothing was running on the server yet, so the request itself is dropped.
    expect(stream.signal?.aborted).toBe(true);
    const list = screen.getByRole("list", { name: "Messages" });
    expect(within(list).getByText("Not sent")).toBeTruthy();
    expect(within(list).getByText("Stopped before it was sent.")).toBeTruthy();
    expect(within(list).getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(liveRegion()).toBe("Reply stopped");
    expect(listMessages).toHaveBeenCalledTimes(2); // Reloaded to see what the server kept.
  });

  it("shows the server's copy of a message stopped before the server answered", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    vi.mocked(cancelGeneration).mockResolvedValue({ cancelled: false, message_id: null });
    vi.mocked(listMessages)
      .mockResolvedValueOnce(thread([]))
      .mockImplementation(() => {
        const sent = vi.mocked(sendMessage).mock.calls[0]?.[1];
        return Promise.resolve(
          thread([
            { ...question, client_message_id: sent?.client_message_id ?? "" },
            message({ status: "cancelled" }),
          ]),
        );
      });
    await openChat("/chat/c1");
    sendText("Explain SSE");

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await settle();

    const list = screen.getByRole("list", { name: "Messages" });
    expect(within(list).queryByText("Not sent")).toBeNull();
    expect(within(list).getAllByText("Explain SSE")).toHaveLength(1);
    expect(within(list).getByText("Cancelled")).toBeTruthy();
  });

  it("shows a failed reply with its error and a Retry that regenerates it", async () => {
    const first = controlledStream();
    const second = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => first.events(control));
    vi.mocked(regenerate).mockImplementation((_id, _body, control) => second.events(control));
    await openChat("/chat/c1");
    sendText("Explain SSE");

    await feed(first, started(), delta("Hel"), {
      type: "error",
      message_id: "m4",
      code: "AI_TIMEOUT",
      message: "The AI provider took too long to answer.",
      retryable: true,
      retry_after: null,
      request_id: "req_9",
      assistant_message: message({ content: "Hel", status: "failed", error_code: "AI_TIMEOUT" }),
    });

    const explained =
      "The AI provider took too long to answer. (ref req_9) This failure is temporary; try again.";
    const list = screen.getByRole("list", { name: "Messages" });
    expect(within(list).getByText("Failed")).toBeTruthy();
    expect(within(list).getByText(explained)).toBeTruthy();
    expect(liveRegion()).toBe(explained);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(regenerate).toHaveBeenCalledWith("c1", {}, { signal: expect.any(AbortSignal) as AbortSignal });
    expect(screen.queryByText("Failed")).toBeNull();
    // The Retry button went away with the failed reply: focus is in the composer, not lost.
    expect(document.activeElement).toBe(composer());
    const again = message({ id: "m5" });
    await feed(second, started(question, again), delta("Hello again", "m5"), completed("Hello again", { id: "m5" }));
    expect(screen.getByText("Hello again")).toBeTruthy();
    expect(screen.queryByText("Hel")).toBeNull();
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeTruthy();
  });

  it("applies a reply's events only to its own message and its own conversation", async () => {
    const tripQuestion = userMessage({
      id: "t1",
      conversation_id: "c2",
      sequence: 1,
      content: "Where should I go in May?",
    });
    vi.mocked(listMessages).mockImplementation((id) => Promise.resolve(thread(id === "c2" ? [tripQuestion] : [])));
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    await openChat("/chat/c1");
    sendText("Explain SSE");

    await feed(stream, started(), delta("Mine"), delta(" NOT MINE", "another-message"), delta(" too"));
    expect(screen.getByText("Mine too")).toBeTruthy();
    expect(screen.queryByText(/NOT MINE/)).toBeNull();

    // Another conversation is opened while the reply keeps streaming.
    fireEvent.click(within(sidebar()).getByRole("link", { name: "Plan a trip" }));
    expect(await screen.findByText("Where should I go in May?")).toBeTruthy();
    await feed(stream, delta(" and more"));
    expect(screen.queryByText(/Mine/)).toBeNull();
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(stream.signal?.aborted).toBe(false);

    // Back in the first conversation the reply is there, with what arrived meanwhile.
    vi.mocked(listMessages).mockImplementation((id) =>
      Promise.resolve(
        thread(id === "c2" ? [tripQuestion] : [question, message({ content: "Mine too", status: "streaming" })]),
      ),
    );
    fireEvent.click(within(sidebar()).getByRole("link", { name: "Explain Django middleware" }));
    expect(await screen.findByText("Mine too and more")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    await feed(stream, completed("Mine too and more"));
    expect(screen.getByText("Mine too and more")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeTruthy();
  });

  it("keeps a message that was not sent and retries it with the same client_message_id", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage)
      .mockImplementationOnce(() => failing(new TypeError("Failed to fetch")))
      .mockImplementationOnce((_id, _body, control) => stream.events(control));
    await openChat("/chat/c1");
    sendText("Explain SSE");
    await settle();

    const failure = "Something went wrong. Check your connection and try again.";
    const list = screen.getByRole("list", { name: "Messages" });
    expect(within(list).getByText("Not sent")).toBeTruthy();
    expect(within(list).getByText(failure)).toBeTruthy();
    expect(within(list).getByText("Explain SSE")).toBeTruthy();
    expect(liveRegion()).toBe(failure);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    const [first, second] = vi.mocked(sendMessage).mock.calls;
    expect(first?.[1].client_message_id).toMatch(UUID);
    expect(second?.[1]).toEqual(first?.[1]);
    // The first attempt had reached the server, which replays it.
    await feed(stream, started(), completed("Hi there"));
    expect(screen.queryByText("Not sent")).toBeNull();
    expect(screen.getAllByText("Explain SSE")).toHaveLength(1);
    expect(screen.getByText("Hi there")).toBeTruthy();
  });

  it("explains what to do with no conversation open, or an empty one", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([]));
    renderChat();

    expect(await screen.findByRole("heading", { name: "Start a conversation" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: "Chat" })).toBeTruthy();
    expect(await within(sidebar()).findByText("No conversations yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: EXAMPLE_PROMPTS[1] }));
    expect(composer().value).toBe(EXAMPLE_PROMPTS[1]);
    expect(document.activeElement).toBe(composer());
    cleanup();

    await openChat("/chat/c1");
    expect(screen.getByRole("heading", { name: "Ask anything" })).toBeTruthy();
    expect(screen.queryByRole("list", { name: "Messages" })).toBeNull();
  });

  it("shows a dismissible banner when the list cannot be loaded or changed", async () => {
    vi.mocked(listConversations).mockRejectedValueOnce(
      new ApiError(503, { code: "SERVICE_UNAVAILABLE", message: "The service is unavailable." }, "req_5"),
    );
    renderChat();

    expect((await screen.findByRole("alert")).textContent).toBe("The service is unavailable. (ref req_5)");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await within(sidebar()).findByRole("link", { name: "Plan a trip" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();

    vi.mocked(deleteConversation).mockRejectedValueOnce(
      new ApiError(
        409,
        { code: "GENERATION_IN_PROGRESS", message: "Stop the reply being generated before deleting." },
        "req_6",
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(within(sidebar()).getByRole("button", { name: "Delete “Plan a trip”" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Stop the reply being generated before deleting. (ref req_6)",
    );
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(within(sidebar()).getByRole("link", { name: "Plan a trip" })).toBeTruthy();
  });

  it("explains when a conversation cannot be opened", async () => {
    vi.mocked(getConversation).mockRejectedValue(
      new ApiError(404, { code: "RESOURCE_NOT_FOUND", message: "Not found." }, "req_7"),
    );
    renderChat("/chat/missing");

    expect((await screen.findByRole("alert")).textContent).toBe("Not found. (ref req_7)");
    expect(composer().getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => {
      expect(location()).toBe("/chat");
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("puts the list in a drawer on narrow screens", async () => {
    mockNarrowScreen();
    renderChat();
    const menu = screen.getByRole("button", { name: "Open conversations" });
    expect(screen.queryByRole("navigation", { name: "Conversations" })).toBeNull();

    fireEvent.click(menu);
    const drawer = screen.getByRole("dialog", { name: "Conversations" });
    expect(menu.getAttribute("aria-expanded")).toBe("true");
    expect(drawer.contains(document.activeElement)).toBe(true);
    fireEvent.click(await within(drawer).findByRole("link", { name: "Plan a trip" }));

    // Choosing a conversation opens it and closes the drawer, back on the menu button.
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(location()).toBe("/chat/c2");
    expect(document.activeElement).toBe(menu);

    fireEvent.click(menu);
    fireEvent.keyDown(screen.getByRole("dialog", { name: "Conversations" }), { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(document.activeElement).toBe(menu);
  });

  it("keeps the drawer modal when the focused control goes away", async () => {
    mockNarrowScreen();
    renderChat();
    fireEvent.click(screen.getByRole("button", { name: "Open conversations" }));
    const drawer = screen.getByRole("dialog", { name: "Conversations" });
    await within(drawer).findByRole("link", { name: "Plan a trip" });

    // As when an archived or deleted conversation took the focused button with it.
    (document.activeElement as HTMLElement).blur();
    expect(document.activeElement).toBe(document.body);
    expect(fireEvent.keyDown(document.body, { key: "Tab" })).toBe(false);
    expect(drawer.contains(document.activeElement)).toBe(true);

    (document.activeElement as HTMLElement).blur();
    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("sends with Enter, but not with Shift+Enter or while composing", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    await openChat("/chat/c1");
    write("日本語");

    // An unprevented keydown lets the browser insert the newline or confirm the composition.
    expect(fireEvent.keyDown(composer(), { key: "Enter", isComposing: true })).toBe(true);
    expect(fireEvent.keyDown(composer(), { key: "Enter", keyCode: 229 })).toBe(true);
    expect(fireEvent.keyDown(composer(), { key: "Enter", shiftKey: true })).toBe(true);
    expect(sendMessage).not.toHaveBeenCalled();

    expect(fireEvent.keyDown(composer(), { key: "Enter" })).toBe(false);
    expect(sendMessage).toHaveBeenCalledOnce();
    expect(vi.mocked(sendMessage).mock.calls[0]?.[1]).toMatchObject({ content: "日本語" });

    // While the reply streams, Enter does not send again.
    write("again");
    fireEvent.keyDown(composer(), { key: "Enter" });
    expect(sendMessage).toHaveBeenCalledOnce();
    await feed(stream, started(), completed("OK"));
  });

  it("loads older messages above the ones shown", async () => {
    vi.mocked(listMessages)
      .mockResolvedValueOnce(
        thread([question, message({ content: "Done.", status: "completed" })], { next_before: 3, has_more: true }),
      )
      .mockResolvedValueOnce(thread([earlierQuestion, earlierAnswer]));
    await openChat("/chat/c1");

    fireEvent.click(screen.getByRole("button", { name: "Load older messages" }));

    expect(await screen.findByText("What is middleware?")).toBeTruthy();
    expect(listMessages).toHaveBeenLastCalledWith("c1", { limit: 50, before: 3 });
    const text = screen.getByRole("list", { name: "Messages" }).textContent;
    expect(text.indexOf("What is middleware?")).toBeLessThan(text.indexOf("Explain SSE"));
    expect(screen.queryByRole("button", { name: "Load older messages" })).toBeNull();
  });

  it("renames, archives and deletes conversations", async () => {
    vi.mocked(updateConversation).mockImplementation((id, body) =>
      Promise.resolve({ ...(id === "c2" ? trip : django), ...body }),
    );
    vi.mocked(deleteConversation).mockResolvedValue(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await openChat("/chat/c1");
    await within(sidebar()).findByRole("link", { name: "Plan a trip" });

    fireEvent.click(within(sidebar()).getByRole("button", { name: "Rename “Plan a trip”" }));
    const field = within(sidebar()).getByLabelText("Conversation title");
    fireEvent.change(field, { target: { value: "Lisbon in May" } });
    fireEvent.submit(field);
    expect(await within(sidebar()).findByRole("link", { name: "Lisbon in May" })).toBeTruthy();
    expect(updateConversation).toHaveBeenCalledWith("c2", { title: "Lisbon in May" });

    const archive = within(sidebar()).getByRole("button", { name: "Archive “Lisbon in May”" });
    archive.focus();
    fireEvent.click(archive);
    await waitFor(() => {
      expect(within(sidebar()).queryByRole("link", { name: "Lisbon in May" })).toBeNull();
    });
    expect(updateConversation).toHaveBeenLastCalledWith("c2", { status: "archived" });
    // The focused button left with its item: focus stays in the list instead of the page.
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByLabelText("Search conversations"));
    });

    fireEvent.click(within(sidebar()).getByRole("button", { name: "Delete “Explain Django middleware”" }));
    await waitFor(() => {
      expect(location()).toBe("/chat");
    });
    expect(deleteConversation).toHaveBeenCalledWith("c1");
    expect(within(sidebar()).queryByRole("link", { name: "Explain Django middleware" })).toBeNull();
  });

  it("changes the conversation's model", async () => {
    vi.mocked(updateConversation).mockResolvedValue({ ...django, model: "nexa-fast" });
    await openChat("/chat/c1");
    const select = screen.getByLabelText<HTMLSelectElement>("Model");
    await waitFor(() => {
      expect(select.disabled).toBe(false);
    });
    // No model of its own: the organization's default is shown.
    expect(select.value).toBe("nexa-balanced");
    expect(screen.getByRole("option", { name: "NEXA Balanced (default)" })).toBeTruthy();

    fireEvent.change(select, { target: { value: "nexa-fast" } });

    expect(updateConversation).toHaveBeenCalledWith("c1", { model: "nexa-fast" });
    await waitFor(() => {
      expect(select.value).toBe("nexa-fast");
    });
  });

  it("shows the user's text as typed, replies as Markdown, and copies the raw text", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    try {
      vi.mocked(listMessages).mockResolvedValue(
        thread([
          userMessage({ id: "m1", sequence: 1, content: "**not bold**\nsecond line" }),
          message({
            id: "m2",
            sequence: 2,
            content: "**bold** and `code`",
            status: "completed",
            output_tokens: 8192,
            finish_reason: "length",
          }),
        ]),
      );
      await openChat("/chat/c1");

      expect(screen.getByText(/not bold/).textContent).toBe("**not bold**\nsecond line");
      expect(screen.getByText("bold").tagName).toBe("STRONG");
      expect(screen.getByText("code").tagName).toBe("CODE");
      expect(screen.getByText("The reply was cut off: it reached the model's output limit.")).toBeTruthy();

      const copyReply = lastOf(screen.getAllByRole("button", { name: "Copy" }));
      fireEvent.click(copyReply);
      await settle();
      expect(writeText).toHaveBeenCalledWith("**bold** and `code`");
      expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy();
    } finally {
      Reflect.deleteProperty(navigator, "clipboard");
    }
  });
});

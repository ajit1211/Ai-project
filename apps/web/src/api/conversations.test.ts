import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { approval, awaitingActivity, toolActivity } from "../test/chatFixtures";
import type { ChatStreamEvent, Conversation, Message } from "../types/chat";
import { ApiError, onSessionLost, setAccessToken } from "./client";
import {
  cancelGeneration,
  createConversation,
  decideApproval,
  deleteConversation,
  getConversation,
  listConversations,
  listMessages,
  parseChatEvent,
  regenerate,
  sendMessage,
  updateConversation,
} from "./conversations";

type Call = { url: string; init: RequestInit };

const encoder = new TextEncoder();

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function frame(event: string, id: number, data: unknown): string {
  return `event: ${event}\nid: ${String(id)}\ndata: ${JSON.stringify(data)}\n\n`;
}

function sse(chunks: string[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream", "X-NEXA-Stream-Protocol": "nexa.chat.v1" },
  });
}

function message(overrides: Partial<Message>): Message {
  return {
    id: "m2",
    conversation_id: "c1",
    sequence: 2,
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
    created_at: "2026-10-09T08:05:09+00:00",
    completed_at: null,
    ...overrides,
  };
}

const user = message({
  id: "m1",
  sequence: 1,
  role: "user",
  content: "Explain SSE",
  status: "completed",
  client_message_id: "cid-1",
  model: "",
  request_id: "",
});
const pending = message({});
const completed = message({
  content: "Server-Sent Events push text.",
  status: "completed",
  provider: "mock",
  provider_model: "mock-1",
  input_tokens: 12,
  output_tokens: 34,
  cost_usd: "0.000120",
  latency_ms: 420,
  ttft_ms: 120,
  finish_reason: "stop",
  completed_at: "2026-10-09T08:05:12+00:00",
});
const start = {
  protocol: "nexa.chat.v1",
  conversation_id: "c1",
  request_id: "req_1",
  user_message: user,
  assistant_message: pending,
};
const usage = {
  message_id: "m2",
  input_tokens: 12,
  output_tokens: 34,
  total_tokens: 46,
  cached_input_tokens: 0,
  cache_write_input_tokens: 0,
  cost_usd: "0.000120",
};

const happyStream =
  frame("message_start", 1, start) +
  ": ping\n\n" +
  frame("text_delta", 2, { message_id: "m2", text: "Server-Sent " }) +
  frame("something_new", 3, { x: 1 }) +
  ": ping\n\n" +
  frame("text_delta", 4, { message_id: "m2", text: "Events push text." }) +
  frame("usage", 5, usage) +
  frame("message_complete", 6, { message: completed });

const conversation: Conversation = {
  id: "c1",
  title: "Explain SSE",
  model: "",
  status: "active",
  message_count: 2,
  generating: false,
  created_at: "2026-10-09T08:00:00+00:00",
  updated_at: "2026-10-09T08:05:12+00:00",
  last_message_at: "2026-10-09T08:05:12+00:00",
};

async function drain(
  events: AsyncIterable<ChatStreamEvent>,
): Promise<{ events: ChatStreamEvent[]; error: unknown }> {
  const seen: ChatStreamEvent[] = [];
  try {
    for await (const event of events) seen.push(event);
  } catch (error) {
    return { events: seen, error };
  }
  return { events: seen, error: null };
}

let calls: Call[];
let responses: Response[];

beforeEach(() => {
  calls = [];
  responses = [];
  setAccessToken("token");
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit) => {
      calls.push({ url, init });
      const next = responses.shift();
      if (!next) throw new Error(`unexpected request ${url}`);
      return Promise.resolve(next);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  onSessionLost(null);
});

function call(index: number): Call {
  const found = calls[index];
  if (!found) throw new Error(`no request at index ${String(index)}`);
  return found;
}

function header(index: number, name: string): string | undefined {
  return (call(index).init.headers as Record<string, string>)[name];
}

describe("sendMessage", () => {
  it("posts the message and yields the typed events, skipping pings and unknown events", async () => {
    // Chunks split inside a frame, a keep-alive comment, an event name and JSON data.
    const cuts = [
      40,
      happyStream.indexOf(": ping") + 3,
      happyStream.indexOf("text_delta") + 4,
      happyStream.indexOf('"Events push') + 5,
      happyStream.length - 25,
    ];
    const chunks = [0, ...cuts].map((from, i) => happyStream.slice(from, cuts[i]));
    expect(chunks.join("")).toBe(happyStream);
    responses.push(sse(chunks));
    const controller = new AbortController();

    const { events, error } = await drain(
      sendMessage("c1", { content: "Explain SSE", client_message_id: "cid-1" }, { signal: controller.signal }),
    );

    expect(error).toBeNull();
    expect(events).toEqual([
      { type: "message_start", ...start },
      { type: "text_delta", message_id: "m2", text: "Server-Sent " },
      { type: "text_delta", message_id: "m2", text: "Events push text." },
      { type: "usage", ...usage },
      { type: "message_complete", message: completed },
    ]);
    expect(call(0).url).toBe("/api/v1/conversations/c1/messages/");
    expect(call(0).init.method).toBe("POST");
    expect(call(0).init.signal).toBe(controller.signal);
    expect(header(0, "Accept")).toBe("text/event-stream");
    expect(header(0, "Content-Type")).toBe("application/json");
    expect(header(0, "Authorization")).toBe("Bearer token");
    expect(JSON.parse(call(0).init.body as string)).toEqual({
      content: "Explain SSE",
      client_message_id: "cid-1",
    });
  });

  it("stops reading after the terminal event", async () => {
    responses.push(
      sse([
        frame("message_start", 1, start),
        frame("message_cancelled", 2, { message: { ...pending, status: "cancelled" } }),
        frame("text_delta", 3, { message_id: "m2", text: "late" }),
      ]),
    );
    const { events, error } = await drain(sendMessage("c1", { content: "Explain SSE" }));
    expect(error).toBeNull();
    expect(events.map((event) => event.type)).toEqual(["message_start", "message_cancelled"]);
  });

  it("yields an error event as the terminal event", async () => {
    const failed = message({ content: "Ser", status: "failed", error_code: "AI_TIMEOUT" });
    const errorEvent = {
      message_id: "m2",
      code: "AI_TIMEOUT",
      message: "The AI provider took too long to answer.",
      retryable: true,
      retry_after: null,
      request_id: "req_1",
      assistant_message: failed,
    };
    responses.push(
      sse([
        frame("message_start", 1, start),
        frame("text_delta", 2, { message_id: "m2", text: "Ser" }),
        frame("error", 3, errorEvent),
      ]),
    );
    const { events, error } = await drain(sendMessage("c1", { content: "Explain SSE" }));
    expect(error).toBeNull();
    expect(events.at(-1)).toEqual({ type: "error", ...errorEvent });
  });

  it("throws NETWORK_ERROR when the stream ends before a terminal event", async () => {
    responses.push(
      sse([frame("message_start", 1, start), frame("text_delta", 2, { message_id: "m2", text: "Ser" })]),
    );
    const { events, error } = await drain(sendMessage("c1", { content: "Explain SSE" }));
    expect(events.map((event) => event.type)).toEqual(["message_start", "text_delta"]);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: "NETWORK_ERROR" });
  });

  it("turns the JSON replay of an earlier submission into start and complete events", async () => {
    responses.push(json(200, { success: true, data: { user_message: user, assistant_message: completed } }));

    const { events, error } = await drain(
      sendMessage("c1", { content: "Explain SSE", client_message_id: "cid-1" }),
    );

    expect(error).toBeNull();
    expect(events).toEqual([
      {
        type: "message_start",
        protocol: "nexa.chat.v1",
        conversation_id: "c1",
        request_id: "req_1",
        user_message: user,
        assistant_message: { ...completed, content: "", status: "pending" },
      },
      { type: "message_complete", message: completed },
    ]);
  });

  it("replays a reply that had failed as an error event", async () => {
    const failed = message({ content: "Ser", status: "failed", error_code: "AI_TIMEOUT" });
    responses.push(json(200, { success: true, data: { user_message: user, assistant_message: failed } }));

    const { events } = await drain(sendMessage("c1", { content: "Explain SSE", client_message_id: "cid-1" }));

    expect(events.map((event) => event.type)).toEqual(["message_start", "error"]);
    expect(events[1]).toMatchObject({
      type: "error",
      message_id: "m2",
      code: "AI_TIMEOUT",
      message: "The AI provider took too long to answer.",
      retryable: true,
      assistant_message: failed,
    });
  });

  it("throws the ApiError of a refusal before the stream starts", async () => {
    responses.push(
      json(409, {
        success: false,
        error: { code: "GENERATION_IN_PROGRESS", message: "A reply is already being generated." },
        request_id: "req_2",
      }),
    );
    const { events, error } = await drain(sendMessage("c1", { content: "Explain SSE" }));
    expect(events).toEqual([]);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "GENERATION_IN_PROGRESS", requestId: "req_2" });
  });
});

describe("the other conversation endpoints", () => {
  it("regenerates through the same stream", async () => {
    responses.push(sse([happyStream]));
    const { events } = await drain(regenerate("c1"));
    expect(events).toHaveLength(5);
    expect(call(0).url).toBe("/api/v1/conversations/c1/regenerate/");
    expect(call(0).init.method).toBe("POST");
    expect(header(0, "Accept")).toBe("text/event-stream");
    expect(JSON.parse(call(0).init.body as string)).toEqual({});
  });

  it("cancels the reply being generated", async () => {
    responses.push(json(200, { success: true, data: { cancelled: true, message_id: "m2" } }));
    expect(await cancelGeneration("c1")).toEqual({ cancelled: true, message_id: "m2" });
    expect(call(0).url).toBe("/api/v1/conversations/c1/cancel/");
    expect(call(0).init.method).toBe("POST");
  });

  it("lists conversations with search and cursor, and reads the paging meta", async () => {
    responses.push(
      json(200, { success: true, data: [conversation], meta: { next_cursor: "abc", has_more: true } }),
    );
    const page = await listConversations({ q: "django mw", limit: 30, cursor: "xyz" });
    expect(page).toEqual({ data: [conversation], meta: { next_cursor: "abc", has_more: true } });
    expect(call(0).url).toBe("/api/v1/conversations/?status=active&q=django+mw&limit=30&cursor=xyz");
  });

  it("lists archived conversations and reports the last page", async () => {
    responses.push(json(200, { success: true, data: [], meta: { next_cursor: null, has_more: false } }));
    const page = await listConversations({ status: "archived" });
    expect(page.meta).toEqual({ next_cursor: null, has_more: false });
    expect(call(0).url).toBe("/api/v1/conversations/?status=archived");
  });

  it("pages messages backwards with before", async () => {
    responses.push(
      json(200, { success: true, data: [user, completed], meta: { next_before: 1, has_more: true } }),
    );
    const page = await listMessages("c1", { limit: 50, before: 7 });
    expect(page).toEqual({ data: [user, completed], meta: { next_before: 1, has_more: true } });
    expect(call(0).url).toBe("/api/v1/conversations/c1/messages/?limit=50&before=7");
  });

  it("creates, reads, updates and deletes a conversation", async () => {
    responses.push(
      json(201, { success: true, data: conversation }),
      json(200, { success: true, data: conversation }),
      json(200, { success: true, data: { ...conversation, status: "archived" } }),
      json(200, { success: true, data: null, message: "Conversation deleted." }),
    );

    expect(await createConversation({ model: "nexa-fast" })).toEqual(conversation);
    expect(await getConversation("c1")).toEqual(conversation);
    expect(await updateConversation("c1", { status: "archived" })).toMatchObject({ status: "archived" });
    await deleteConversation("c1");

    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ["POST", "/api/v1/conversations/"],
      ["GET", "/api/v1/conversations/c1/"],
      ["PATCH", "/api/v1/conversations/c1/"],
      ["DELETE", "/api/v1/conversations/c1/"],
    ]);
    expect(JSON.parse(call(0).init.body as string)).toEqual({ model: "nexa-fast" });
    expect(JSON.parse(call(2).init.body as string)).toEqual({ status: "archived" });
  });
});

describe("tool calling", () => {
  const toolCall = {
    message_id: "m2",
    invocation_id: "inv-1",
    call_id: "call_1",
    tool_id: "calculator",
    tool_name: "Calculator",
    step: 1,
    status: "executing",
    summary: "Calculating 6*7",
    risk: "low",
    error_code: null,
    error_message: null,
  };
  const toolResult = {
    message_id: "m2",
    invocation_id: "inv-1",
    call_id: "call_1",
    tool_id: "calculator",
    status: "succeeded",
    duration_ms: 3,
    result: { expression: "6*7", result: 42, result_text: "42" },
    result_truncated: false,
    error_code: null,
    error_message: null,
    sources: [],
  };
  const pendingApproval = approval({ invocation_id: "inv-2" });
  const paused = message({
    content: "6*7 is 42. Shall I remember that?",
    status: "awaiting_approval",
    tools: [toolActivity(), awaitingActivity(pendingApproval)],
  });

  it("reads tool_call and tool_result, and ends the stream at approval_required", async () => {
    responses.push(
      sse([
        frame("message_start", 1, start),
        frame("tool_call", 2, toolCall),
        frame("tool_result", 3, toolResult),
        frame("text_delta", 4, { message_id: "m2", text: "6*7 is 42." }),
        frame("approval_required", 5, { message: paused, approval: pendingApproval }),
        frame("text_delta", 6, { message_id: "m2", text: "never read" }),
      ]),
    );
    const { events, error } = await drain(sendMessage("c1", { content: "What is 6*7? Remember it." }));
    expect(error).toBeNull();
    expect(events).toEqual([
      { type: "message_start", ...start },
      { type: "tool_call", ...toolCall },
      { type: "tool_result", ...toolResult },
      { type: "text_delta", message_id: "m2", text: "6*7 is 42." },
      { type: "approval_required", message: paused, approval: pendingApproval },
    ]);
  });

  it("drops tool events whose data is not an object", () => {
    expect(parseChatEvent({ event: "tool_call", id: "2", data: "[1, 2]" })).toBeNull();
    expect(parseChatEvent({ event: "tool_result", id: "3", data: "not json" })).toBeNull();
  });

  it("posts a decision with the approval's arguments hash and streams the same reply on", async () => {
    const continued = { ...paused, status: "streaming" as const };
    const done = message({ content: "6*7 is 42. Saved.", status: "completed", tools: paused.tools });
    responses.push(
      sse([
        frame("message_start", 1, { ...start, assistant_message: continued }),
        frame("tool_call", 2, { ...toolCall, invocation_id: "inv-2", tool_id: "save_memory", status: "executing" }),
        frame("text_delta", 3, { message_id: "m2", text: " Saved." }),
        frame("message_complete", 4, { message: done }),
      ]),
    );
    const controller = new AbortController();

    const { events, error } = await drain(
      decideApproval(
        "c1",
        "ap-1",
        { decision: "approve", arguments_hash: pendingApproval.arguments_hash },
        { signal: controller.signal },
      ),
    );

    expect(error).toBeNull();
    expect(events.map((event) => event.type)).toEqual(["message_start", "tool_call", "text_delta", "message_complete"]);
    expect(call(0).url).toBe("/api/v1/conversations/c1/approvals/ap-1/");
    expect(call(0).init.method).toBe("POST");
    expect(call(0).init.signal).toBe(controller.signal);
    expect(header(0, "Accept")).toBe("text/event-stream");
    expect(JSON.parse(call(0).init.body as string)).toEqual({
      decision: "approve",
      arguments_hash: "a".repeat(64),
    });
  });

  it.each([
    ["TOOL_APPROVAL_STALE", "The action changed since it was shown to you. Review it again before deciding."],
    ["TOOL_APPROVAL_EXPIRED", "This action was not approved in time and has expired."],
    ["TOOL_APPROVAL_CLOSED", "This action is no longer waiting for a decision."],
  ])("throws the ApiError of a refused decision (%s)", async (code, text) => {
    responses.push(json(409, { success: false, error: { code, message: text }, request_id: "req_8" }));
    const { events, error } = await drain(
      decideApproval("c1", "ap-1", { decision: "reject", arguments_hash: "b".repeat(64) }),
    );
    expect(events).toEqual([]);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code, message: text });
  });

  it("replays an earlier submission whose reply is paused as approval_required", async () => {
    responses.push(json(200, { success: true, data: { user_message: user, assistant_message: paused } }));
    const { events } = await drain(sendMessage("c1", { content: "Remember it.", client_message_id: "cid-1" }));
    expect(events.map((event) => event.type)).toEqual(["message_start", "approval_required"]);
    expect(events[1]).toEqual({ type: "approval_required", message: paused, approval: pendingApproval });
  });

  it("sends no tools key unless asked, and passes one through when given", async () => {
    responses.push(sse([happyStream]), sse([happyStream]));
    await drain(sendMessage("c1", { content: "Hi" }));
    await drain(sendMessage("c1", { content: "Hi", tools: "none" }));
    expect(JSON.parse(call(0).init.body as string)).toEqual({ content: "Hi" });
    expect(JSON.parse(call(1).init.body as string)).toEqual({ content: "Hi", tools: "none" });
  });
});

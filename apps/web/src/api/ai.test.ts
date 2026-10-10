import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatRequest, StreamEvent } from "../types/ai";
import { AiStreamError, chat, getMyUsage, listModels, streamChat } from "./ai";
import { ApiError, getAccessToken, onSessionLost, setAccessToken } from "./client";

type Call = { url: string; init: RequestInit };
type Next = Response | ((init: RequestInit) => Response);

const encoder = new TextEncoder();

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
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
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const request: ChatRequest = {
  model: "fast",
  messages: [{ role: "user", content: "Say hi" }],
  feature: "playground",
};

const start = {
  id: "msg_1",
  model: "fast",
  provider: "openai",
  provider_model: "gpt-4o-mini",
  request_id: "req_9",
};
const usage = {
  input_tokens: 12,
  output_tokens: 34,
  total_tokens: 46,
  cached_input_tokens: 0,
  cache_write_input_tokens: 0,
  cost_usd: "0.000120",
};
const end = { finish_reason: "stop", latency_ms: 420, attempts: 1, fallback_used: false };

const happyStream =
  frame("message.start", 1, start) +
  ": ping\n" +
  frame("message.delta", 2, { text: "Hello" }) +
  frame("message.delta", 3, { text: " world" }) +
  frame("something.new", 4, { x: 1 }) +
  frame("usage", 5, usage) +
  frame("message.end", 6, end);

const expired = {
  success: false,
  error: { code: "AUTH_TOKEN_EXPIRED", message: "expired" },
  request_id: "req_1",
};
const refreshed = { success: true, data: { access_token: "fresh", active_organization_id: null } };
const revoked = {
  success: false,
  error: { code: "AUTH_SESSION_REVOKED", message: "gone" },
  request_id: "req_2",
};

async function drain(
  events: AsyncIterable<StreamEvent>,
): Promise<{ events: StreamEvent[]; error: unknown }> {
  const seen: StreamEvent[] = [];
  try {
    for await (const event of events) seen.push(event);
  } catch (error) {
    return { events: seen, error };
  }
  return { events: seen, error: null };
}

let calls: Call[];
let responses: Next[];

beforeEach(() => {
  calls = [];
  responses = [];
  setAccessToken("stale");
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit) => {
      calls.push({ url, init });
      const next = responses.shift();
      if (!next) throw new Error(`unexpected request ${url}`);
      return Promise.resolve(typeof next === "function" ? next(init) : next);
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

describe("streamChat", () => {
  it("posts the request and yields typed events, skipping pings and unknown events", async () => {
    // Chunk boundaries fall inside frames on purpose.
    responses.push(sse([happyStream.slice(0, 37), happyStream.slice(37, 150), happyStream.slice(150)]));

    const { events, error } = await drain(streamChat(request));

    expect(error).toBeNull();
    expect(events).toEqual([
      { type: "message.start", ...start },
      { type: "message.delta", text: "Hello" },
      { type: "message.delta", text: " world" },
      { type: "usage", ...usage },
      { type: "message.end", ...end },
    ]);
    expect(call(0).url).toBe("/api/v1/ai/chat/stream/");
    expect(call(0).init.method).toBe("POST");
    expect(header(0, "Authorization")).toBe("Bearer stale");
    expect(header(0, "Content-Type")).toBe("application/json");
    expect(header(0, "Accept")).toContain("text/event-stream");
    expect(JSON.parse(call(0).init.body as string)).toEqual(request);
  });

  it("throws an ApiError with the code when refused before the stream starts", async () => {
    responses.push(
      json(422, {
        success: false,
        error: { code: "AI_MODEL_UNAVAILABLE", message: "No such model." },
        request_id: "req_3",
      }),
    );
    const { events, error } = await drain(streamChat(request));
    expect(events).toEqual([]);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 422, code: "AI_MODEL_UNAVAILABLE", requestId: "req_3" });
  });

  it("carries the Retry-After hint of a quota refusal", async () => {
    responses.push(
      json(
        429,
        {
          success: false,
          error: { code: "AI_QUOTA_EXCEEDED", message: "Quota exhausted." },
          request_id: "req_4",
        },
        { "Retry-After": "30" },
      ),
    );
    const { error } = await drain(streamChat(request));
    expect(error).toMatchObject({ code: "AI_QUOTA_EXCEEDED", retryAfter: 30 });
  });

  it("refreshes once on 401 and replays the stream request with the new token", async () => {
    responses.push(json(401, expired), json(200, refreshed), sse([happyStream]));

    const { events, error } = await drain(streamChat(request));

    expect(error).toBeNull();
    expect(events).toHaveLength(5);
    expect(calls.map((c) => c.url)).toEqual([
      "/api/v1/ai/chat/stream/",
      "/api/v1/auth/refresh/",
      "/api/v1/ai/chat/stream/",
    ]);
    expect(header(2, "Authorization")).toBe("Bearer fresh");
    expect(JSON.parse(call(2).init.body as string)).toEqual(request);
    expect(getAccessToken()).toBe("fresh");
  });

  it("ends the session when the refresh fails", async () => {
    const lost = vi.fn();
    onSessionLost(lost);
    responses.push(json(401, expired), json(401, revoked));

    const { error } = await drain(streamChat(request));

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 401, code: "AUTH_TOKEN_EXPIRED" });
    expect(lost).toHaveBeenCalledOnce();
    expect(getAccessToken()).toBeNull();
  });

  it("throws an AiStreamError after the events that preceded an error event", async () => {
    responses.push(
      sse([
        frame("message.start", 1, start),
        frame("message.delta", 2, { text: "Hel" }),
        frame("error", 3, {
          code: "AI_PROVIDER_UNAVAILABLE",
          message: "The provider is unavailable.",
          retryable: true,
          request_id: "req_5",
        }),
      ]),
    );

    const { events, error } = await drain(streamChat(request));

    expect(events.map((e) => e.type)).toEqual(["message.start", "message.delta"]);
    expect(error).toBeInstanceOf(AiStreamError);
    expect(error).toMatchObject({
      code: "AI_PROVIDER_UNAVAILABLE",
      message: "The provider is unavailable.",
      retryable: true,
      requestId: "req_5",
    });
  });

  it("passes the abort signal to fetch and surfaces the abort", async () => {
    const controller = new AbortController();
    responses.push((init) => {
      const signal = init.signal as AbortSignal;
      const body = new ReadableStream<Uint8Array>({
        start(streamController) {
          streamController.enqueue(encoder.encode(frame("message.start", 1, start)));
          signal.addEventListener("abort", () => {
            streamController.error(new DOMException("The operation was aborted.", "AbortError"));
          });
        },
      });
      return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    });

    const events = streamChat(request, { signal: controller.signal });
    const first = await events.next();
    expect(first).toMatchObject({ done: false, value: { type: "message.start" } });
    expect(call(0).init.signal).toBe(controller.signal);

    controller.abort();

    await expect(events.next()).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("the JSON endpoints", () => {
  it("lists the models", async () => {
    const model = {
      name: "fast",
      display_name: "Fast",
      description: "Quick.",
      is_default: true,
      capabilities: ["chat", "streaming"],
      context_window: 128000,
      max_output_tokens: 4096,
    };
    responses.push(json(200, { success: true, data: [model] }));
    expect(await listModels()).toEqual([model]);
    expect(call(0).url).toBe("/api/v1/ai/models/");
    expect(header(0, "Authorization")).toBe("Bearer stale");
  });

  it("posts a completion and returns the response", async () => {
    const completion = { ...start, content: "Hi!", parsed: null, usage, ...end };
    responses.push(json(200, { success: true, data: completion }));
    expect(await chat(request)).toEqual(completion);
    expect(call(0).url).toBe("/api/v1/ai/chat/");
    expect(call(0).init.method).toBe("POST");
    expect(JSON.parse(call(0).init.body as string)).toEqual(request);
  });

  it("asks for the caller's own usage", async () => {
    responses.push(json(200, { success: true, data: { scope: "me", totals: { requests: 1 } } }));
    const summary = await getMyUsage(7);
    expect(summary.scope).toBe("me");
    expect(call(0).url).toBe("/api/v1/ai/usage/?scope=me&days=7");
  });
});

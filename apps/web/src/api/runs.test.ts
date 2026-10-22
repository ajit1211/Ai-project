import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { completedRun, run, step, toolStep, waitingRun } from "../test/agentFixtures";
import type { RunEvent } from "../types/agents";
import { ApiError, setAccessToken } from "./client";
import {
  cancelRun,
  decideRunApproval,
  getRun,
  listRuns,
  listSteps,
  newIdempotencyKey,
  parseRunEvent,
  pauseRun,
  resumeRun,
  streamRunEvents,
  submitRun,
} from "./runs";

type Call = { url: string; init: RequestInit };

const encoder = new TextEncoder();

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** A frame as the server writes it: the event name, an id, and the payload without `type`. */
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
});

const body = (index: number): unknown => JSON.parse(calls[index]?.init.body as string);

async function drain(events: AsyncIterable<RunEvent>): Promise<{ events: RunEvent[]; error: unknown }> {
  const seen: RunEvent[] = [];
  try {
    for await (const event of events) seen.push(event);
  } catch (error) {
    return { events: seen, error };
  }
  return { events: seen, error: null };
}

describe("the task endpoints", () => {
  it("starts a task with its goal, input and idempotency key", async () => {
    const queued = run({ status: "queued", step_count: 0 });
    responses.push(json(202, { success: true, data: queued, message: "Task queued." }));
    const submitted = {
      agent_id: "agent-1",
      goal: "What is our refund policy?",
      input: { region: "EU" },
      idempotency_key: "key-123",
    };
    expect(await submitRun(submitted)).toEqual(queued);
    expect(calls[0]?.url).toBe("/api/v1/runs/");
    expect(calls[0]?.init.method).toBe("POST");
    expect(body(0)).toEqual(submitted);
  });

  it("reports a refused submission, too many tasks included", async () => {
    responses.push(
      json(429, {
        success: false,
        error: { code: "RUN_LIMIT_REACHED", message: "You have too many runs in progress. Wait for one to finish." },
        request_id: "req_4",
      }),
    );
    const refused = await submitRun({ agent_id: "agent-1", goal: "Hi" }).catch((err: unknown) => err);
    expect(refused).toBeInstanceOf(ApiError);
    expect(refused).toMatchObject({ status: 429, code: "RUN_LIMIT_REACHED" });
  });

  it("lists tasks with filters and a cursor, and reads the paging meta", async () => {
    responses.push(
      json(200, { success: true, data: [run()], meta: { next_cursor: "c2", has_more: true } }),
      json(200, { success: true, data: [], meta: { next_cursor: null, has_more: false } }),
    );
    expect(await listRuns({ status: "active", agent_id: "agent-1", limit: 30 })).toEqual({
      runs: [run()],
      nextCursor: "c2",
      hasMore: true,
    });
    expect(calls[0]?.url).toBe("/api/v1/runs/?status=active&agent_id=agent-1&limit=30");

    expect(await listRuns({ cursor: "c2" })).toEqual({ runs: [], nextCursor: null, hasMore: false });
    expect(calls[1]?.url).toBe("/api/v1/runs/?cursor=c2");
  });

  it("reads a task and its steps", async () => {
    responses.push(
      json(200, { success: true, data: run() }),
      json(200, { success: true, data: [step(), toolStep()] }),
      json(200, { success: true, data: [] }),
    );
    expect(await getRun("run-1")).toEqual(run());
    expect(calls[0]?.url).toBe("/api/v1/runs/run-1/");
    expect(await listSteps("run-1")).toEqual([step(), toolStep()]);
    expect(calls[1]?.url).toBe("/api/v1/runs/run-1/steps/");
    await listSteps("run-1", { after: 2, limit: 50 });
    expect(calls[2]?.url).toBe("/api/v1/runs/run-1/steps/?after=2&limit=50");
  });

  it("pauses, resumes and cancels with a POST each", async () => {
    const answer = (status: string, message: string) =>
      json(200, {
        success: true,
        data: { id: "run-1", status, cancel_requested: false, pause_requested: false, message },
        message,
      });
    responses.push(answer("paused", "Paused."), answer("queued", "Resumed."), answer("cancelled", "Cancelled."));
    expect(await pauseRun("run-1")).toMatchObject({ status: "paused", message: "Paused." });
    expect(await resumeRun("run-1")).toMatchObject({ status: "queued", message: "Resumed." });
    expect(await cancelRun("run-1")).toMatchObject({ status: "cancelled", message: "Cancelled." });
    expect(calls.map((call) => [call.init.method, call.url])).toEqual([
      ["POST", "/api/v1/runs/run-1/pause/"],
      ["POST", "/api/v1/runs/run-1/resume/"],
      ["POST", "/api/v1/runs/run-1/cancel/"],
    ]);
    expect(calls.every((call) => call.init.body === undefined)).toBe(true);
  });

  it("decides on the pending action with its arguments hash", async () => {
    const waiting = waitingRun();
    responses.push(
      json(200, { success: true, data: run({ status: "queued" }), message: "Approved." }),
      json(409, {
        success: false,
        error: {
          code: "TOOL_APPROVAL_STALE",
          message: "The action changed since it was shown to you. Review it again before deciding.",
        },
        request_id: "req_5",
      }),
    );
    const pending = waiting.pending_approval;
    if (!pending) throw new Error("the fixture waits for an approval");
    expect(
      await decideRunApproval("run-1", pending.id, { decision: "approve", arguments_hash: pending.arguments_hash }),
    ).toMatchObject({ status: "queued" });
    expect(calls[0]?.url).toBe("/api/v1/runs/run-1/approvals/ap-1/");
    expect(calls[0]?.init.method).toBe("POST");
    expect(body(0)).toEqual({ decision: "approve", arguments_hash: "a".repeat(64) });

    const stale = await decideRunApproval("run-1", "ap-1", {
      decision: "reject",
      arguments_hash: "b".repeat(64),
      comment: "Not now",
    }).catch((err: unknown) => err);
    expect(body(1)).toEqual({ decision: "reject", arguments_hash: "b".repeat(64), comment: "Not now" });
    expect(stale).toMatchObject({ status: 409, code: "TOOL_APPROVAL_STALE" });
  });
});

describe("the progress stream", () => {
  it("yields run, step and end events, skipping keep-alives and unknown events", async () => {
    const running = run();
    const done = completedRun();
    const stream =
      ": ping\n\n" +
      frame("run", 1, { run: running, step: null }) +
      frame("step", 2, { run: null, step: step() }) +
      ": ping\n\n" +
      frame("something_new", 3, { x: 1 }) +
      frame("step", 4, { run: null, step: toolStep() }) +
      frame("end", 5, { run: done, step: null });
    // Chunk boundaries fall anywhere, inside a frame included.
    responses.push(sse([stream.slice(0, 30), stream.slice(30, 200), stream.slice(200)]));
    const controller = new AbortController();

    const { events, error } = await drain(streamRunEvents("run-1", { signal: controller.signal }));
    expect(error).toBeNull();
    expect(events).toEqual([
      { type: "run", run: running },
      { type: "step", step: step() },
      { type: "step", step: toolStep() },
      { type: "end", run: done },
    ]);
    expect(calls[0]?.url).toBe("/api/v1/runs/run-1/events/");
    expect(calls[0]?.init.method).toBeUndefined(); // a GET
    expect(new Headers(calls[0]?.init.headers).get("Accept")).toBe("text/event-stream");
    expect(new Headers(calls[0]?.init.headers).get("Authorization")).toBe("Bearer token");
    expect(calls[0]?.init.signal).toBe(controller.signal);
  });

  it("stops reading after end", async () => {
    responses.push(
      sse([frame("end", 1, { run: completedRun() }), frame("run", 2, { run: run({ status: "running" }) })]),
    );
    const { events } = await drain(streamRunEvents("run-1"));
    expect(events.map((event) => event.type)).toEqual(["end"]);
  });

  it("fails when the stream closes before the task ends", async () => {
    responses.push(sse([frame("run", 1, { run: run() })]));
    const { events, error } = await drain(streamRunEvents("run-1"));
    expect(events).toHaveLength(1);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: "NETWORK_ERROR" });
  });

  it("reports a refused stream with the server's error", async () => {
    responses.push(
      json(404, {
        success: false,
        error: { code: "RESOURCE_NOT_FOUND", message: "The requested resource was not found." },
        request_id: "req_6",
      }),
    );
    const { events, error } = await drain(streamRunEvents("run-9"));
    expect(events).toEqual([]);
    expect(error).toMatchObject({ status: 404, code: "RESOURCE_NOT_FOUND" });
  });

  it("drops frames without the object their name promises", () => {
    expect(parseRunEvent({ event: "run", data: '{"run": null}' })).toBeNull();
    expect(parseRunEvent({ event: "step", data: '{"step": [1]}' })).toBeNull();
    expect(parseRunEvent({ event: "end", data: "not json" })).toBeNull();
    expect(parseRunEvent({ event: "message", data: '{"run": {}}' })).toBeNull();
    // The payload may carry its type too; the event name decides.
    expect(parseRunEvent({ event: "step", data: JSON.stringify({ type: "step", step: step() }) })).toEqual({
      type: "step",
      step: step(),
    });
  });
});

describe("idempotency keys", () => {
  it("are fresh and valid for the server", () => {
    const first = newIdempotencyKey();
    expect(first).toMatch(/^[A-Za-z0-9_.:-]{1,128}$/);
    expect(newIdempotencyKey()).not.toBe(first);
  });

  it("are made without crypto.randomUUID outside a secure context", () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes.fill(7) });
    expect(newIdempotencyKey()).toBe("07".repeat(16));
  });
});

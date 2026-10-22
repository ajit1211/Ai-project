import { describe, expect, it } from "vitest";

import { approval, awaitingActivity, chatMessage, toolActivity } from "../test/chatFixtures";
import type { Message, ToolCallEvent, ToolResultEvent } from "../types/chat";
import { buildItems, type LiveReply, type Thread } from "./chatThread";
import { applyToolCall, applyToolResult, pendingApproval, withApproval } from "./toolActivity";

const call: ToolCallEvent = {
  type: "tool_call",
  message_id: "m4",
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

const result: ToolResultEvent = {
  type: "tool_result",
  message_id: "m4",
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

describe("tool activity from stream events", () => {
  it("adds a checked call, then completes it with its result", () => {
    const running = applyToolCall([], call);
    expect(running).toEqual([
      toolActivity({ tool_version: "", status: "executing", duration_ms: null, result: null }),
    ]);
    expect(applyToolResult(running, result)).toEqual([toolActivity({ tool_version: "" })]);
  });

  it("updates a stored call in place, keeping what the event does not say", () => {
    const stored = awaitingActivity();
    const [other, resumed] = applyToolCall([toolActivity(), stored], {
      ...call,
      invocation_id: stored.invocation_id,
      tool_id: "save_memory",
      tool_name: "Save to memory",
      status: "executing",
      summary: "",
      risk: "high",
    });
    expect(other).toEqual(toolActivity());
    // Its summary and approval stay; the new state replaces the old one.
    expect(resumed).toEqual({ ...stored, status: "executing" });
  });

  it("shows a result whose call it never saw under the tool's id", () => {
    expect(applyToolResult([], { ...result, invocation_id: "inv-5", tool_id: "weather" })).toMatchObject([
      { invocation_id: "inv-5", tool_id: "weather", tool_name: "weather", status: "succeeded", summary: "" },
    ]);
  });
});

describe("approvals of a paused reply", () => {
  const paused: Message = chatMessage({ status: "awaiting_approval", tools: [toolActivity(), awaitingActivity()] });

  it("finds the pending approval only on a paused reply", () => {
    expect(pendingApproval(paused)).toEqual(approval());
    expect(pendingApproval({ ...paused, status: "completed" })).toBeNull();
    expect(pendingApproval(chatMessage({ status: "awaiting_approval", tools: [toolActivity()] }))).toBeNull();
    expect(pendingApproval(chatMessage({ status: "awaiting_approval" }))).toBeNull();
  });

  it("puts the event's approval on the call it is about", () => {
    const latest = approval({ expires_at: "2026-10-20T10:00:00+00:00" });
    const stored = { ...paused, tools: [toolActivity(), { ...awaitingActivity(), approval: null }] };
    expect(withApproval(stored, latest).tools?.[1]?.approval).toEqual(latest);
    expect(withApproval(stored, approval({ invocation_id: "inv-other" }))).toBe(stored);
  });

  it("shows the paused reply as continuing while a decision is sent", () => {
    const question = chatMessage({ id: "m3", sequence: 3, role: "user", content: "Remember it" });
    const thread: Thread = {
      conversationId: "c1",
      conversation: null,
      messages: [question, paused],
      nextBefore: null,
      hasOlder: false,
      error: null,
    };
    const live: LiveReply = {
      conversationId: "c1",
      kind: "decide",
      submission: null,
      decision: { approval: approval(), choice: "approve", replyId: "m4" },
      userMessage: null,
      reply: null,
      text: "",
      tools: [],
      usage: null,
      found: null,
      stopping: false,
    };
    const items = buildItems({ thread, live, failures: {}, finished: {} });
    // No placeholder rows: the paused reply itself is the one continuing.
    expect(items.map((item) => [item.key, item.streaming, item.deciding])).toEqual([
      ["m3", false, undefined],
      ["m4", true, "approve"],
    ]);
  });
});

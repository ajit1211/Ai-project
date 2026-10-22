import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { mergeSteps } from "../../hooks/useRun";
import { agentDetail, orgAgent, step, toolStep } from "../../test/agentFixtures";
import { changedFields, checkDraft, draftFrom } from "./agentDraft";
import {
  decisionLabel,
  describeAgentError,
  describeInputProblem,
  describeRunFailure,
  formatSeconds,
  isFinal,
  RUN_LIMIT_TEXT,
  runStatusLabel,
  runStatusTone,
} from "./labels";

describe("the words for tasks", () => {
  it("labels and colours every status, and knows which are final", () => {
    expect(runStatusLabel("waiting_for_approval")).toBe("Needs your approval");
    expect(runStatusTone("running")).toBe("info");
    expect(runStatusTone("timed_out")).toBe("down");
    expect(runStatusLabel("brand_new")).toBe("brand_new");
    expect(runStatusTone("brand_new")).toBe("neutral");
    expect(["completed", "failed", "cancelled", "timed_out"].every(isFinal)).toBe(true);
    expect(["queued", "running", "waiting_for_approval", "paused", "cancel_requested"].some(isFinal)).toBe(false);
  });

  it("says why a task did not finish, with the server's message when it adds something", () => {
    expect(
      describeRunFailure({ status: "failed", failure_reason: "token_budget", error_message: "The agent used up its token budget." }),
    ).toEqual({
      summary: "The task used up its token budget before it finished.",
      detail: "The agent used up its token budget.",
    });
    expect(describeRunFailure({ status: "failed", failure_reason: "model_unavailable", error_message: null })).toEqual({
      summary: "The agent's model is no longer available.",
      detail: null,
    });
    // Unknown reasons: the server's message, else the status.
    expect(describeRunFailure({ status: "failed", failure_reason: "new_reason", error_message: "Withdrawn." })).toEqual({
      summary: "Withdrawn.",
      detail: null,
    });
    expect(describeRunFailure({ status: "timed_out", failure_reason: null, error_message: null }).summary).toBe(
      "The task ran out of time before it finished.",
    );
    expect(describeRunFailure({ status: "failed", failure_reason: null, error_message: "  " }).summary).toBe(
      "The task did not finish.",
    );
  });

  it("names every decision, and an unknown one as sent", () => {
    expect(decisionLabel("request_approval")).toBe("Asked for approval");
    expect(decisionLabel("call_tool")).toBe("Chose a tool");
    expect(decisionLabel("teleport")).toBe("teleport");
  });

  it("writes durations", () => {
    expect(formatSeconds(45)).toBe("45 s");
    expect(formatSeconds(272)).toBe("4 min 32 s");
    expect(formatSeconds(600)).toBe("10 min");
    expect(formatSeconds(3900)).toBe("1 h 5 min");
    expect(formatSeconds(-3)).toBe("0 s");
  });

  it("explains refused task inputs by the keyword that failed", () => {
    expect(describeInputProblem({ field: "input", code: "required", message: "fails 'required'" })).toBe(
      "A required value is missing.",
    );
    expect(describeInputProblem({ field: "input.x", code: "brandNew", message: "fails 'brandNew'" })).toBe(
      "fails 'brandNew'",
    );
  });

  it("words refusals for the user, never showing an internal message", () => {
    const refusal = (status: number, code: string, message = "Server says.") => new ApiError(status, { code, message }, "req_1");
    expect(describeAgentError(refusal(429, "RUN_LIMIT_REACHED"))).toBe(RUN_LIMIT_TEXT);
    expect(RUN_LIMIT_TEXT).toContain("too many tasks in progress");
    expect(describeAgentError(refusal(409, "RUN_NOT_ACTIVE", "The task has already ended."))).toBe(
      "The task has already ended.",
    );
    expect(describeAgentError(refusal(403, "PERMISSION_DENIED"), "You cannot change this agent.")).toBe(
      "You cannot change this agent.",
    );
    expect(describeAgentError(refusal(500, "INTERNAL_ERROR", "KeyError at views.py"))).toBe(
      "Something went wrong on NEXA's side. Try again in a moment. (ref req_1)",
    );
    expect(describeAgentError(new TypeError("Failed to fetch"))).toBe(
      "Something went wrong. Check your connection and try again.",
    );
  });
});

describe("the agent form's draft", () => {
  it("turns a valid draft into the body, trimmed", () => {
    const draft = { ...draftFrom(null), name: "  Writer ", instructions: " Write. \n", allowed_tools: ["calculator"] };
    expect(checkDraft(draft)).toEqual({
      ok: true,
      body: {
        name: "Writer",
        description: "",
        instructions: "Write.",
        model: "",
        allowed_tools: ["calculator"],
        max_steps: 12,
        max_tool_calls: 20,
        max_duration_seconds: 600,
        max_tokens: 200_000,
        approval_policy: "registry",
        enabled: true,
        input_schema: null,
        output_schema: null,
      },
    });
  });

  it("sends only what changed on an agent, the tools compared as a set", () => {
    const writer = agentDetail(orgAgent());
    const draft = draftFrom(writer);
    const unchanged = checkDraft({ ...draft, allowed_tools: [...draft.allowed_tools].reverse() });
    if (!unchanged.ok) throw new Error("the draft of an agent is valid");
    expect(changedFields(unchanged.body, writer)).toEqual({});

    const changed = checkDraft({ ...draft, max_steps: "30", enabled: false, input_schema: '{"type": "object"}' });
    if (!changed.ok) throw new Error("still valid");
    expect(changedFields(changed.body, writer)).toEqual({
      max_steps: 30,
      enabled: false,
      input_schema: { type: "object" },
    });
  });
});

describe("a task's steps", () => {
  it("are added and replaced by id, in order, and a finished step never goes back", () => {
    const running = toolStep({ status: "running", finished_at: null });
    const done = toolStep();
    const merged = mergeSteps([running], [step(), done]);
    expect(merged.map((item) => [item.id, item.status])).toEqual([
      ["s1", "succeeded"],
      ["s2", "succeeded"],
    ]);
    // An older copy (still running) arriving late is ignored.
    expect(mergeSteps(merged, [running])[1]?.status).toBe("succeeded");
  });
});

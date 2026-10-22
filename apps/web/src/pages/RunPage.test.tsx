/**
 * The task page: a task followed live through its progress stream (steps upserted, the task
 * replaced, the end), polling when the stream fails, the timeline of every kind of step, the
 * approval the task waits for (approve, decline, a refused decision), the controls, the
 * budget, and the result or the failure of a finished task.
 */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import {
  cancelRun,
  decideRunApproval,
  getRun,
  listSteps,
  pauseRun,
  resumeRun,
  streamRunEvents,
  type RequestControl,
} from "../api/runs";
import { AuthContext } from "../auth/AuthContext";
// The page loads the answer (and Markdown with it) lazily; loading it here, up front, keeps a
// busy test run from waiting on that import.
import "../components/agents/RunResult";
import { completedRun, failedRun, run, step, toolStep, waitingRun } from "../test/agentFixtures";
import { approval, awaitingActivity, toolActivity } from "../test/chatFixtures";
import { signedIn } from "../test/documentFixtures";
import type { Run, RunEvent, Step } from "../types/agents";
import { RunPage } from "./RunPage";

vi.mock("../api/runs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/runs")>();
  return {
    ...actual,
    getRun: vi.fn(),
    listSteps: vi.fn(),
    streamRunEvents: vi.fn(),
    pauseRun: vi.fn(),
    resumeRun: vi.fn(),
    cancelRun: vi.fn(),
    decideRunApproval: vi.fn(),
  };
});

/** A progress stream the test feeds event by event; it ends after `end`, on failure or on abort. */
function controlledStream() {
  const queue: RunEvent[] = [];
  let failure: Error | null = null;
  let wake: () => void = () => undefined;
  return {
    push(...events: RunEvent[]) {
      queue.push(...events);
      wake();
    },
    fail(error: Error) {
      failure = error;
      wake();
    },
    async *events(control?: RequestControl): AsyncGenerator<RunEvent, void, undefined> {
      const signal = control?.signal;
      for (;;) {
        const next = queue.shift();
        if (next) {
          yield next;
          if (next.type === "end") return;
          continue;
        }
        if (failure) throw failure;
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

function renderPage() {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={["/agents/runs/run-1"]}>
        <Routes>
          <Route path="/agents/runs/:runId" element={<RunPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

async function feed(stream: ControlledStream, ...events: RunEvent[]): Promise<void> {
  await act(async () => {
    stream.push(...events);
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

/** The task as the page loads it, and its steps; no stream unless the test gives one. */
function stored(task: Run, steps: Step[] = []): void {
  vi.mocked(getRun).mockResolvedValue(task);
  vi.mocked(listSteps).mockResolvedValue(steps);
}

function streaming(): ControlledStream {
  const stream = controlledStream();
  vi.mocked(streamRunEvents).mockImplementation((_id, control) => stream.events(control));
  return stream;
}

const statusRegion = () => document.querySelector(".run-status") as HTMLElement;
/** The live region of action outcomes (the status region is the other one). */
const notice = () => document.querySelector('.run-page > .visually-hidden[aria-live="polite"]')?.textContent ?? "";
const timeline = () => screen.getByRole("list", { name: "Steps" });
/** Each step's head: kind, summary, state, duration and tokens. */
const stepRows = () =>
  [...document.querySelectorAll(".run-step-head")].map((row) =>
    [...row.children]
      .map((part) => part.textContent)
      .filter(Boolean)
      .join(" "),
  );
const card = () => screen.getByRole("region", { name: "Approval needed" });
const meter = (label: string) => {
  const term = within(screen.getByRole("region", { name: "Budget" })).getByText(label, { selector: "dt" });
  return term.parentElement?.querySelector(".run-meter-value")?.textContent ?? "";
};

describe("RunPage", () => {
  beforeEach(() => {
    // A stream that stays open, sending nothing, unless a test gives its own.
    vi.mocked(streamRunEvents).mockImplementation((_id, control) => controlledStream().events(control));
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.resetAllMocks();
  });

  it("follows a task live: new steps, changed steps, the task, then its end", async () => {
    stored(run(), [step()]);
    const stream = streaming();
    renderPage();

    expect(await screen.findByRole("heading", { name: "What is our refund policy?", level: 1 })).toBeTruthy();
    expect(screen.getByText("Document Assistant · version 1")).toBeTruthy();
    expect(statusRegion().getAttribute("aria-live")).toBe("polite");
    expect(statusRegion().textContent).toBe("RunningSearching the documents");
    expect(getRun).toHaveBeenCalledWith("run-1", { signal: expect.any(AbortSignal) as AbortSignal });
    expect(listSteps).toHaveBeenCalledWith("run-1", {}, { signal: expect.any(AbortSignal) as AbortSignal });
    await settle();
    expect(streamRunEvents).toHaveBeenCalledWith("run-1", { signal: expect.any(AbortSignal) as AbortSignal });
    expect(stepRows()).toEqual(["Decision Search the refund policy Done 900 ms 440 tokens"]);

    // The stream starts with the task and every step so far: nothing is shown twice.
    const searching = toolStep({
      status: "running",
      finished_at: null,
      duration_ms: null,
      tool: toolActivity({
        invocation_id: "inv-5",
        tool_id: "search_documents",
        tool_name: "Document search",
        summary: "Searching documents for “refund policy”",
        status: "executing",
        duration_ms: null,
        result: null,
      }),
    });
    await feed(stream, { type: "run", run: run() }, { type: "step", step: step() }, { type: "step", step: searching });
    expect(stepRows()).toEqual([
      "Decision Search the refund policy Done 900 ms 440 tokens",
      "Tool Searching documents for “refund policy” Running",
    ]);
    expect(document.querySelector(".run-step .tool-spinner")).not.toBeNull();

    // The same step again, now done: replaced in place.
    await feed(stream, { type: "step", step: toolStep() });
    expect(stepRows()[1]).toBe("Tool Searching documents for “refund policy” Done 12 ms");
    await feed(
      stream,
      { type: "run", run: run({ step_count: 3, progress_message: "Writing the answer", updated_at: "2026-10-22T09:00:09+00:00" }) },
    );
    expect(statusRegion().textContent).toBe("RunningWriting the answer");

    const answered = step({
      id: "s3",
      sequence: 3,
      kind: "final",
      summary: "Completed",
      detail: { answer_chars: 70, cited: ["R1"], invalid_citations: ["R7"], has_output: true },
      input_tokens: 0,
      output_tokens: 0,
      duration_ms: null,
    });
    await feed(stream, { type: "step", step: answered }, { type: "end", run: completedRun() });
    expect(stepRows()[2]).toBe("Answer Completed Done");
    expect(within(timeline()).getByText("Cites R1.")).toBeTruthy();
    expect(
      within(timeline()).getByText("The citation R7 could not be verified; it is shown as plain text."),
    ).toBeTruthy();
    expect(statusRegion().textContent).toBe("CompletedDone");
    expect(await screen.findByRole("region", { name: "Answer" })).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Task controls" })).toBeNull();
  });

  it("shows a finished task without following it", async () => {
    stored(completedRun(), [step()]);
    renderPage();
    await screen.findByRole("region", { name: "Answer" });
    await settle();
    expect(streamRunEvents).not.toHaveBeenCalled();
  });

  it("shows the answer with its citations, structured output and sources", async () => {
    stored(completedRun());
    renderPage();
    const answer = await screen.findByRole("region", { name: "Answer" });
    expect(within(answer).getByText("30 days").tagName).toBe("STRONG"); // Markdown, safely rendered

    // [R1] leads to its source; [R2]'s document is gone, and says so.
    const cite = within(answer).getByRole("button", { name: "Source R1: refunds.md" });
    expect(cite.textContent).toBe("1");
    expect(within(answer).getByRole("button", { name: "Source R2: no longer available" })).toBeTruthy();

    const sources = within(answer).getByRole("list", { name: "Sources" });
    const [first, second] = within(sources).getAllByRole("listitem");
    expect(within(first as HTMLElement).getByRole("link", { name: "refunds.md" }).getAttribute("href")).toBe(
      "/documents/doc-1",
    );
    expect(within(second as HTMLElement).getByText("No longer available")).toBeTruthy();
    expect(within(second as HTMLElement).queryByRole("link")).toBeNull();
    expect(within(second as HTMLElement).queryByText("plans.md")).toBeNull();

    fireEvent.click(cite);
    await waitFor(() => {
      expect(document.activeElement).toBe(first);
    });
    expect(first?.classList.contains("highlighted")).toBe(true);

    const output = within(answer).getByLabelText("Structured output as JSON");
    expect(JSON.parse(output.textContent)).toEqual({ answer: "Refunds within 30 days.", answered: true });
  });

  it("says why a task did not finish", async () => {
    stored(failedRun());
    const { unmount } = renderPage();
    const failure = await screen.findByRole("region", { name: "Why the task did not finish" });
    expect(within(failure).getByText("The agent reached its step limit before it finished.")).toBeTruthy();
    expect(within(failure).getByText("The agent did not finish within its step limit.")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Answer" })).toBeNull();
    unmount();

    stored(failedRun({ failure_reason: "model_unavailable", error_message: null }));
    const second = renderPage();
    expect(await screen.findByText("The agent's model is no longer available.")).toBeTruthy();
    second.unmount();

    // A reason this client does not know: the server's message says it.
    stored(failedRun({ failure_reason: "brand_new_reason", error_message: "The agent's budget was withdrawn." }));
    renderPage();
    expect(await screen.findByText("The agent's budget was withdrawn.")).toBeTruthy();
  });

  it("shows every kind of step with what is worth knowing about it", async () => {
    const proposal = step({
      id: "s3",
      sequence: 3,
      summary: "Asking to run save_memory",
      detail: {
        action: "request_approval",
        decision: {
          action: "request_approval",
          tool: "save_memory",
          arguments: { content: "Prefers metric units" },
          message: "Saving this keeps your preference for next time.",
        },
        context_dropped: 2,
      },
    });
    const progress = step({
      id: "s4",
      sequence: 4,
      kind: "progress",
      summary: "Found the policy",
      detail: { message: "Found the policy" },
      input_tokens: 0,
      output_tokens: 0,
      duration_ms: null,
    });
    const strange = step({
      id: "s5",
      sequence: 5,
      summary: "Thinking",
      detail: { action: "meditate", decision: { action: "meditate" } },
      input_tokens: 0,
      output_tokens: 0,
      duration_ms: null,
    });
    const problem = step({
      id: "s6",
      sequence: 6,
      kind: "error",
      status: "failed",
      summary: "The decision was not valid",
      detail: { reason: "'action' must be one of call_tool, request_approval." },
      error_code: "invalid_decision",
      input_tokens: 0,
      output_tokens: 0,
      duration_ms: null,
      retry_count: 1,
    });
    stored(completedRun(), [step(), toolStep(), proposal, progress, strange, problem]);
    renderPage();
    await screen.findByRole("list", { name: "Steps" });

    expect(stepRows()).toEqual([
      "Decision Search the refund policy Done 900 ms 440 tokens",
      "Tool Searching documents for “refund policy” Done 12 ms",
      "Decision Asking to run save_memory Done 900 ms 440 tokens",
      "Progress Found the policy Done",
      "Decision Thinking Done",
      "Problem The decision was not valid Failed",
    ]);
    const [decision, tool, asked, , unknown, error] = [...timeline().children] as HTMLElement[];

    // The decision: what was chosen, with the arguments folded away.
    expect(within(decision as HTMLElement).getByText("Chose a tool", { exact: false }).textContent).toBe(
      "Chose a tool: search_documents",
    );
    fireEvent.click(within(decision as HTMLElement).getByText("Show arguments"));
    expect(within(decision as HTMLElement).getByText(/"query": "refund policy"/)).toBeTruthy();

    // The tool call, as the chat shows one: its result under "Show result".
    fireEvent.click(within(tool as HTMLElement).getByText("Show result"));
    expect(within(tool as HTMLElement).getByRole("link", { name: "refunds.md" }).getAttribute("href")).toBe(
      "/documents/doc-1",
    );

    // Asking for approval, with why.
    expect(within(asked as HTMLElement).getByText("Asked for approval", { exact: false }).textContent).toBe(
      "Asked for approval: save_memory",
    );
    expect(within(asked as HTMLElement).getByText("Why: Saving this keeps your preference for next time.")).toBeTruthy();
    expect(
      within(asked as HTMLElement).getByText("2 earlier messages were left out of the model's context to save space."),
    ).toBeTruthy();

    // An action this client does not know is shown as sent.
    expect(within(unknown as HTMLElement).getByText("meditate")).toBeTruthy();

    // A problem says what went wrong, and a retried step says so.
    expect(within(error as HTMLElement).getByText("'action' must be one of call_tool, request_approval.")).toBeTruthy();
    expect(within(error as HTMLElement).getByText("Retried once.")).toBeTruthy();
  });

  it("approves the pending action with its arguments hash, and the task continues", async () => {
    const waiting = waitingRun();
    stored(waiting, [step(), toolStep({ id: "s3", sequence: 3, status: "waiting", tool: awaitingActivity(), summary: "Saving to your memories", finished_at: null, duration_ms: null })]);
    streaming();
    vi.mocked(decideRunApproval).mockResolvedValue(
      run({ status: "queued", progress_message: "Approved; continuing", updated_at: "2026-10-22T09:00:12+00:00" }),
    );
    renderPage();

    const approvalCard = await screen.findByRole("region", { name: "Approval needed" });
    expect(within(approvalCard).getByText("Saving to your memories")).toBeTruthy();
    expect(within(approvalCard).getByText("Save to memory")).toBeTruthy();
    expect(within(approvalCard).getByText("High risk")).toBeTruthy();
    expect(within(approvalCard).getByText("Save a memory")).toBeTruthy();
    expect(within(approvalCard).getByText("Expires").nextElementSibling?.querySelector("time")?.getAttribute("dateTime")).toBe(
      waiting.pending_approval?.expires_at,
    );
    expect(statusRegion().textContent).toContain("Needs your approval");
    expect(stepRows()[1]).toBe("Tool Saving to your memories Needs your approval");
    expect(within(timeline()).getByText("Waiting for your decision, above.")).toBeTruthy();
    expect(meter("Time")).toBe("4 min left (clock stopped)");

    fireEvent.click(within(card()).getByRole("button", { name: "Approve" }));
    expect(decideRunApproval).toHaveBeenCalledWith("run-1", "ap-1", { decision: "approve", arguments_hash: "a".repeat(64) });
    expect(within(card()).getByRole<HTMLButtonElement>("button", { name: "Approving…" }).disabled).toBe(true);
    expect(within(card()).getByRole<HTMLButtonElement>("button", { name: "Decline" }).disabled).toBe(true);

    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();
    });
    expect(statusRegion().textContent).toBe("QueuedApproved; continuing");
    expect(document.activeElement).toBe(statusRegion());
    expect(notice()).toBe("Action approved; the task continues.");
  });

  it("declines the pending action", async () => {
    stored(waitingRun());
    streaming();
    vi.mocked(decideRunApproval).mockResolvedValue(run({ status: "queued", progress_message: "Declined; continuing" }));
    renderPage();
    fireEvent.click(await within(await screen.findByRole("region", { name: "Approval needed" })).findByRole("button", { name: "Decline" }));
    expect(decideRunApproval).toHaveBeenCalledWith("run-1", "ap-1", { decision: "reject", arguments_hash: "a".repeat(64) });
    expect(within(card()).getByRole("button", { name: "Declining…" })).toBeTruthy();
    await waitFor(() => {
      expect(notice()).toBe("Action declined; the task continues.");
    });
  });

  it("says why a decision was refused, shows the action as it is now, and decides on that one", async () => {
    const changed = approval({ id: "ap-2", arguments_hash: "c".repeat(64), summary: "Saving the new text" });
    vi.mocked(getRun)
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValue(waitingRun({ pending_approval: changed, updated_at: "2026-10-22T09:00:11+00:00" }));
    vi.mocked(listSteps).mockResolvedValue([]);
    streaming();
    vi.mocked(decideRunApproval)
      .mockRejectedValueOnce(
        new ApiError(
          409,
          {
            code: "TOOL_APPROVAL_STALE",
            message: "The action changed since it was shown to you. Review it again before deciding.",
          },
          "req_9",
        ),
      )
      .mockResolvedValueOnce(run({ status: "queued", updated_at: "2026-10-22T09:00:12+00:00" }));
    renderPage();

    fireEvent.click(await within(await screen.findByRole("region", { name: "Approval needed" })).findByRole("button", { name: "Approve" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "The action changed since it was shown to you. Review it again before deciding.",
    );
    expect(await within(card()).findByText("Saving the new text")).toBeTruthy();
    expect(getRun).toHaveBeenCalledTimes(2);

    fireEvent.click(within(card()).getByRole("button", { name: "Approve" }));
    expect(decideRunApproval).toHaveBeenLastCalledWith("run-1", "ap-2", { decision: "approve", arguments_hash: "c".repeat(64) });
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();
    });
  });

  it("shows a decision that came too late, and the task it ended", async () => {
    vi.mocked(getRun)
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValue(
        failedRun({
          failure_reason: "approval_expired",
          error_message: "The action was not approved in time.",
          updated_at: "2026-10-22T09:16:00+00:00",
        }),
      );
    vi.mocked(listSteps).mockResolvedValue([]);
    streaming();
    vi.mocked(decideRunApproval).mockRejectedValue(
      new ApiError(409, { code: "TOOL_APPROVAL_EXPIRED", message: "This action was not approved in time and has expired." }, "req_8"),
    );
    renderPage();
    fireEvent.click(await within(await screen.findByRole("region", { name: "Approval needed" })).findByRole("button", { name: "Approve" }));
    expect((await screen.findByRole("alert")).textContent).toBe("This action was not approved in time and has expired.");
    expect(await screen.findByText("An action waited too long for your approval and expired.")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();
  });

  it("pauses and resumes, as the server allows, then shows the task as it is now", async () => {
    vi.mocked(getRun)
      .mockResolvedValueOnce(run())
      .mockResolvedValueOnce(
        run({
          status: "paused",
          can_pause: false,
          can_resume: true,
          deadline_at: null,
          time_left_seconds: 200,
          progress_message: "Paused",
          updated_at: "2026-10-22T09:00:30+00:00",
        }),
      )
      .mockResolvedValueOnce(run({ status: "queued", progress_message: "", updated_at: "2026-10-22T09:00:40+00:00" }));
    vi.mocked(listSteps).mockResolvedValue([]);
    streaming();
    vi.mocked(pauseRun).mockResolvedValue({
      id: "run-1",
      status: "paused",
      cancel_requested: false,
      pause_requested: false,
      message: "Paused.",
    });
    vi.mocked(resumeRun).mockResolvedValue({
      id: "run-1",
      status: "queued",
      cancel_requested: false,
      pause_requested: false,
      message: "Resumed.",
    });
    renderPage();

    const controls = await screen.findByRole("group", { name: "Task controls" });
    expect(within(controls).queryByRole("button", { name: "Resume" })).toBeNull();
    fireEvent.click(within(controls).getByRole("button", { name: "Pause" }));
    expect(pauseRun).toHaveBeenCalledWith("run-1");
    expect(within(controls).getByRole<HTMLButtonElement>("button", { name: "Pausing…" }).disabled).toBe(true);
    await waitFor(() => {
      expect(statusRegion().textContent).toBe("PausedPaused");
    });
    expect(notice()).toBe("Paused.");
    expect(meter("Time")).toBe("3 min 20 s left (clock stopped)");
    expect(document.activeElement).toBe(statusRegion());

    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(resumeRun).toHaveBeenCalledWith("run-1");
    await waitFor(() => {
      expect(statusRegion().textContent).toBe("Queued");
    });
    expect(notice()).toBe("Resumed.");
  });

  it("cancels only after a confirmation", async () => {
    vi.mocked(getRun)
      .mockResolvedValueOnce(run())
      .mockResolvedValue(
        run({
          status: "cancel_requested",
          cancel_requested: true,
          can_pause: false,
          can_cancel: false,
          updated_at: "2026-10-22T09:00:30+00:00",
        }),
      );
    vi.mocked(listSteps).mockResolvedValue([]);
    streaming();
    vi.mocked(cancelRun).mockResolvedValue({
      id: "run-1",
      status: "cancel_requested",
      cancel_requested: true,
      pause_requested: false,
      message: "Cancelling: the run stops at its next checkpoint.",
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Cancel task" }));
    const question = screen.getByRole("group", { name: "Cancel this task? It stops and cannot be resumed." });
    expect(document.activeElement).toBe(within(question).getByRole("button", { name: "Keep it running" }));
    fireEvent.click(within(question).getByRole("button", { name: "Keep it running" }));
    expect(cancelRun).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel task" }));

    fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
    fireEvent.click(within(screen.getByRole("group", { name: /Cancel this task/ })).getByRole("button", { name: "Cancel task" }));
    await waitFor(() => {
      expect(cancelRun).toHaveBeenCalledWith("run-1");
    });
    await waitFor(() => {
      expect(statusRegion().textContent).toContain("Cancelling");
    });
    expect(screen.queryByRole("button", { name: "Cancel task" })).toBeNull();
    expect(notice()).toBe("Cancelling: the run stops at its next checkpoint.");
  });

  it("says that a running task will pause at its next checkpoint", async () => {
    stored(run({ pause_requested: true, can_pause: false }));
    streaming();
    renderPage();
    await screen.findByRole("heading", { level: 1 });
    expect(statusRegion().textContent).toBe("RunningSearching the documentsIt will pause at its next checkpoint.");
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel task" })).toBeTruthy();
  });

  it("says why a control was refused", async () => {
    stored(run());
    streaming();
    vi.mocked(pauseRun).mockRejectedValue(
      new ApiError(409, { code: "RUN_NOT_ACTIVE", message: "Only a queued or running task can be paused." }, "req_6"),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Only a queued or running task can be paused.");
    expect(getRun).toHaveBeenCalledTimes(2); // the task as it is now
  });

  it("shows how much of its budget the task has used", async () => {
    stored(run({ step_count: 7, tool_call_count: 1 }));
    streaming();
    renderPage();
    await screen.findByRole("region", { name: "Budget" });
    expect(meter("Steps")).toBe("7 of 8");
    expect(meter("Tool calls")).toBe("1 of 6");
    expect(meter("Tokens")).toBe(`907 of ${(120_000).toLocaleString()}`);
    // Nearly used up: the bar warns.
    const steps = within(screen.getByRole("region", { name: "Budget" })).getByText("Steps").parentElement;
    expect(steps?.querySelector(".run-meter-high")).not.toBeNull();
    // Running: the time counts down to the deadline.
    await waitFor(() => {
      expect(meter("Time")).toMatch(/ left$/);
    });
  });

  it("polls the task when the stream fails, until it is over", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    vi.mocked(getRun)
      .mockResolvedValueOnce(run())
      .mockResolvedValueOnce(run({ step_count: 3, progress_message: "Writing the answer", updated_at: "2026-10-22T09:00:09+00:00" }))
      .mockResolvedValue(completedRun());
    vi.mocked(listSteps)
      .mockResolvedValueOnce([step()])
      .mockResolvedValueOnce([step(), toolStep()])
      .mockResolvedValue([step(), toolStep()]);
    vi.mocked(streamRunEvents).mockImplementation(async function* () {
      await Promise.resolve();
      throw new ApiError(0, { code: "NETWORK_ERROR", message: "The progress stream closed before the task finished." }, null);
      yield* []; // never reached; keeps the function a generator
    });
    renderPage();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(stepRows()).toHaveLength(1);
    expect(screen.getByText("Live updates are unavailable; checking for changes every few seconds.")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(getRun).toHaveBeenCalledTimes(2);
    expect(stepRows()).toHaveLength(2);
    expect(statusRegion().textContent).toBe("RunningWriting the answer");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(statusRegion().textContent).toBe("CompletedDone");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getRun).toHaveBeenCalledTimes(3);
  });

  it("opens the stream again when it closes after serving a while", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-22T09:00:00+00:00"));
    stored(run());
    const first = controlledStream();
    const second = controlledStream();
    vi.mocked(streamRunEvents)
      .mockImplementationOnce((_id, control) => first.events(control))
      .mockImplementationOnce((_id, control) => second.events(control));
    renderPage();
    await screen.findByRole("heading", { level: 1 });
    await settle();

    // An hour on, the server ends the stream (it closes each one after an hour).
    vi.setSystemTime(new Date("2026-10-22T10:00:00+00:00"));
    await act(async () => {
      first.fail(new ApiError(0, { code: "NETWORK_ERROR", message: "The progress stream closed." }, null));
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(streamRunEvents).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/Live updates are unavailable/)).toBeNull();
    expect(getRun).toHaveBeenCalledTimes(1);

    await feed(second, { type: "end", run: completedRun() });
    expect(statusRegion().textContent).toBe("CompletedDone");
  });

  it("stops following when the page goes away", async () => {
    stored(run());
    const stream = streaming();
    let signal: AbortSignal | undefined;
    vi.mocked(streamRunEvents).mockImplementation((_id, control) => {
      signal = control?.signal;
      return stream.events(control);
    });
    const { unmount } = renderPage();
    await screen.findByRole("heading", { level: 1 });
    await settle();
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it("says when the task does not exist", async () => {
    vi.mocked(getRun).mockRejectedValue(
      new ApiError(404, { code: "RESOURCE_NOT_FOUND", message: "The requested resource was not found." }, "req_1"),
    );
    vi.mocked(listSteps).mockResolvedValue([]);
    renderPage();
    expect(await screen.findByRole("heading", { name: "Task not found" })).toBeTruthy();
  });
});

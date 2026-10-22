import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useParams } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getAgent } from "../api/agents";
import { ApiError } from "../api/client";
import { submitRun } from "../api/runs";
import { AuthContext } from "../auth/AuthContext";
import { agent, agentDetail, orgAgent, run } from "../test/agentFixtures";
import { signedIn } from "../test/documentFixtures";
import type { JsonSchema, Run } from "../types/agents";
import { NewRunPage } from "./NewRunPage";

vi.mock("../api/agents", () => ({
  listAgents: vi.fn(),
  getAgent: vi.fn(),
  createAgent: vi.fn(),
  updateAgent: vi.fn(),
  deleteAgent: vi.fn(),
}));

vi.mock("../api/runs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/runs")>();
  return { ...actual, submitRun: vi.fn() };
});

/** The task page a started task opens. */
function TaskPage() {
  const { runId } = useParams();
  return <p>Task {runId}</p>;
}

function renderForm(agentId = "agent-1") {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={[`/agents/${agentId}/run`]}>
        <Routes>
          <Route path="/agents/:agentId/run" element={<NewRunPage />} />
          <Route path="/agents/runs/:runId" element={<TaskPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const start = () => {
  fireEvent.click(screen.getByRole("button", { name: /^Start/ }));
};
/** The error under a control, which describes it. */
const errorOf = (control: HTMLElement): string | null => {
  const ids = control.getAttribute("aria-describedby")?.split(" ") ?? [];
  const error = ids.map((id) => document.getElementById(id)).find((node) => node?.classList.contains("field-error"));
  return error?.textContent ?? null;
};
const sent = () => vi.mocked(submitRun).mock.calls.map(([body]) => body);

/** A schema whose properties all become controls. */
const BRIEF_SCHEMA: JsonSchema = {
  type: "object",
  properties: {
    topic: { type: "string", title: "Topic", maxLength: 120, description: "What the notes are about." },
    tone: { type: "string", enum: ["formal", "casual"] },
    max_items: { type: "integer", minimum: 1, maximum: 10, default: 3 },
    include_drafts: { type: "boolean" },
    notes: { type: "string" },
  },
  required: ["topic", "tone"],
};

describe("NewRunPage", () => {
  beforeEach(() => {
    vi.mocked(getAgent).mockResolvedValue(agentDetail(agent()));
    vi.mocked(submitRun).mockResolvedValue(run({ status: "queued" }));
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("starts a task with the goal and an idempotency key, then opens it", async () => {
    renderForm();
    expect(await screen.findByRole("heading", { name: "Document Assistant", level: 1 })).toBeTruthy();
    expect(getAgent).toHaveBeenCalledWith("agent-1", { signal: expect.any(AbortSignal) as AbortSignal });
    // The Document Assistant takes a question.
    const question = screen.getByRole("textbox", { name: "Question" });
    expect(question.getAttribute("aria-required")).toBe("true");
    fireEvent.change(question, { target: { value: "What is our refund policy?" } });
    start();

    await waitFor(() => {
      expect(submitRun).toHaveBeenCalledOnce();
    });
    const [body] = sent();
    expect(body).toEqual({
      agent_id: "agent-1",
      goal: "What is our refund policy?",
      input: {},
      idempotency_key: expect.stringMatching(/^[A-Za-z0-9_.:-]{1,128}$/) as string,
    });
    expect(await screen.findByText("Task run-1")).toBeTruthy();
  });

  it("starts one task however often the form is submitted, and retries with the same key", async () => {
    let fail: (error: Error) => void = () => undefined;
    vi.mocked(submitRun)
      .mockImplementationOnce(
        () =>
          new Promise<Run>((_resolve, reject) => {
            fail = reject;
          }),
      )
      .mockResolvedValueOnce(run({ status: "queued" }));
    renderForm();
    fireEvent.change(await screen.findByRole("textbox", { name: "Question" }), { target: { value: "Refunds?" } });
    start();
    start();
    fireEvent.submit(screen.getByRole("form", { name: "New task" }));
    expect(submitRun).toHaveBeenCalledOnce();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Starting…" }).disabled).toBe(true);

    // The answer was lost: trying again sends the same key, so the server starts no second task.
    fail(new TypeError("Failed to fetch"));
    expect(await screen.findByRole("alert")).toBeTruthy();
    start();
    await screen.findByText("Task run-1");
    const [first, second] = sent();
    expect(second?.idempotency_key).toBe(first?.idempotency_key);
  });

  it("asks for the goal before sending", async () => {
    renderForm();
    const question = await screen.findByRole("textbox", { name: "Question" });
    fireEvent.change(question, { target: { value: "   " } });
    start();
    expect(errorOf(question)).toBe("Describe the task.");
    await waitFor(() => {
      expect(document.activeElement).toBe(question);
    });
    expect(submitRun).not.toHaveBeenCalled();
  });

  it("makes the inputs of an object schema, and sends what they hold", async () => {
    vi.mocked(getAgent).mockResolvedValue(agentDetail(orgAgent({ has_input_schema: true }), { input_schema: BRIEF_SCHEMA }));
    renderForm("agent-2");
    const inputs = await screen.findByRole("group", { name: "Inputs" });

    const topic = within(inputs).getByRole("textbox", { name: "Topic" });
    expect(topic.tagName).toBe("INPUT");
    expect(topic.getAttribute("maxLength")).toBe("120");
    expect(topic.getAttribute("aria-required")).toBe("true");
    expect(within(inputs).getByText("What the notes are about.")).toBeTruthy();
    const tone = within(inputs).getByRole<HTMLSelectElement>("combobox", { name: "Tone" });
    expect([...tone.options].map((option) => option.text)).toEqual(["Choose…", "formal", "casual"]);
    const items = within(inputs).getByRole<HTMLInputElement>("spinbutton", { name: "Max items" });
    expect(items.value).toBe("3");
    expect(items.min).toBe("1");
    expect(items.max).toBe("10");
    expect(items.getAttribute("aria-required")).toBeNull();
    const drafts = within(inputs).getByRole<HTMLInputElement>("checkbox", { name: "Include drafts" });
    expect(drafts.checked).toBe(false);
    // Text without a length limit gets a text area.
    expect(within(inputs).getByRole("textbox", { name: "Notes" }).tagName).toBe("TEXTAREA");

    fireEvent.change(screen.getByRole("textbox", { name: "Goal" }), { target: { value: "Draft the notes" } });
    fireEvent.change(topic, { target: { value: "Release 4.2" } });
    fireEvent.change(tone, { target: { value: "1" } });
    fireEvent.change(items, { target: { value: "5" } });
    fireEvent.click(drafts);
    start();
    await waitFor(() => {
      expect(submitRun).toHaveBeenCalledOnce();
    });
    expect(sent()[0]).toMatchObject({
      agent_id: "agent-2",
      goal: "Draft the notes",
      input: { topic: "Release 4.2", tone: "casual", max_items: 5, include_drafts: true },
    });
  });

  it("checks the inputs before sending", async () => {
    vi.mocked(getAgent).mockResolvedValue(agentDetail(orgAgent({ has_input_schema: true }), { input_schema: BRIEF_SCHEMA }));
    renderForm("agent-2");
    const inputs = await screen.findByRole("group", { name: "Inputs" });
    fireEvent.change(screen.getByRole("textbox", { name: "Goal" }), { target: { value: "Draft the notes" } });
    const items = within(inputs).getByRole("spinbutton", { name: "Max items" });
    fireEvent.change(items, { target: { value: "11" } });
    start();

    const topic = within(inputs).getByRole("textbox", { name: "Topic" });
    expect(errorOf(topic)).toBe("Enter a value.");
    expect(errorOf(within(inputs).getByRole("combobox", { name: "Tone" }))).toBe("Choose a value.");
    expect(errorOf(items)).toBe(`Enter ${(10).toLocaleString()} or less.`);
    await waitFor(() => {
      expect(document.activeElement).toBe(topic);
    });
    expect(submitRun).not.toHaveBeenCalled();
  });

  it("puts the server's input errors next to their inputs", async () => {
    vi.mocked(getAgent).mockResolvedValue(agentDetail(orgAgent({ has_input_schema: true }), { input_schema: BRIEF_SCHEMA }));
    vi.mocked(submitRun).mockRejectedValue(
      new ApiError(
        422,
        {
          code: "VALIDATION_FAILED",
          message: "Some fields are invalid.",
          details: [
            { field: "input.topic", code: "pattern", message: "fails 'pattern'" },
            { field: "input", code: "required", message: "fails 'required'" },
          ],
        },
        "req_1",
      ),
    );
    renderForm("agent-2");
    const inputs = await screen.findByRole("group", { name: "Inputs" });
    fireEvent.change(screen.getByRole("textbox", { name: "Goal" }), { target: { value: "Draft the notes" } });
    fireEvent.change(within(inputs).getByRole("textbox", { name: "Topic" }), { target: { value: "!!" } });
    fireEvent.change(within(inputs).getByRole("combobox", { name: "Tone" }), { target: { value: "0" } });
    start();

    const topic = within(inputs).getByRole("textbox", { name: "Topic" });
    await waitFor(() => {
      expect(errorOf(topic)).toBe("This does not have the expected form.");
    });
    expect(within(inputs).getByText("A required value is missing.")).toBeTruthy();
    await waitFor(() => {
      expect(document.activeElement).toBe(topic);
    });
  });

  it("says when too many tasks are in progress", async () => {
    vi.mocked(submitRun).mockRejectedValue(
      new ApiError(
        429,
        { code: "RUN_LIMIT_REACHED", message: "You have too many runs in progress. Wait for one to finish." },
        "req_2",
      ),
    );
    renderForm();
    fireEvent.change(await screen.findByRole("textbox", { name: "Question" }), { target: { value: "Refunds?" } });
    start();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("too many tasks in progress");
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Start task" }).disabled).toBe(false);
  });

  it("says when the agent cannot be run, or the caller may not run agents", async () => {
    vi.mocked(submitRun)
      .mockRejectedValueOnce(new ApiError(404, { code: "AGENT_UNAVAILABLE", message: "That agent is not available." }, "req_3"))
      .mockRejectedValueOnce(
        new ApiError(403, { code: "PERMISSION_DENIED", message: "You do not have permission to perform this action." }, "req_4"),
      );
    renderForm();
    fireEvent.change(await screen.findByRole("textbox", { name: "Question" }), { target: { value: "Refunds?" } });
    start();
    expect((await screen.findByRole("alert")).textContent).toBe(
      "This agent is not available: it may have been switched off or deleted.",
    );
    start();
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toBe("You do not have permission to run agents in this organization.");
    });
  });

  it("asks for JSON when the schema is more than simple fields", async () => {
    const schema: JsonSchema = {
      type: "object",
      properties: { tags: { type: "array", items: { type: "string" } } },
      required: ["tags"],
    };
    vi.mocked(getAgent).mockResolvedValue(agentDetail(orgAgent({ has_input_schema: true }), { input_schema: schema }));
    renderForm("agent-2");
    const json = await screen.findByRole("textbox", { name: "Input (JSON)" });
    fireEvent.change(screen.getByRole("textbox", { name: "Goal" }), { target: { value: "Tag it" } });
    fireEvent.change(json, { target: { value: "[1]" } });
    start();
    expect(errorOf(json)).toBe("Enter a JSON object, in braces: { … }.");

    fireEvent.change(json, { target: { value: '{"tags": ["a", "b"]}' } });
    start();
    await waitFor(() => {
      expect(submitRun).toHaveBeenCalledOnce();
    });
    expect(sent()[0]?.input).toEqual({ tags: ["a", "b"] });
  });

  it("starts nothing with an agent that is switched off", async () => {
    vi.mocked(getAgent).mockResolvedValue(agentDetail(orgAgent({ enabled: false })));
    renderForm("agent-2");
    expect(await screen.findByText("This agent is switched off, so no task can be started with it.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Start task" })).toBeNull();
  });

  it("says when the agent does not exist", async () => {
    vi.mocked(getAgent).mockRejectedValue(
      new ApiError(404, { code: "RESOURCE_NOT_FOUND", message: "The requested resource was not found." }, "req_5"),
    );
    renderForm("agent-9");
    expect(await screen.findByRole("heading", { name: "Agent not found" })).toBeTruthy();
  });
});

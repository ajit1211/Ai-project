import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listAgents } from "../api/agents";
import { ApiError } from "../api/client";
import { listRuns } from "../api/runs";
import { AuthContext } from "../auth/AuthContext";
import { agent, completedRun, failedRun, orgAgent, run, waitingRun } from "../test/agentFixtures";
import { signedIn } from "../test/documentFixtures";
import type { Run, RunPage } from "../types/agents";
import { RunsPage } from "./RunsPage";

vi.mock("../api/agents", () => ({
  listAgents: vi.fn(),
  getAgent: vi.fn(),
  createAgent: vi.fn(),
  updateAgent: vi.fn(),
  deleteAgent: vi.fn(),
}));

vi.mock("../api/runs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/runs")>();
  return { ...actual, listRuns: vi.fn() };
});

function page(runs: Run[], nextCursor: string | null = null): RunPage {
  return { runs, nextCursor, hasMore: nextCursor !== null };
}

function renderPage() {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={["/agents/runs"]}>
        <Routes>
          <Route path="/agents/runs" element={<RunsPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const tasks = () => screen.getByRole("list", { name: "Tasks" });

function itemFor(goal: string): HTMLElement {
  const item = within(tasks()).getByRole("link", { name: goal }).closest("li");
  if (!item) throw new Error(`no task for ${goal}`);
  return item;
}

const running = run();
const done = completedRun({ id: "run-2", goal: "Summarize the Q4 report" });
const broken = failedRun({ id: "run-3", goal: "Write the release notes", agent_id: "agent-2", agent_name: "Release notes writer" });

describe("RunsPage", () => {
  beforeEach(() => {
    vi.mocked(listRuns).mockResolvedValue(page([running, done, broken]));
    vi.mocked(listAgents).mockResolvedValue([agent(), orgAgent()]);
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("lists the tasks with their agent, goal, status, steps and start time", async () => {
    renderPage();
    const items = within(await screen.findByRole("list", { name: "Tasks" })).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(listRuns).toHaveBeenCalledWith(
      { status: undefined, agent_id: undefined, limit: 30 },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );

    const first = itemFor("What is our refund policy?");
    expect(within(first).getByRole("link").getAttribute("href")).toBe("/agents/runs/run-1");
    expect(within(first).getByText("Document Assistant")).toBeTruthy();
    expect(within(first).getByText("2 of 8 steps")).toBeTruthy();
    expect(within(first).getByText("Running", { selector: ".badge" }).classList.contains("badge-info")).toBe(true);
    expect(within(first).getByText(/Started/).querySelector("time")?.getAttribute("dateTime")).toBe(running.created_at);

    expect(within(itemFor("Summarize the Q4 report")).getByText("Completed", { selector: ".badge" }).classList.contains("badge-up")).toBe(true);
    const failure = itemFor("Write the release notes");
    expect(within(failure).getByText("Release notes writer")).toBeTruthy();
    expect(within(failure).getByText("Failed", { selector: ".badge" }).classList.contains("badge-down")).toBe(true);
  });

  it("filters by status and by agent", async () => {
    renderPage();
    await screen.findByRole("list", { name: "Tasks" });
    const status = screen.getByRole<HTMLSelectElement>("combobox", { name: "Filter by status" });
    expect([...status.options].map((option) => option.value)).toEqual([
      "",
      "active",
      "finished",
      "queued",
      "running",
      "waiting_for_approval",
      "paused",
      "cancel_requested",
      "completed",
      "failed",
      "cancelled",
      "timed_out",
    ]);
    vi.mocked(listRuns).mockResolvedValue(page([waitingRun()]));
    fireEvent.change(status, { target: { value: "active" } });
    await waitFor(() => {
      expect(listRuns).toHaveBeenLastCalledWith({ status: "active", agent_id: undefined, limit: 30 }, expect.anything());
    });
    expect(await within(tasks()).findByText("Needs your approval", { selector: ".badge" })).toBeTruthy();

    const agentFilter = screen.getByRole("combobox", { name: "Filter by agent" });
    await within(agentFilter).findByRole("option", { name: "Release notes writer" });
    fireEvent.change(agentFilter, { target: { value: "agent-2" } });
    await waitFor(() => {
      expect(listRuns).toHaveBeenLastCalledWith({ status: "active", agent_id: "agent-2", limit: 30 }, expect.anything());
    });
  });

  it("loads the next page with the cursor", async () => {
    const older = completedRun({ id: "run-0", goal: "An older question" });
    vi.mocked(listRuns)
      .mockResolvedValueOnce(page([running], "c2"))
      .mockResolvedValueOnce(page([older]));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await within(tasks()).findByRole("link", { name: "An older question" })).toBeTruthy();
    expect(listRuns).toHaveBeenLastCalledWith({ status: undefined, agent_id: undefined, limit: 30, cursor: "c2" });
    expect(within(tasks()).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("says when there are no tasks, and when no task matches the filters", async () => {
    vi.mocked(listRuns).mockResolvedValue(page([]));
    renderPage();
    expect(await screen.findByText("No tasks yet.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "agent library" }).getAttribute("href")).toBe("/agents");

    fireEvent.change(screen.getByRole("combobox", { name: "Filter by status" }), { target: { value: "failed" } });
    expect(await screen.findByText("No tasks match these filters.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("No tasks yet.")).toBeTruthy();
    expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "Filter by status" }).value).toBe("");
  });

  it("shows fixed wording for an unexpected failure, and loads again on request", async () => {
    vi.mocked(listRuns)
      .mockRejectedValueOnce(new ApiError(500, { code: "INTERNAL_ERROR", message: "boom" }, "req_8"))
      .mockResolvedValueOnce(page([running]));
    renderPage();
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Something went wrong on NEXA's side. Try again in a moment. (ref req_8)",
    );
    expect(screen.getByText("The tasks could not be loaded.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await within(await screen.findByRole("list", { name: "Tasks" })).findByText("What is our refund policy?")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, type InitialEntry } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { deleteAgent, listAgents } from "../api/agents";
import { ApiError } from "../api/client";
import { AuthContext } from "../auth/AuthContext";
import { agent, orgAgent } from "../test/agentFixtures";
import { signedIn } from "../test/documentFixtures";
import { AgentsPage } from "./AgentsPage";

vi.mock("../api/agents", () => ({
  listAgents: vi.fn(),
  getAgent: vi.fn(),
  createAgent: vi.fn(),
  updateAgent: vi.fn(),
  deleteAgent: vi.fn(),
}));

function renderPage(entry: InitialEntry = "/agents") {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/agents" element={<AgentsPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const list = () => screen.getByRole("list", { name: "Agents" });
const liveRegion = () => document.querySelector('.agents-page > [aria-live="polite"]')?.textContent ?? "";

function cardOf(name: string): HTMLElement {
  const heading = within(list()).getByRole("heading", { name });
  const card = heading.closest("li");
  if (!card) throw new Error(`no card for ${name}`);
  return card;
}

describe("AgentsPage", () => {
  beforeEach(() => {
    // The server lists the platform's agents first; the page puts the Document Assistant first anyway.
    vi.mocked(listAgents).mockResolvedValue([orgAgent(), agent()]);
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("shows each agent with its origin, tools, limits and version, the Document Assistant first", async () => {
    renderPage();
    await screen.findByRole("list", { name: "Agents" });
    const cards = [...list().children] as HTMLElement[];
    expect(cards).toHaveLength(2);
    expect(listAgents).toHaveBeenCalledWith({}, { signal: expect.any(AbortSignal) as AbortSignal });

    const assistant = cards[0] as HTMLElement;
    expect(within(assistant).getByRole("heading", { name: "Document Assistant" })).toBeTruthy();
    expect(within(assistant).getByText("Start here")).toBeTruthy();
    expect(within(assistant).getByText("Platform")).toBeTruthy();
    expect(within(assistant).getByText("Version 1")).toBeTruthy();
    const tools = within(within(assistant).getByRole("list", { name: "Tools" })).getAllByRole("listitem");
    expect(tools.map((tool) => tool.textContent)).toEqual(["Document searchMedium risk", "Document detailsLow risk"]);
    // Numbers are written the user's way (120,000 or 1,20,000).
    const tokens = (120_000).toLocaleString();
    expect(
      within(assistant).getByText(`Up to 8 steps, 6 tool calls, 5 min and ${tokens} tokens per task.`, { exact: false }),
    ).toBeTruthy();
    expect(within(assistant).getByText(/Answers with structured output\./)).toBeTruthy();
    expect(within(assistant).getByRole("link", { name: "Run Document Assistant" }).getAttribute("href")).toBe(
      "/agents/agent-1/run",
    );
    // A platform agent can be run, never changed.
    expect(within(assistant).queryByRole("link", { name: /Edit/ })).toBeNull();
    expect(within(assistant).queryByRole("button", { name: /Delete/ })).toBeNull();

    const writer = cardOf("Release notes writer");
    expect(within(writer).getByText("Organization")).toBeTruthy();
    expect(within(writer).queryByText("Start here")).toBeNull();
    const save = within(writer).getByText("Save to memory").closest("li") as HTMLElement;
    expect(within(save).getByText("High risk", { selector: ".badge" }).classList.contains("badge-down")).toBe(true);
    expect(within(save).getByText("Needs approval")).toBeTruthy();
    expect(within(writer).getByText(/Model: nexa-fast\./)).toBeTruthy();
    expect(within(writer).getByRole("link", { name: "Edit Release notes writer" }).getAttribute("href")).toBe(
      "/agents/agent-2/edit",
    );
    expect(within(writer).getByRole("button", { name: "Delete Release notes writer" })).toBeTruthy();

    expect(screen.getByRole("link", { name: "New agent" }).getAttribute("href")).toBe("/agents/new");
    expect(screen.getByRole("link", { name: "Task history" }).getAttribute("href")).toBe("/agents/runs");
  });

  it("hides Edit and Delete where the server does not allow them, and Run for an agent switched off", async () => {
    vi.mocked(listAgents).mockResolvedValue([
      agent(),
      orgAgent({ can_edit: false, can_delete: false, enabled: false }),
    ]);
    renderPage();
    await screen.findByRole("list", { name: "Agents" });
    const writer = cardOf("Release notes writer");
    expect(within(writer).getByText("Switched off")).toBeTruthy();
    expect(within(writer).queryByRole("link")).toBeNull();
    expect(within(writer).queryByRole("button")).toBeNull();
  });

  it("asks every tool for approval under the all_tool_calls policy", async () => {
    vi.mocked(listAgents).mockResolvedValue([orgAgent({ approval_policy: "all_tool_calls" })]);
    renderPage();
    await screen.findByRole("list", { name: "Agents" });
    expect(within(cardOf("Release notes writer")).getAllByText("Needs approval")).toHaveLength(2);
  });

  it("deletes an agent after an inline confirmation", async () => {
    vi.mocked(deleteAgent).mockResolvedValue(undefined);
    renderPage();
    await screen.findByRole("list", { name: "Agents" });
    fireEvent.click(screen.getByRole("button", { name: "Delete Release notes writer" }));
    const question = screen.getByRole("group", {
      name: "Delete “Release notes writer”? The tasks it carried out stay in their history.",
    });
    expect(deleteAgent).not.toHaveBeenCalled();
    fireEvent.click(within(question).getByRole("button", { name: "Delete" }));
    await waitFor(() => {
      expect(deleteAgent).toHaveBeenCalledWith("agent-2");
    });
    await waitFor(() => {
      expect(within(list()).queryByRole("heading", { name: "Release notes writer" })).toBeNull();
    });
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Agents", level: 1 }));
    expect(liveRegion()).toBe("Release notes writer deleted.");
  });

  it("keeps an agent whose deletion was refused, and says why", async () => {
    vi.mocked(deleteAgent).mockRejectedValue(
      new ApiError(403, { code: "PERMISSION_DENIED", message: "You cannot delete this agent." }, "req_3"),
    );
    renderPage();
    await screen.findByRole("list", { name: "Agents" });
    fireEvent.click(screen.getByRole("button", { name: "Delete Release notes writer" }));
    fireEvent.click(within(screen.getByRole("group")).getByRole("button", { name: "Delete" }));
    expect((await screen.findByRole("alert")).textContent).toBe("You cannot delete this agent.");
    expect(cardOf("Release notes writer")).toBeTruthy();
  });

  it("shows fixed wording for an unexpected failure, and loads again on request", async () => {
    vi.mocked(listAgents)
      .mockRejectedValueOnce(new ApiError(500, { code: "INTERNAL_ERROR", message: "Traceback ..." }, "req_7"))
      .mockResolvedValueOnce([agent()]);
    renderPage();
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Something went wrong on NEXA's side. Try again in a moment. (ref req_7)",
    );
    expect(screen.getByText("The agents could not be loaded.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await within(await screen.findByRole("list", { name: "Agents" })).findByText("Document Assistant")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says so when there are no agents", async () => {
    vi.mocked(listAgents).mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText("No agents yet.")).toBeTruthy();
  });

  it("confirms an agent saved on its form", async () => {
    renderPage({ pathname: "/agents", state: { saved: "Release notes writer" } });
    expect((await screen.findByRole("status")).textContent).toBe("“Release notes writer” was saved.");
  });
});

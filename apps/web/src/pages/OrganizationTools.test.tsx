/** The organization page's AI tools: listed with switches for those who may manage them. */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api/client";
import { listTools, setToolEnabled } from "../api/tools";
import type { OrganizationDetail } from "../api/types";
import { AuthContext } from "../auth/AuthContext";
import { toolInfo } from "../test/chatFixtures";
import { signedIn } from "../test/documentFixtures";
import { OrganizationPage } from "./OrganizationPage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: vi.fn() };
});

vi.mock("../api/tools", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/tools")>();
  return { ...actual, listTools: vi.fn(), setToolEnabled: vi.fn() };
});

function organization(permissions: string[], id = "o1"): OrganizationDetail {
  return {
    id,
    name: "Acme",
    slug: "acme",
    kind: "team",
    created_at: "2026-10-01T08:00:00+00:00",
    role: { id: "r1", key: "admin", name: "Admin" },
    settings: {},
    permissions,
  };
}

function renderPage(organizationId = "o1") {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={[`/organizations/${organizationId}`]}>
        <Routes>
          <Route path="/organizations/:organizationId" element={<OrganizationPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const calculator = toolInfo();
const memory = toolInfo({
  id: "save_memory",
  name: "Save to memory",
  description: "Save a short statement to your memories.",
  risk: "high",
  read_only: false,
  requires_approval: true,
});
const weather = toolInfo({
  id: "weather",
  name: "Weather",
  description: "Forecasts.",
  enabled: false,
  available: false,
  disabled_by: "platform",
});

describe("the organization's AI tools", () => {
  beforeEach(() => {
    vi.mocked(api).mockImplementation((path: string) =>
      Promise.resolve({ data: path.endsWith("/o2/") ? organization(["ai.tool.manage"], "o2") : organization(["ai.tool.manage"]) }),
    );
    vi.mocked(listTools).mockResolvedValue([calculator, memory, weather]);
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("lists the tools with switches, and switches one off", async () => {
    vi.mocked(setToolEnabled).mockResolvedValue({ ...calculator, enabled: false, available: false, disabled_by: "organization" });
    renderPage();

    const list = await screen.findByRole("list", { name: "AI tools" });
    const switches = within(list).getAllByRole<HTMLInputElement>("switch");
    expect(switches.map((item) => [item.getAttribute("aria-label"), item.checked, item.disabled])).toEqual([
      ["Let AI use Calculator", true, false],
      ["Let AI use Save to memory", true, false],
      ["Let AI use Weather", false, true],
    ]);
    expect(within(list).getByText("High risk")).toBeTruthy();
    expect(within(list).getByText("Asks for approval")).toBeTruthy();
    expect(within(list).getByText("Switched off for the whole platform.")).toBeTruthy();

    fireEvent.click(within(list).getByRole("switch", { name: "Let AI use Calculator" }));
    expect(setToolEnabled).toHaveBeenCalledWith("calculator", false);
    expect(await screen.findByText("Calculator is off for this organization.")).toBeTruthy();
    expect(within(list).getByRole<HTMLInputElement>("switch", { name: "Let AI use Calculator" }).checked).toBe(false);
  });

  it("shows a refused change", async () => {
    vi.mocked(setToolEnabled).mockRejectedValue(
      new ApiError(403, { code: "PERMISSION_DENIED", message: "You do not have permission to do that." }, "req_4"),
    );
    renderPage();
    const list = await screen.findByRole("list", { name: "AI tools" });
    fireEvent.click(within(list).getByRole("switch", { name: "Let AI use Save to memory" }));
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toBe("You do not have permission to do that. (ref req_4)");
    });
    expect(within(list).getByRole<HTMLInputElement>("switch", { name: "Let AI use Save to memory" }).checked).toBe(true);
  });

  it("is not shown to members who may not manage tools", async () => {
    vi.mocked(api).mockImplementation((path: string) =>
      Promise.resolve({ data: path.endsWith("/members/") ? [] : organization(["members.view"]) }),
    );
    renderPage();
    expect(await screen.findByRole("heading", { name: "Acme" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "AI tools" })).toBeNull();
    expect(listTools).not.toHaveBeenCalled();
  });

  it("asks to switch organization first on another organization's page", async () => {
    renderPage("o2");
    expect(await screen.findByText("Switch to this organization to manage the tools its AI may use.")).toBeTruthy();
    expect(listTools).not.toHaveBeenCalled();
  });
});

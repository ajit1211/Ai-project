import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listModels } from "../api/ai";
import { createAgent, getAgent, updateAgent } from "../api/agents";
import { ApiError } from "../api/client";
import { listTools } from "../api/tools";
import { AuthContext } from "../auth/AuthContext";
import { agent, agentDetail, orgAgent } from "../test/agentFixtures";
import { toolInfo } from "../test/chatFixtures";
import { signedIn } from "../test/documentFixtures";
import { AgentFormPage } from "./AgentFormPage";

vi.mock("../api/agents", () => ({
  listAgents: vi.fn(),
  getAgent: vi.fn(),
  createAgent: vi.fn(),
  updateAgent: vi.fn(),
  deleteAgent: vi.fn(),
}));

vi.mock("../api/tools", () => ({ listTools: vi.fn(), setToolEnabled: vi.fn() }));

vi.mock("../api/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/ai")>();
  return { ...actual, listModels: vi.fn() };
});

/** Where saving leads: the library, with the saved agent's name in the navigation state. */
function Library() {
  const location = useLocation();
  return <p>Library {JSON.stringify(location.state)}</p>;
}

function renderForm(path: string) {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/agents" element={<Library />} />
          <Route path="/agents/new" element={<AgentFormPage />} />
          <Route path="/agents/:agentId/edit" element={<AgentFormPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const textbox = (name: string) => screen.getByRole<HTMLInputElement>("textbox", { name });
const number = (name: string) => screen.getByRole<HTMLInputElement>("spinbutton", { name });
const save = (name = "Create agent") => {
  fireEvent.click(screen.getByRole("button", { name }));
};
/** The error under a control, which describes it. */
const errorOf = (control: HTMLElement): string | null => {
  const ids = control.getAttribute("aria-describedby")?.split(" ") ?? [];
  const error = ids.map((id) => document.getElementById(id)).find((node) => node?.classList.contains("field-error"));
  return error?.textContent ?? null;
};

const calculator = toolInfo();
const memory = toolInfo({
  id: "save_memory",
  name: "Save to memory",
  description: "Saves something to your memories.",
  risk: "high",
  read_only: false,
  requires_approval: true,
});
const switchedOff = toolInfo({ id: "workspace_info", name: "Workspace info", enabled: false, available: false });

describe("AgentFormPage", () => {
  beforeEach(() => {
    vi.mocked(listTools).mockResolvedValue([calculator, memory, switchedOff]);
    vi.mocked(listModels).mockResolvedValue([
      {
        name: "nexa-balanced",
        display_name: "Balanced",
        description: "",
        is_default: true,
        capabilities: [],
        context_window: 200_000,
        max_output_tokens: 8192,
      },
      {
        name: "nexa-fast",
        display_name: "Fast",
        description: "",
        is_default: false,
        capabilities: [],
        context_window: 200_000,
        max_output_tokens: 8192,
      },
    ]);
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("creates an agent from the form, then returns to the library", async () => {
    vi.mocked(createAgent).mockResolvedValue(agentDetail(orgAgent({ name: "Release notes writer" })));
    renderForm("/agents/new");
    expect(screen.getByRole("heading", { name: "New agent", level: 1 })).toBeTruthy();

    fireEvent.change(textbox("Name"), { target: { value: "  Release notes writer " } });
    fireEvent.change(textbox("Description"), { target: { value: "Drafts release notes." } });
    fireEvent.change(textbox("Instructions"), { target: { value: "Write short release notes." } });
    await screen.findByRole("option", { name: "Fast" });
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "nexa-fast" } });

    // Only the tools available here are offered, each with its risk and whether it asks first.
    const tools = screen.getByRole("group", { name: "Tools" });
    await within(tools).findByRole("checkbox", { name: /Calculator/ });
    expect(within(tools).queryByRole("checkbox", { name: /Workspace info/ })).toBeNull();
    const memoryOption = within(tools).getByRole("checkbox", { name: /Save to memory/ });
    const memoryLabel = memoryOption.closest("label") as HTMLElement;
    expect(within(memoryLabel).getByText("High risk")).toBeTruthy();
    expect(within(memoryLabel).getByText("Needs approval")).toBeTruthy();
    fireEvent.click(within(tools).getByRole("checkbox", { name: /Calculator/ }));
    fireEvent.click(memoryOption);

    fireEvent.change(number("Steps"), { target: { value: "20" } });
    fireEvent.change(number("Tool calls"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("radio", { name: "Ask before every tool call" }));
    fireEvent.change(textbox("Output schema"), {
      target: { value: '{"type": "object", "properties": {"notes": {"type": "string"}}}' },
    });
    save();

    await waitFor(() => {
      expect(createAgent).toHaveBeenCalledOnce();
    });
    expect(createAgent).toHaveBeenCalledWith({
      name: "Release notes writer",
      description: "Drafts release notes.",
      instructions: "Write short release notes.",
      model: "nexa-fast",
      allowed_tools: ["calculator", "save_memory"],
      max_steps: 20,
      max_tool_calls: 5,
      max_duration_seconds: 600,
      max_tokens: 200_000,
      approval_policy: "all_tool_calls",
      enabled: true,
      input_schema: null,
      output_schema: { type: "object", properties: { notes: { type: "string" } } },
    });
    expect(await screen.findByText('Library {"saved":"Release notes writer"}')).toBeTruthy();
  });

  it("checks the form before sending, and moves focus to the first problem", async () => {
    renderForm("/agents/new");
    fireEvent.change(number("Steps"), { target: { value: "99" } });
    fireEvent.change(number("Token budget"), { target: { value: "" } });
    fireEvent.change(textbox("Input schema"), { target: { value: "{not json" } });
    fireEvent.change(textbox("Output schema"), { target: { value: "[1, 2]" } });
    save();

    expect(screen.getByRole("alert").textContent).toBe("Some fields need your attention.");
    expect(errorOf(textbox("Name"))).toBe("Enter a name.");
    expect(textbox("Name").getAttribute("aria-invalid")).toBe("true");
    expect(errorOf(textbox("Instructions"))).toBe("Enter the instructions.");
    expect(errorOf(number("Steps"))).toBe(`Enter a number from 1 to ${(50).toLocaleString()}.`);
    expect(errorOf(number("Token budget"))).toBe("Enter a whole number.");
    expect(errorOf(number("Tool calls"))).toBeNull();
    expect(errorOf(textbox("Input schema"))).toBe("This is not valid JSON.");
    expect(errorOf(textbox("Output schema"))).toBe("Enter a JSON object, in braces: { … }.");
    await waitFor(() => {
      expect(document.activeElement).toBe(textbox("Name"));
    });
    expect(createAgent).not.toHaveBeenCalled();
  });

  it("shows the server's field errors next to their fields", async () => {
    vi.mocked(createAgent).mockRejectedValue(
      new ApiError(
        422,
        {
          code: "VALIDATION_FAILED",
          message: "Some fields are invalid.",
          details: [
            { field: "model", code: "unknown", message: "That model is not available." },
            { field: "input_schema", code: "invalid", message: "The schema is not a valid JSON Schema." },
          ],
        },
        "req_1",
      ),
    );
    renderForm("/agents/new");
    fireEvent.change(textbox("Name"), { target: { value: "Writer" } });
    fireEvent.change(textbox("Instructions"), { target: { value: "Write." } });
    fireEvent.change(textbox("Input schema"), { target: { value: '{"type": "nope"}' } });
    save();

    const model = screen.getByRole("combobox", { name: "Model" });
    await waitFor(() => {
      expect(errorOf(model)).toBe("That model is not available.");
    });
    expect(errorOf(textbox("Input schema"))).toBe("The schema is not a valid JSON Schema.");
    expect(screen.queryByRole("alert")).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(model);
    });
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Create agent" }).disabled).toBe(false);
  });

  it("says why a tool was refused, under the tools", async () => {
    vi.mocked(createAgent).mockRejectedValue(
      new ApiError(422, { code: "TOOL_NOT_ALLOWED", message: "The tool 'Calculator' is switched off." }, "req_2"),
    );
    renderForm("/agents/new");
    fireEvent.change(textbox("Name"), { target: { value: "Writer" } });
    fireEvent.change(textbox("Instructions"), { target: { value: "Write." } });
    fireEvent.click(await screen.findByRole("checkbox", { name: /Calculator/ }));
    save();

    const tools = screen.getByRole("group", { name: "Tools" });
    expect(await within(tools).findByText("The tool 'Calculator' is switched off.")).toBeTruthy();
    await waitFor(() => {
      expect(document.activeElement).toBe(tools);
    });
  });

  it("says so when the caller may not create agents", async () => {
    vi.mocked(createAgent).mockRejectedValue(
      new ApiError(403, { code: "PERMISSION_DENIED", message: "You do not have permission to perform this action." }, "req_3"),
    );
    renderForm("/agents/new");
    fireEvent.change(textbox("Name"), { target: { value: "Writer" } });
    fireEvent.change(textbox("Instructions"), { target: { value: "Write." } });
    save();
    expect((await screen.findByRole("alert")).textContent).toBe(
      "You do not have permission to create agents in this organization.",
    );
  });

  it("edits an agent, sending only what changed", async () => {
    const writer = agentDetail(orgAgent({ model: "nexa-fast" }));
    vi.mocked(getAgent).mockResolvedValue(writer);
    vi.mocked(updateAgent).mockResolvedValue({ ...writer, name: "Notes writer" });
    renderForm("/agents/agent-2/edit");

    expect(await screen.findByRole("heading", { name: "Edit Release notes writer", level: 1 })).toBeTruthy();
    expect(getAgent).toHaveBeenCalledWith("agent-2", { signal: expect.any(AbortSignal) as AbortSignal });
    expect(textbox("Name").value).toBe("Release notes writer");
    expect(textbox("Instructions").value).toBe(writer.instructions);
    expect(number("Steps").value).toBe("12");
    await waitFor(() => {
      expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "Model" }).value).toBe("nexa-fast");
    });
    const tools = screen.getByRole("group", { name: "Tools" });
    expect(within(tools).getByRole<HTMLInputElement>("checkbox", { name: /Calculator/ }).checked).toBe(true);
    expect(within(tools).getByRole<HTMLInputElement>("checkbox", { name: /Save to memory/ }).checked).toBe(true);

    fireEvent.change(textbox("Name"), { target: { value: "Notes writer" } });
    save("Save changes");
    await waitFor(() => {
      expect(updateAgent).toHaveBeenCalledWith("agent-2", { name: "Notes writer" });
    });
    expect(await screen.findByText('Library {"saved":"Notes writer"}')).toBeTruthy();
  });

  it("keeps a tool the agent has that is no longer available, so it can be removed", async () => {
    const writer = agentDetail(
      orgAgent({
        tools: [{ id: "workspace_info", name: "Workspace info", risk: "low", requires_approval: false }],
      }),
    );
    vi.mocked(getAgent).mockResolvedValue(writer);
    vi.mocked(updateAgent).mockResolvedValue(writer);
    renderForm("/agents/agent-2/edit");
    // Once the catalog is in, it says the tool is no longer available.
    expect(await screen.findByText("No longer available")).toBeTruthy();
    const option = screen.getByRole<HTMLInputElement>("checkbox", { name: /Workspace info/ });
    expect(option.checked).toBe(true);
    expect(within(option.closest("label") as HTMLElement).getByText("No longer available")).toBeTruthy();
    fireEvent.click(option);
    expect(option.disabled).toBe(true); // it cannot be chosen again
    save("Save changes");
    await waitFor(() => {
      expect(updateAgent).toHaveBeenCalledWith("agent-2", { allowed_tools: [] });
    });
  });

  it("refuses to edit an agent the platform ships", async () => {
    vi.mocked(getAgent).mockResolvedValue(agentDetail(agent()));
    renderForm("/agents/agent-1/edit");
    expect(
      await screen.findByText("This agent comes with NEXA and cannot be changed. You can create your own agent instead."),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
  });

  it("says when the agent does not exist", async () => {
    vi.mocked(getAgent).mockRejectedValue(
      new ApiError(404, { code: "RESOURCE_NOT_FOUND", message: "The requested resource was not found." }, "req_4"),
    );
    renderForm("/agents/agent-9/edit");
    expect(await screen.findByRole("heading", { name: "Agent not found" })).toBeTruthy();
  });
});

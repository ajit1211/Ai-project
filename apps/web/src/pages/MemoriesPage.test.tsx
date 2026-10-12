import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import {
  approveMemory,
  clearMemories,
  createMemory,
  deleteMemory,
  getMemorySettings,
  listMemories,
  rejectMemory,
  updateMemory,
  updateMemorySettings,
} from "../api/memories";
import type { Memory, MemoryPage } from "../types/memory";
import { MemoriesPage } from "./MemoriesPage";

vi.mock("../api/memories", () => ({
  listMemories: vi.fn(),
  searchMemories: vi.fn(),
  createMemory: vi.fn(),
  getMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  approveMemory: vi.fn(),
  rejectMemory: vi.fn(),
  getMemorySettings: vi.fn(),
  updateMemorySettings: vi.fn(),
  clearMemories: vi.fn(),
}));

function memory(overrides: Partial<Memory> = {}): Memory {
  return {
    id: "m1",
    scope: "user",
    kind: "preference",
    key: "preferred_language",
    content: "Prefers Python for scripts",
    value: null,
    source_type: "explicit",
    confidence: 0.9,
    importance: 3,
    status: "active",
    owner_id: "u1",
    team_id: null,
    conversation_id: "c1",
    source_message_id: "msg1",
    superseded_by_id: null,
    created_at: "2026-10-01T08:00:00+00:00",
    updated_at: "2026-10-01T08:00:00+00:00",
    last_accessed_at: null,
    expires_at: null,
    editable: true,
    ...overrides,
  };
}

const python = memory();
const candidate = memory({
  id: "m2",
  kind: "fact",
  key: "",
  content: "Possibly works nights",
  source_type: "inferred",
  confidence: 0.5,
  status: "candidate",
  conversation_id: null,
});
const shared = memory({
  id: "m3",
  scope: "organization",
  kind: "decision",
  content: "Reports are in INR",
  source_type: "manual",
  confidence: 1,
  editable: false,
  conversation_id: null,
});

function page(items: Memory[]): MemoryPage {
  return { data: items, meta: { next_cursor: null, has_more: false } };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <MemoriesPage />
    </MemoryRouter>,
  );
}

const list = () => screen.getByRole("list");

describe("MemoriesPage", () => {
  beforeEach(() => {
    vi.mocked(listMemories).mockResolvedValue(page([python, candidate, shared]));
    vi.mocked(getMemorySettings).mockResolvedValue({
      extraction_enabled: true,
      auto_save: true,
      retention_days: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    vi.restoreAllMocks();
  });

  it("lists memories with their scope, source and origin, asking for active ones first", async () => {
    renderPage();
    const items = await within(await screen.findByRole("list")).findAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(listMemories).toHaveBeenCalledWith(
      { scope: undefined, kind: undefined, status: "active", q: undefined, limit: 30 },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );
    const first = items[0] as HTMLElement;
    expect(within(first).getByText("Prefers Python for scripts")).toBeTruthy();
    expect(within(first).getByText("Private")).toBeTruthy();
    expect(within(first).getByText("You said so")).toBeTruthy();
    expect(within(first).getByRole("link", { name: "From a conversation" }).getAttribute("href")).toBe(
      "/chat/c1",
    );
    // A shared memory the caller cannot change has no actions.
    const third = items[2] as HTMLElement;
    expect(within(third).getByText("Organization")).toBeTruthy();
    expect(within(third).queryByRole("button")).toBeNull();
  });

  it("filters by status and searches", async () => {
    renderPage();
    await screen.findByRole("list");
    vi.mocked(listMemories).mockResolvedValue(page([candidate]));
    fireEvent.change(screen.getByLabelText("Filter by status"), { target: { value: "candidate" } });
    await waitFor(() => {
      expect(listMemories).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "candidate" }),
        expect.anything(),
      );
    });
    fireEvent.change(screen.getByLabelText("Search memories"), { target: { value: "nights" } });
    await waitFor(() => {
      expect(listMemories).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "candidate", q: "nights" }),
        expect.anything(),
      );
    });
  });

  it("approves and rejects candidates", async () => {
    vi.mocked(listMemories).mockResolvedValue(page([candidate, memory({ id: "m4", status: "candidate", content: "Possibly likes Rust" })]));
    vi.mocked(approveMemory).mockResolvedValue({ ...candidate, status: "active" });
    vi.mocked(rejectMemory).mockResolvedValue({ ...candidate, id: "m4", content: "Possibly likes Rust", status: "rejected" });
    renderPage();
    fireEvent.change(screen.getByLabelText("Filter by status"), { target: { value: "candidate" } });
    await screen.findByRole("button", { name: "Approve “Possibly works nights”" });

    fireEvent.click(screen.getByRole("button", { name: "Approve “Possibly works nights”" }));
    await waitFor(() => {
      expect(approveMemory).toHaveBeenCalledWith("m2");
    });
    // Active now, so it leaves the list of candidates.
    await waitFor(() => {
      expect(screen.queryByText("Possibly works nights")).toBeNull();
    });
    fireEvent.click(screen.getByRole("button", { name: "Reject “Possibly likes Rust”" }));
    await waitFor(() => {
      expect(rejectMemory).toHaveBeenCalledWith("m4");
    });
    expect(document.querySelector('[aria-live="polite"]')?.textContent).toBe("Memory rejected.");
  });

  it("edits a memory in place", async () => {
    vi.mocked(updateMemory).mockResolvedValue({ ...python, content: "Prefers Python", source_type: "manual" });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Edit “Prefers Python for scripts”" }));
    const field = screen.getByLabelText("Memory");
    fireEvent.change(field, { target: { value: "Prefers Python" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(updateMemory).toHaveBeenCalledWith("m1", { content: "Prefers Python" });
    });
    expect(await within(list()).findByText("Prefers Python")).toBeTruthy();
    expect(screen.queryByLabelText("Memory")).toBeNull();
  });

  it("deletes only after confirmation", async () => {
    vi.mocked(deleteMemory).mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();
    const button = await screen.findByRole("button", { name: "Delete “Prefers Python for scripts”" });
    fireEvent.click(button);
    expect(deleteMemory).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => {
      expect(deleteMemory).toHaveBeenCalledWith("m1");
    });
    await waitFor(() => {
      expect(screen.queryByText("Prefers Python for scripts")).toBeNull();
    });
  });

  it("adds a memory by hand", async () => {
    vi.mocked(createMemory).mockResolvedValue(memory({ id: "m9", content: "Likes short answers", source_type: "manual", confidence: 1 }));
    renderPage();
    await screen.findByRole("list");
    fireEvent.change(screen.getByLabelText("What should NEXA remember?"), { target: { value: "Likes short answers" } });
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "preference" } });
    fireEvent.click(screen.getByRole("button", { name: "Save memory" }));
    await waitFor(() => {
      expect(createMemory).toHaveBeenCalledWith({ content: "Likes short answers", kind: "preference", scope: "user" });
    });
    expect(await within(list()).findByText("Likes short answers")).toBeTruthy();
    expect(screen.getByLabelText<HTMLTextAreaElement>("What should NEXA remember?").value).toBe("");
  });

  it("changes the automatic memory settings and clears memories after confirmation", async () => {
    vi.mocked(updateMemorySettings).mockResolvedValue({ extraction_enabled: false, auto_save: true, retention_days: null });
    vi.mocked(clearMemories).mockResolvedValue(3);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    const toggle = await screen.findByLabelText("Remember things from my conversations");
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(updateMemorySettings).toHaveBeenCalledWith({ extraction_enabled: false });
    });
    expect((await screen.findByLabelText<HTMLInputElement>("Save what I state myself without asking")).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Clear my memories" }));
    await waitFor(() => {
      expect(clearMemories).toHaveBeenCalledWith("all");
    });
    await waitFor(() => {
      expect(listMemories).toHaveBeenCalledTimes(2); // reloaded afterwards
    });
  });

  it("shows a dismissible error when the server refuses", async () => {
    vi.mocked(createMemory).mockRejectedValue(
      new ApiError(422, { code: "VALIDATION_FAILED", message: "This looks like a password, key or token. NEXA never stores those." }, "req_1"),
    );
    renderPage();
    await screen.findByRole("list");
    fireEvent.change(screen.getByLabelText("What should NEXA remember?"), { target: { value: "password: hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save memory" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("NEXA never stores those");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

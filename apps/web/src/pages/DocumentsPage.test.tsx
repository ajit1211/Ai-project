import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, type InitialEntry } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import {
  deleteDocument,
  getDocument,
  getDocumentStatus,
  getIndexStatus,
  listDocuments,
  retryDocument,
  uploadDocument,
  type UploadControl,
} from "../api/documents";
import { AuthContext } from "../auth/AuthContext";
import {
  documentIndex,
  documentPage,
  documentRecord,
  NOT_EMBEDDED,
  NO_INDEX,
  queuedRecord,
  semanticIndex,
  semanticUnavailable,
  signedIn,
} from "../test/documentFixtures";
import type { DocumentRecord, DocumentStatus } from "../types/document";
import { DocumentsPage } from "./DocumentsPage";

vi.mock("../api/documents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/documents")>();
  return {
    ...actual,
    listDocuments: vi.fn(),
    getDocument: vi.fn(),
    getDocumentStatus: vi.fn(),
    getIndexStatus: vi.fn(),
    getDocumentContent: vi.fn(),
    uploadDocument: vi.fn(),
    updateDocument: vi.fn(),
    deleteDocument: vi.fn(),
    retryDocument: vi.fn(),
    requestReindex: vi.fn(),
    downloadDocument: vi.fn(),
  };
});

const report = documentRecord();
const notes = documentRecord({
  id: "d2",
  filename: "notes.txt",
  title: "Meeting notes",
  format: "txt",
  media_type: "text/plain",
  size_bytes: 1536,
  owner: { id: "u2", email: "bob@example.com" },
  status: "failed",
  error_code: "corrupted",
  error_message: "The file could not be read.",
  has_text: false,
  index: NO_INDEX,
  can_retry: true,
  can_reindex: false,
});

function renderPage(entry: InitialEntry = "/documents") {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/documents/:id" element={<p>Document page</p>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const documentList = () => screen.getByRole("list", { name: "Documents" });
const uploadList = () => screen.getByRole("list", { name: "Uploads" });
const liveRegion = () => document.querySelector('[aria-live="polite"]')?.textContent ?? "";
const fileInput = () => screen.getByLabelText<HTMLInputElement>("Choose files to upload");

function itemFor(name: string): HTMLElement {
  const link = within(documentList()).getByRole("link", { name });
  const item = link.closest("li");
  if (!item) throw new Error(`no list item for ${name}`);
  return item;
}

/** Let resolved mocks and the renders they cause settle while fake timers are on. */
async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("DocumentsPage", () => {
  beforeEach(() => {
    vi.mocked(listDocuments).mockResolvedValue(documentPage([report, notes]));
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.resetAllMocks();
  });

  it("lists documents with their format, name, size, owner, status and upload date", async () => {
    renderPage();
    const items = await within(await screen.findByRole("list", { name: "Documents" })).findAllByRole(
      "listitem",
    );
    expect(items).toHaveLength(2);
    expect(listDocuments).toHaveBeenCalledWith(
      { status: undefined, format: undefined, owner: undefined, searchable: undefined, q: undefined, limit: 30 },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );

    const first = itemFor("q4-report.pdf");
    expect(within(first).getByRole("link", { name: "q4-report.pdf" }).getAttribute("href")).toBe("/documents/d1");
    expect(within(first).getByText("PDF")).toBeTruthy();
    expect(within(first).getByText("2.4 MB")).toBeTruthy();
    expect(within(first).getByText("You")).toBeTruthy();
    expect(within(first).getByText("Ready")).toBeTruthy();
    expect(within(first).getByText("Searchable")).toBeTruthy();
    expect(within(first).getByText(/Uploaded/).querySelector("time")?.getAttribute("dateTime")).toBe(
      report.created_at,
    );
    expect(within(first).queryByRole("button", { name: /Retry/ })).toBeNull();

    // A title is shown first, with the filename under it; a failure says why.
    const second = itemFor("Meeting notes");
    expect(within(second).getByText("notes.txt")).toBeTruthy();
    expect(within(second).getByText("1.5 KB")).toBeTruthy();
    expect(within(second).getByText("bob@example.com")).toBeTruthy();
    expect(within(second).getByText("Failed")).toBeTruthy();
    expect(within(second).getByText("The file could not be read.")).toBeTruthy();
    expect(within(second).queryByText("Searchable")).toBeNull();
    expect(within(second).getByRole("button", { name: "Retry processing “Meeting notes”" })).toBeTruthy();
  });

  it("shows an empty library, and an empty search apart from it", async () => {
    vi.mocked(listDocuments).mockResolvedValue(documentPage([]));
    renderPage();
    expect(await screen.findByText("No documents yet.")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Filter by status"), { target: { value: "failed" } });
    expect(await screen.findByText("No documents match this search and these filters.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear search and filters" }));
    expect(await screen.findByText("No documents yet.")).toBeTruthy();
    expect(screen.getByLabelText<HTMLSelectElement>("Filter by status").value).toBe("");
  });

  it("filters by status, format, search index and owner, and searches once typing pauses", async () => {
    renderPage();
    await screen.findByRole("list", { name: "Documents" });
    fireEvent.change(screen.getByLabelText("Filter by status"), { target: { value: "completed" } });
    fireEvent.change(screen.getByLabelText("Filter by format"), { target: { value: "pdf" } });
    fireEvent.change(screen.getByLabelText("Filter by search index"), { target: { value: "true" } });
    fireEvent.click(screen.getByLabelText("Only mine"));
    await waitFor(() => {
      expect(listDocuments).toHaveBeenLastCalledWith(
        { status: "completed", format: "pdf", owner: "me", searchable: true, q: undefined, limit: 30 },
        expect.anything(),
      );
    });
    const before = vi.mocked(listDocuments).mock.calls.length;
    fireEvent.change(screen.getByLabelText("Search documents by file name"), { target: { value: " q4 " } });
    expect(listDocuments).toHaveBeenCalledTimes(before); // not on every keystroke
    await waitFor(() => {
      expect(listDocuments).toHaveBeenLastCalledWith(expect.objectContaining({ q: "q4" }), expect.anything());
    });
  });

  it("loads the next page with the cursor", async () => {
    const older = documentRecord({ id: "d0", filename: "older.pdf" });
    vi.mocked(listDocuments)
      .mockResolvedValueOnce(documentPage([report], { next_cursor: "c2", has_more: true }))
      .mockResolvedValueOnce(documentPage([older]));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await within(documentList()).findByRole("link", { name: "older.pdf" })).toBeTruthy();
    expect(listDocuments).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: "c2", limit: 30 }));
    expect(within(documentList()).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("shows every status as its badge", async () => {
    const expected: [DocumentStatus, string, string][] = [
      ["uploaded", "Uploaded", "badge-waiting"],
      ["queued", "Queued", "badge-waiting"],
      ["processing", "Processing", "badge-waiting"],
      ["completed", "Ready", "badge-up"],
      ["failed", "Failed", "badge-down"],
      ["needs_ocr", "Needs OCR", "badge-down"],
      ["unsupported", "Unsupported", "badge-down"],
    ];
    vi.mocked(listDocuments).mockResolvedValue(
      documentPage(
        expected.map(([status]) => documentRecord({ id: status, filename: `${status}.pdf`, status })),
      ),
    );
    renderPage();
    await screen.findByRole("list", { name: "Documents" });
    for (const [status, label, tone] of expected) {
      const badge = within(itemFor(`${status}.pdf`)).getByText(label, { selector: ".badge" });
      expect(badge.classList.contains(tone)).toBe(true);
    }
  });

  it("refuses an unsupported file and one over the size limit before sending anything", async () => {
    vi.mocked(listDocuments).mockResolvedValue(
      documentPage([report], { limits: { max_upload_bytes: 1024, accepted_extensions: [".pdf", ".txt"] } }),
    );
    renderPage();
    await screen.findByRole("list", { name: "Documents" });
    expect(fileInput().accept).toBe(".pdf,.txt");
    expect(screen.getByText(".pdf or .txt, up to 1 KB each.")).toBeTruthy();

    fireEvent.change(fileInput(), {
      target: {
        files: [
          new File(["MZ"], "setup.exe", { type: "application/octet-stream" }),
          new File([new Uint8Array(2048)], "big.pdf", { type: "application/pdf" }),
        ],
      },
    });

    const rows = within(uploadList()).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByText("This file type is not supported. Upload a .pdf or .txt file.")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText("The file is 2 KB; the limit is 1 KB.")).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText("Not uploaded")).toBeTruthy();
    expect(uploadDocument).not.toHaveBeenCalled();
    expect(liveRegion()).toContain("setup.exe cannot be uploaded");
    expect(liveRegion()).toContain("big.pdf cannot be uploaded");

    fireEvent.click(screen.getByRole("button", { name: "Clear finished" }));
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
  });

  it("uploads a chosen file with progress and puts it on top of the list", async () => {
    let finish: (record: DocumentRecord) => void = () => undefined;
    let progress: UploadControl["onProgress"];
    vi.mocked(uploadDocument).mockImplementation((_file, _fields, control) => {
      progress = control?.onProgress;
      return new Promise((resolve) => {
        finish = resolve;
      });
    });
    renderPage();
    await screen.findByRole("list", { name: "Documents" });

    fireEvent.change(fileInput(), { target: { files: [new File(["hello"], "plan.md")] } });
    expect(uploadDocument).toHaveBeenCalledWith(expect.any(File), {}, expect.objectContaining({ signal: expect.any(AbortSignal) as AbortSignal }));
    expect(liveRegion()).toBe("Uploading 1 file.");
    act(() => {
      progress?.(40, 100);
    });
    const bar = screen.getByRole("progressbar", { name: "Uploading plan.md" });
    expect((bar as HTMLProgressElement).value).toBe(40);
    expect(within(uploadList()).getByText("40%")).toBeTruthy();

    await act(async () => {
      finish(queuedRecord({ id: "d9", filename: "plan.md", format: "md" }));
      await Promise.resolve();
    });
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(within(uploadList()).getByText("Uploaded, processing has started")).toBeTruthy();
    expect(within(uploadList()).getByRole("link", { name: "Open plan.md" }).getAttribute("href")).toBe("/documents/d9");
    const first = within(documentList()).getAllByRole("listitem")[0] as HTMLElement;
    expect(within(first).getByRole("link", { name: "plan.md" })).toBeTruthy();
    expect(within(first).getByText("Queued")).toBeTruthy();
    expect(liveRegion()).toBe("plan.md uploaded. Processing has started.");
  });

  it("uploads dropped files, and the drop zone also opens the picker", async () => {
    vi.mocked(uploadDocument).mockResolvedValue(queuedRecord({ id: "d9", filename: "data.csv", format: "csv" }));
    renderPage();
    await screen.findByRole("list", { name: "Documents" });
    const zone = screen.getByRole("button", { name: "Drop files here, or choose files" });

    const picker = vi.spyOn(fileInput(), "click").mockImplementation(() => undefined);
    fireEvent.click(zone);
    expect(picker).toHaveBeenCalledOnce();

    fireEvent.dragEnter(zone, { dataTransfer: { types: ["Files"], files: [] } });
    expect(zone.classList.contains("dragging")).toBe(true);
    fireEvent.drop(zone, { dataTransfer: { types: ["Files"], files: [new File(["a,b"], "data.csv")] } });
    expect(zone.classList.contains("dragging")).toBe(false);
    await waitFor(() => {
      expect(uploadDocument).toHaveBeenCalledOnce();
    });
    expect(await within(documentList()).findByRole("link", { name: "data.csv" })).toBeTruthy();
  });

  it("shows an upload the server refused with its reason", async () => {
    vi.mocked(uploadDocument).mockRejectedValue(
      new ApiError(402, { code: "QUOTA_EXCEEDED", message: "The organization's document storage is full." }, "req_1"),
    );
    renderPage();
    await screen.findByRole("list", { name: "Documents" });
    fireEvent.change(fileInput(), { target: { files: [new File(["x"], "a.pdf")] } });
    expect(await within(uploadList()).findByText("The organization's document storage is full.")).toBeTruthy();
    expect(within(uploadList()).getByText("Failed")).toBeTruthy();
    expect(liveRegion()).toBe("a.pdf was not uploaded. The organization's document storage is full.");
  });

  it("retries a failed document in place", async () => {
    vi.mocked(retryDocument).mockResolvedValue({ ...notes, status: "queued", can_retry: false, error_message: null });
    renderPage();
    await screen.findByRole("list", { name: "Documents" });
    fireEvent.click(screen.getByRole("button", { name: "Retry processing “Meeting notes”" }));
    await waitFor(() => {
      expect(retryDocument).toHaveBeenCalledWith("d2");
    });
    const item = itemFor("Meeting notes");
    expect(await within(item).findByText("Queued")).toBeTruthy();
    expect(within(item).queryByRole("button", { name: /Retry/ })).toBeNull();
    expect(document.activeElement).toBe(within(item).getByRole("link", { name: "Meeting notes" }));
    expect(liveRegion()).toBe("Processing Meeting notes again.");
  });

  it("deletes only after an inline confirmation, then moves focus to the next document", async () => {
    vi.mocked(deleteDocument).mockResolvedValue(undefined);
    renderPage();
    await screen.findByRole("list", { name: "Documents" });

    const remove = screen.getByRole("button", { name: "Delete “q4-report.pdf”" });
    fireEvent.click(remove);
    const question = screen.getByRole("group", { name: "Delete “q4-report.pdf”? This cannot be undone." });
    expect(deleteDocument).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(within(question).getByRole("button", { name: "Cancel" }));

    // Cancel (or Escape) closes the question and returns focus to Delete.
    fireEvent.click(within(question).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("group")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Delete “q4-report.pdf”" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete “q4-report.pdf”" }));
    fireEvent.keyDown(screen.getByRole("group"), { key: "Escape" });
    expect(screen.queryByRole("group")).toBeNull();
    expect(deleteDocument).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Delete “q4-report.pdf”" }));
    fireEvent.click(within(screen.getByRole("group")).getByRole("button", { name: "Delete" }));
    await waitFor(() => {
      expect(deleteDocument).toHaveBeenCalledWith("d1");
    });
    await waitFor(() => {
      expect(within(documentList()).queryByRole("link", { name: "q4-report.pdf" })).toBeNull();
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(within(documentList()).getByRole("link", { name: "Meeting notes" }));
    });
    expect(liveRegion()).toBe("q4-report.pdf deleted.");
  });

  it("shows fixed wording, not the server's, for an unexpected failure", async () => {
    vi.mocked(listDocuments)
      .mockRejectedValueOnce(
        new ApiError(500, { code: "INTERNAL_ERROR", message: "KeyError: 'organization' at views.py:88" }, "req_7"),
      )
      .mockResolvedValueOnce(documentPage([report]));
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Something went wrong on NEXA's side. Try again in a moment. (ref req_7)");
    expect(screen.getByText("The documents could not be loaded.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await within(await screen.findByRole("list", { name: "Documents" })).findByText("q4-report.pdf")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("confirms a deletion made on the document's own page", async () => {
    renderPage({ pathname: "/documents", state: { deleted: "Q4 report" } });
    expect((await screen.findByRole("status")).textContent).toBe("“Q4 report” was deleted.");
  });

  it("polls documents in progress, through extraction and indexing, then stops", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const queued = queuedRecord({ id: "d3", filename: "board.pdf" });
    vi.mocked(listDocuments).mockResolvedValue(documentPage([queued, report]));
    vi.mocked(getDocumentStatus)
      .mockResolvedValueOnce({
        id: "d3",
        status: "processing",
        error_code: null,
        error_message: null,
        attempt_count: 1,
        processing_started_at: "2026-10-12T08:00:05+00:00",
        processed_at: null,
        updated_at: "2026-10-12T08:00:05+00:00",
      })
      .mockResolvedValueOnce({
        id: "d3",
        status: "completed",
        error_code: null,
        error_message: null,
        attempt_count: 1,
        processing_started_at: "2026-10-12T08:00:05+00:00",
        processed_at: "2026-10-12T08:00:40+00:00",
        updated_at: "2026-10-12T08:00:40+00:00",
      });
    const pending = documentIndex({ status: "pending", searchable: false, chunk_count: 0, started_at: null, finished_at: null, indexed_at: null });
    vi.mocked(getDocument).mockResolvedValue(documentRecord({ id: "d3", filename: "board.pdf", index: pending }));
    vi.mocked(getIndexStatus).mockResolvedValue(documentIndex());

    renderPage();
    await flush();
    const item = () => itemFor("board.pdf");
    expect(within(item()).getByText("Queued")).toBeTruthy();
    expect(getDocumentStatus).not.toHaveBeenCalled();

    await flush(2_500);
    expect(getDocumentStatus).toHaveBeenCalledTimes(1);
    expect(getDocumentStatus).toHaveBeenCalledWith("d3", { signal: expect.any(AbortSignal) as AbortSignal });
    expect(within(item()).getByText("Processing")).toBeTruthy();

    // Finished: the whole document is fetched again for its counts and index.
    await flush(2_500);
    expect(getDocument).toHaveBeenCalledWith("d3", { signal: expect.any(AbortSignal) as AbortSignal });
    expect(within(item()).getByText("Ready")).toBeTruthy();
    expect(within(item()).queryByText("Searchable")).toBeNull();
    expect(liveRegion()).toBe("board.pdf is ready.");

    // Its index is being built now: index-status is polled until it is searchable.
    await flush(2_500);
    expect(getIndexStatus).toHaveBeenCalledWith("d3", { signal: expect.any(AbortSignal) as AbortSignal });
    expect(within(item()).getByText("Searchable")).toBeTruthy();
    expect(liveRegion()).toBe("board.pdf is now searchable.");

    await flush(20_000);
    expect(getDocumentStatus).toHaveBeenCalledTimes(2);
    expect(getIndexStatus).toHaveBeenCalledTimes(1);
  });

  it("shows where each document's semantic index stands, leaving out what says nothing", async () => {
    const embedding = documentRecord({
      id: "d4",
      filename: "plan.md",
      format: "md",
      index: documentIndex({ semantic: semanticIndex({ status: "embedding", ready: false, ready_at: null }) }),
    });
    const failedEmbedding = documentRecord({
      id: "d5",
      filename: "slides.pdf",
      index: documentIndex({
        semantic: semanticIndex({ status: "failed", ready: false, error_message: "Embedding took too long." }),
      }),
    });
    const notEmbedded = documentRecord({ id: "d6", filename: "old.pdf", index: documentIndex({ semantic: NOT_EMBEDDED }) });
    vi.mocked(listDocuments).mockResolvedValue(documentPage([report, embedding, failedEmbedding, notEmbedded, notes]));
    vi.mocked(getIndexStatus).mockResolvedValue(documentIndex());
    renderPage();
    await screen.findByRole("list", { name: "Documents" });

    const expected: [string, string, string][] = [
      ["q4-report.pdf", "Semantic search ready", "badge-up"],
      ["plan.md", "Embedding…", "badge-waiting"],
      ["slides.pdf", "Embedding failed", "badge-down"],
      ["old.pdf", "Not embedded", "badge-neutral"],
    ];
    for (const [name, label, tone] of expected) {
      expect(within(itemFor(name)).getByText(label, { selector: ".badge" }).classList.contains(tone)).toBe(true);
    }
    // Not extracted, so nothing to embed yet: no semantic badge at all.
    expect(itemFor("Meeting notes").querySelectorAll(".badge")).toHaveLength(1);
    expect(screen.queryByText(/documents are found by their keywords/)).toBeNull();
  });

  it("says once, not on every document, that semantic search does not run here", async () => {
    const unavailable = documentIndex({ semantic: semanticUnavailable("disabled") });
    vi.mocked(listDocuments).mockResolvedValue(
      documentPage([
        documentRecord({ index: unavailable }),
        documentRecord({ id: "d2", filename: "notes.txt", index: unavailable }),
      ]),
    );
    renderPage();
    await screen.findByRole("list", { name: "Documents" });
    expect(
      screen.getAllByText("Semantic search is turned off here; documents are found by their keywords."),
    ).toHaveLength(1);
    expect(screen.queryByText("Semantic search unavailable")).toBeNull();
    expect(within(itemFor("notes.txt")).getByText("Searchable")).toBeTruthy();
  });

  it("polls a document being embedded until it can be found by meaning, then stops", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const stale = documentRecord({
      id: "d4",
      filename: "plan.md",
      index: documentIndex({ semantic: semanticIndex({ status: "stale", ready: false }) }),
    });
    vi.mocked(listDocuments).mockResolvedValue(documentPage([stale, notes]));
    vi.mocked(getIndexStatus)
      .mockResolvedValueOnce(documentIndex({ semantic: semanticIndex({ status: "embedding", ready: false }) }))
      .mockResolvedValue(documentIndex());
    renderPage();
    await flush();
    const item = () => itemFor("plan.md");
    expect(within(item()).getByText("Updating embeddings", { selector: ".badge" })).toBeTruthy();
    expect(getIndexStatus).not.toHaveBeenCalled();

    await flush(2_500);
    expect(getIndexStatus).toHaveBeenCalledWith("d4", { signal: expect.any(AbortSignal) as AbortSignal });
    expect(within(item()).getByText("Embedding…", { selector: ".badge" })).toBeTruthy();

    await flush(2_500);
    expect(within(item()).getByText("Semantic search ready", { selector: ".badge" })).toBeTruthy();
    expect(liveRegion()).toBe("plan.md can now be found by meaning.");

    await flush(20_000);
    expect(getIndexStatus).toHaveBeenCalledTimes(2);
    expect(getDocumentStatus).not.toHaveBeenCalled();
  });

  it("polls a few documents at a time, and stops when the page goes away", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const many = ["a", "b", "c", "d", "e"].map((id) => queuedRecord({ id, filename: `${id}.pdf` }));
    vi.mocked(listDocuments).mockResolvedValue(documentPage(many));
    vi.mocked(getDocumentStatus).mockImplementation((id) =>
      Promise.resolve({
        id,
        status: "queued",
        error_code: null,
        error_message: null,
        attempt_count: 1,
        processing_started_at: null,
        processed_at: null,
        updated_at: "2026-10-12T08:00:00+00:00",
      }),
    );
    const { unmount } = renderPage();
    await flush();
    await flush(2_500);
    expect(vi.mocked(getDocumentStatus).mock.calls.map(([id]) => id)).toEqual(["a", "b", "c"]);
    await flush(2_500);
    expect(vi.mocked(getDocumentStatus).mock.calls.map(([id]) => id)).toEqual(["a", "b", "c", "d", "e", "a"]);

    unmount();
    await flush(10_000);
    expect(getDocumentStatus).toHaveBeenCalledTimes(6);
  });
});

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import {
  deleteDocument,
  downloadDocument,
  getDocument,
  getDocumentContent,
  getIndexStatus,
  requestReindex,
  retryDocument,
  updateDocument,
} from "../api/documents";
import { AuthContext } from "../auth/AuthContext";
import {
  documentIndex,
  documentRecord,
  ingestion,
  NO_INDEX,
  queuedRecord,
  signedIn,
} from "../test/documentFixtures";
import type { DocumentContent } from "../types/document";
import { DocumentDetailPage } from "./DocumentDetailPage";

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

const report = documentRecord({
  title: "Q4 report",
  warnings: ["pages_without_text:3", "encoding_fallback:cp1252", "mystery_warning"],
  index: documentIndex({ warnings: ["chunks_truncated"] }),
  ingestions: [
    ingestion({ id: "i2", attempt: 2 }),
    ingestion({
      id: "i1",
      attempt: 1,
      status: "failed",
      error_code: "timeout",
      error_message: "Processing took too long and was stopped.",
      page_count: null,
      word_count: 0,
    }),
  ],
});

function slice(content: string, overrides: Partial<DocumentContent> = {}): DocumentContent {
  return {
    content,
    offset: 0,
    limit: 20_000,
    total_chars: content.length,
    has_more: false,
    next_offset: null,
    pages: [{ page: 1, char_start: 0, char_end: content.length }],
    ...overrides,
  };
}

/** Where the deletion sent us, and what it said. */
function Library() {
  const location = useLocation();
  const state = location.state as { deleted?: string } | null;
  return <p>Library page{state?.deleted ? `: ${state.deleted} deleted` : ""}</p>;
}

function renderPage(path = "/documents/d1") {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/documents" element={<Library />} />
          <Route path="/documents/:id" element={<DocumentDetailPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const textBlock = () => screen.getByRole("region", { name: "Document text" });
const liveRegion = () => document.querySelector('[aria-live="polite"]')?.textContent ?? "";
const signal = { signal: expect.any(AbortSignal) as AbortSignal };

describe("DocumentDetailPage", () => {
  beforeEach(() => {
    vi.mocked(getDocument).mockResolvedValue(report);
    vi.mocked(getDocumentContent).mockResolvedValue(slice("Revenue grew 12% in Q4."));
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.resetAllMocks();
  });

  it("shows the document's details, its warnings in words and every processing attempt", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: "Q4 report" })).toBeTruthy();
    expect(getDocument).toHaveBeenCalledWith("d1", signal);

    const details = screen.getByRole("complementary", { name: "Document details" });
    expect(within(details).getAllByText("q4-report.pdf")).toHaveLength(1);
    expect(within(details).getByText("PDF")).toBeTruthy();
    expect(within(details).getByText("2.4 MB")).toBeTruthy();
    expect(within(details).getByText("abababababab…").getAttribute("title")).toBe(report.sha256);
    expect(within(details).getByText("ada@example.com")).toBeTruthy();
    expect(within(details).getByText("Private: only the owner")).toBeTruthy();
    expect(within(details).getByText("3,400")).toBeTruthy();
    expect(within(details).getByText("21,000")).toBeTruthy();

    expect(screen.getByText("3 pages have no text; they may be scanned images.")).toBeTruthy();
    expect(
      screen.getByText(
        "The file is not UTF-8, so it was read as Windows-1252, a best guess: some characters may be wrong.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("mystery_warning")).toBeTruthy(); // unknown codes as sent
    expect(
      screen.getByText("The document is longer than NEXA indexes; only its beginning can be found by search."),
    ).toBeTruthy();

    const history = screen.getByRole("table");
    const rows = within(history).getAllByRole("row");
    expect(rows).toHaveLength(3); // the header and two attempts, newest first
    expect(within(rows[1] as HTMLElement).getByText("3,400 words, 12 pages")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText("2.4 s")).toBeTruthy();
    expect(within(rows[2] as HTMLElement).getByText("Failed")).toBeTruthy();
    expect(within(rows[2] as HTMLElement).getByText("Processing took too long and was stopped.")).toBeTruthy();
  });

  it("renders the extracted text as plain text, never as HTML", async () => {
    const hostile = '<script>window.hacked = true</script><img src=x onerror="alert(1)"><b>bold</b> & more';
    vi.mocked(getDocumentContent).mockResolvedValue(slice(hostile));
    renderPage();
    await screen.findByRole("region", { name: "Document text" });
    expect(getDocumentContent).toHaveBeenCalledWith("d1", {}, signal);
    expect(textBlock().textContent).toBe(hostile);
    expect(screen.getByText(hostile)).toBe(textBlock());
    expect(textBlock().querySelector("script, img, b")).toBeNull();
    expect(screen.getByText(`${String(hostile.length)} characters`)).toBeTruthy();
  });

  it("appends the next slice of text from next_offset", async () => {
    vi.mocked(getDocumentContent)
      .mockResolvedValueOnce(slice("Hello", { total_chars: 11, has_more: true, next_offset: 5 }))
      .mockResolvedValueOnce(slice(" world", { offset: 5, total_chars: 11 }));
    renderPage();
    await screen.findByRole("region", { name: "Document text" });
    expect(screen.getByText("Characters 1–5 of 11")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load more text" }));
    await waitFor(() => {
      expect(textBlock().textContent).toBe("Hello world");
    });
    expect(getDocumentContent).toHaveBeenLastCalledWith("d1", { offset: 5 });
    expect(screen.queryByRole("button", { name: "Load more text" })).toBeNull();
  });

  it("opens the page named by ?page= and goes back to the whole document", async () => {
    vi.mocked(getDocumentContent).mockImplementation((_id, request) =>
      Promise.resolve(
        request && "page" in request
          ? slice(`Text of page ${String(request.page)}`, {
              page: request.page,
              offset: 900,
              pages: [{ page: request.page, char_start: 900, char_end: 914 }],
            })
          : slice("The whole document"),
      ),
    );
    renderPage("/documents/d1?page=3");
    await screen.findByRole("region", { name: "Document text" });
    expect(getDocumentContent).toHaveBeenCalledWith("d1", { page: 3 }, signal);
    expect(textBlock().textContent).toBe("Text of page 3");
    expect(textBlock().scrollTop).toBe(0);
    expect(screen.getByText("Page 3 of 12")).toBeTruthy();
    expect(getDocumentContent).not.toHaveBeenCalledWith("d1", {}, signal);

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => {
      expect(textBlock().textContent).toBe("Text of page 4");
    });
    fireEvent.change(screen.getByLabelText("Go to page"), { target: { value: "9" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    await waitFor(() => {
      expect(textBlock().textContent).toBe("Text of page 9");
    });

    fireEvent.click(screen.getByRole("button", { name: "Show whole document" }));
    await waitFor(() => {
      expect(textBlock().textContent).toBe("The whole document");
    });
    expect(getDocumentContent).toHaveBeenLastCalledWith("d1", {}, signal);
    expect(screen.queryByRole("button", { name: "Show whole document" })).toBeNull();
  });

  it("opens the text at the character named by ?offset=", async () => {
    vi.mocked(getDocumentContent).mockResolvedValue(
      slice("the cited passage", { offset: 1200, total_chars: 40_000, has_more: true, next_offset: 21_200 }),
    );
    renderPage("/documents/d1?offset=1200");
    await screen.findByRole("region", { name: "Document text" });
    expect(getDocumentContent).toHaveBeenCalledWith("d1", { offset: 1200 }, signal);
    expect(textBlock().scrollTop).toBe(0);
    expect(screen.getByText("Characters 1,201–21,200 of 40,000")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Show whole document" })).toBeTruthy();
  });

  it("says when the page asked for does not exist", async () => {
    vi.mocked(getDocumentContent).mockRejectedValue(
      new ApiError(404, { code: "RESOURCE_NOT_FOUND", message: "Not found." }, "req_1"),
    );
    renderPage("/documents/d1?page=99");
    expect((await screen.findByRole("alert")).textContent).toBe("This document has no page 99.");
  });

  it("explains a failed extraction and retries it", async () => {
    const failed = documentRecord({
      status: "failed",
      error_code: "encrypted",
      error_message: "The PDF is password protected.",
      has_text: false,
      index: NO_INDEX,
      can_retry: true,
      can_reindex: false,
      ingestions: [ingestion({ status: "failed", error_message: "The PDF is password protected." })],
    });
    vi.mocked(getDocument)
      .mockResolvedValueOnce(failed)
      .mockResolvedValue(queuedRecord({ attempt_count: 2, ingestions: [ingestion({ id: "i2", attempt: 2, status: "queued" })] }));
    vi.mocked(retryDocument).mockResolvedValue(queuedRecord({ attempt_count: 2 }));
    renderPage();

    const processing = await screen.findByRole("region", { name: "Processing" });
    expect(within(processing).getByText("Failed", { selector: ".badge" })).toBeTruthy();
    expect(within(processing).getByText("The PDF is password protected.")).toBeTruthy();
    expect(screen.getByText("No text could be extracted from this file.")).toBeTruthy();
    expect(getDocumentContent).not.toHaveBeenCalled();
    expect(within(processing).queryByRole("button", { name: "Re-index" })).toBeNull();

    fireEvent.click(within(processing).getByRole("button", { name: "Retry processing" }));
    await waitFor(() => {
      expect(retryDocument).toHaveBeenCalledWith("d1");
    });
    expect(await within(processing).findByText("Queued", { selector: ".badge" })).toBeTruthy();
    expect(within(processing).queryByRole("button", { name: "Retry processing" })).toBeNull();
    expect(screen.getByText("The text appears here once processing finishes.")).toBeTruthy();
    expect(liveRegion()).toBe("Processing has been queued again.");
    expect(document.activeElement).toBe(within(processing).getByRole("heading", { name: "Processing" }));
  });

  it("shows the search index apart from extraction, and re-indexes", async () => {
    vi.mocked(requestReindex).mockResolvedValue(
      documentIndex({ status: "pending", requested_at: "2026-10-12T09:00:00+00:00", started_at: null }),
    );
    renderPage();
    const processing = await screen.findByRole("region", { name: "Processing" });
    const rows = within(processing).getAllByRole("term").map((term) => term.parentElement as HTMLElement);
    expect(rows.map((row) => within(row).getByRole("term").textContent)).toEqual(["Text extraction", "Search index"]);
    expect(within(rows[0] as HTMLElement).getByText("Ready")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText("Indexed")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText(/^14 passages, about 5,200 tokens; indexed/)).toBeTruthy();

    fireEvent.click(within(processing).getByRole("button", { name: "Re-index" }));
    await waitFor(() => {
      expect(requestReindex).toHaveBeenCalledWith("d1");
    });
    expect(await within(rows[1] as HTMLElement).findByText("Queued")).toBeTruthy();
    expect(within(processing).getByRole("button", { name: "Indexing…" }).hasAttribute("disabled")).toBe(true);
    expect(liveRegion()).toBe("Indexing has been queued.");
  });

  it("refuses a re-index in the server's words when it conflicts", async () => {
    vi.mocked(requestReindex).mockRejectedValue(
      new ApiError(409, { code: "CONFLICT", message: "The document has no extracted text to index yet." }, "req_2"),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Re-index" }));
    expect((await screen.findByRole("alert")).textContent).toBe("The document has no extracted text to index yet.");
  });

  it("polls the index status while the index is built", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.mocked(getDocument).mockResolvedValue(
      documentRecord({ index: documentIndex({ status: "indexing", searchable: false, finished_at: null, indexed_at: null }) }),
    );
    vi.mocked(getIndexStatus).mockResolvedValue(documentIndex());
    renderPage();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const processing = screen.getByRole("region", { name: "Processing" });
    expect(within(processing).getByText("Indexing", { selector: ".badge" })).toBeTruthy();
    expect(getIndexStatus).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500);
    });
    expect(getIndexStatus).toHaveBeenCalledWith("d1", signal);
    expect(within(processing).getByText("Indexed", { selector: ".badge" })).toBeTruthy();
    expect(liveRegion()).toBe("q4-report.pdf is now searchable.");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getIndexStatus).toHaveBeenCalledTimes(1);
  });

  it("saves edited details, checking the category first", async () => {
    vi.mocked(updateDocument).mockResolvedValue({
      ...report,
      title: "Q4 board report",
      category: "board-reports",
      visibility: "organization",
      ingestions: undefined,
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit details" });
    expect(document.activeElement).toBe(within(form).getByLabelText("Title"));
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: " Q4 board report " } });
    fireEvent.change(within(form).getByLabelText("Category"), { target: { value: "Board Reports" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect((await within(form).findByRole("alert")).textContent).toBe(
      "Use lowercase letters, digits, - and _, starting with a letter or a digit.",
    );
    expect(updateDocument).not.toHaveBeenCalled();

    fireEvent.change(within(form).getByLabelText(/^Category/), { target: { value: "board-reports" } });
    fireEvent.change(within(form).getByLabelText("Visible to"), { target: { value: "organization" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(updateDocument).toHaveBeenCalledWith("d1", {
        title: "Q4 board report",
        category: "board-reports",
        visibility: "organization",
      });
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Q4 board report" })).toBeTruthy();
    expect(screen.queryByRole("form", { name: "Edit details" })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText("Everyone in the organization")).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(3); // the attempts survive the update
    expect(liveRegion()).toBe("Changes saved.");
  });

  it("shows field errors from the server next to the field", async () => {
    vi.mocked(updateDocument).mockRejectedValue(
      new ApiError(
        422,
        {
          code: "VALIDATION_FAILED",
          message: "Some fields are invalid.",
          details: [{ field: "title", code: "max_length", message: "Ensure this field has no more than 200 characters." }],
        },
        "req_3",
      ),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Ensure this field has no more than 200 characters.");
  });

  it("keeps team sharing read-only", async () => {
    vi.mocked(getDocument).mockResolvedValue(documentRecord({ visibility: "team", team_id: "7f3c2a10-0000-4000-8000-000000000000" }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit details" });
    expect(within(form).queryByRole("combobox")).toBeNull();
    expect(within(form).getByText("Team members only. Team sharing cannot be changed here.")).toBeTruthy();
    expect(screen.getByText("Team 7f3c2a10")).toBeTruthy();
  });

  it("deletes only after confirmation, then returns to the library", async () => {
    vi.mocked(deleteDocument).mockResolvedValue(undefined);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Delete “Q4 report”" }));
    const question = screen.getByRole("group", { name: /^Delete this document\?/ });
    expect(deleteDocument).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(within(question).getByRole("button", { name: "Cancel" }));
    fireEvent.click(within(question).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Library page: Q4 report deleted")).toBeTruthy();
    expect(deleteDocument).toHaveBeenCalledWith("d1");
  });

  it("downloads the original file", async () => {
    vi.mocked(downloadDocument).mockResolvedValue(undefined);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Download" }));
    await waitFor(() => {
      expect(downloadDocument).toHaveBeenCalledWith(expect.objectContaining({ id: "d1", filename: "q4-report.pdf" }));
    });
    await waitFor(() => {
      expect(liveRegion()).toBe("Downloading q4-report.pdf.");
    });
  });

  it("says when the document does not exist", async () => {
    vi.mocked(getDocument).mockRejectedValue(
      new ApiError(404, { code: "RESOURCE_NOT_FOUND", message: "The requested resource was not found." }, "req_4"),
    );
    renderPage();
    expect(await screen.findByRole("heading", { name: "Document not found" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "All documents" }).getAttribute("href")).toBe("/documents");
  });
});

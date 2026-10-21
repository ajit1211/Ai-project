import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { listMessageSources } from "../../api/conversations";
import type { ChatItem } from "../../hooks/chatThread";
import { chatItem, chatMessage, grounding, source, unavailableSource } from "../../test/chatFixtures";
import type { Grounding, Message } from "../../types/chat";
import { MessageBubble } from "./MessageBubble";

vi.mock("../../api/conversations", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/conversations")>();
  return { ...actual, listMessageSources: vi.fn() };
});

const q4 = source();
const csv = source({
  label: "S2",
  rank: 2,
  document_id: "doc-2",
  document_title: null,
  filename: "sales.csv",
  format: "csv",
  page_start: null,
  page_end: null,
  row_start: 1,
  row_end: 20,
  char_start: 1200,
  excerpt: "region,revenue\nnorth,120",
});
const consulted = source({
  label: "S3",
  rank: 3,
  cited: false,
  document_id: "doc-3",
  document_title: "Board minutes",
  section: "Finance > Outlook",
  page_start: 7,
  page_end: 8,
  excerpt: "The board expects growth to slow.",
});

function grounded(content: string, overrides: Partial<Message> = {}): Message {
  return chatMessage({ content, grounding: grounding(), sources: [q4, csv, consulted], ...overrides });
}

function renderItem(item: ChatItem) {
  return render(
    <MemoryRouter>
      <ol>
        <MessageBubble item={item} canRegenerate={false} canRetry={false} onRegenerate={vi.fn()} onRetry={vi.fn()} />
      </ol>
    </MemoryRouter>,
  );
}

const card = (label: string): HTMLElement => {
  const cards = [...document.querySelectorAll<HTMLElement>(".source-card")];
  const found = cards.find((item) => item.querySelector(".source-label")?.textContent === label);
  if (!found) throw new Error(`no card for ${label}`);
  return found;
};

describe("a reply answered from documents", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("turns the markers of its own sources into citation buttons and leaves the rest as text", () => {
    const { container } = renderItem(
      chatItem(grounded("Revenue grew [S1]. By region [S1][S2], and see [S1, S9] or [S7].\n\n`code [S1]`")),
    );
    expect(screen.getByText("From your documents")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Source S1: Q4 report, page 3" })).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: "Source S2: sales.csv, rows 1–20" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Source S1: Q4 report, page 3" })[0]?.textContent).toBe("1");

    // Labels no source has are unverified: they stay the text they were.
    const paragraph = container.querySelector(".markdown p");
    expect(paragraph?.textContent).toBe("Revenue grew 1. By region 12, and see 1[S9] or [S7].");
    expect(screen.queryByRole("button", { name: /S7|S9/ })).toBeNull();
    // Code shows markers as typed.
    const code = container.querySelector("code");
    expect(code?.textContent).toBe("code [S1]");
    expect(code?.querySelector("button")).toBeNull();
  });

  it("follows a citation to its source card, opening the folded passages when needed", async () => {
    renderItem(chatItem(grounded("Growth [S2] may slow [S3].")));
    const others = screen.getByText("Other passages consulted (1)").closest("details");
    expect(others?.open).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Source S2: sales.csv, rows 1–20" }));
    await waitFor(() => {
      expect(document.activeElement).toBe(card("S2"));
    });
    expect(card("S2").classList.contains("highlighted")).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Source S3: Board minutes, pages 7–8, Finance > Outlook" }));
    await waitFor(() => {
      expect(document.activeElement).toBe(card("S3"));
    });
    expect(others?.open).toBe(true);
  });

  it("shows cited passages as cards, with where they are and a link to them", () => {
    renderItem(chatItem(grounded("Answer [S1][S2].")));
    const sources = screen.getByRole("list", { name: "Sources" });
    expect(within(sources).getAllByRole("listitem")).toHaveLength(2);
    expect(within(card("S1")).getByText("Q4 report")).toBeTruthy();
    expect(within(card("S1")).getByText("PDF")).toBeTruthy();
    expect(within(card("S1")).getByText("p. 3")).toBeTruthy();
    expect(within(card("S1")).getByRole("link", { name: "Open document: Q4 report" }).getAttribute("href")).toBe(
      "/documents/doc-1?page=3",
    );
    // No page: the link opens the text at the passage's first character.
    expect(within(card("S2")).getByText("rows 1–20")).toBeTruthy();
    expect(within(card("S2")).getByRole("link", { name: "Open document: sales.csv" }).getAttribute("href")).toBe(
      "/documents/doc-2?offset=1200",
    );
    expect(within(card("S2")).getByText("region,revenue north,120", { normalizer: (text) => text.replace(/\s+/g, " ") })).toBeTruthy();
    expect(within(card("S3")).getByText("p. 7–8, Finance > Outlook")).toBeTruthy();
  });

  it("shows excerpts as plain text, never as HTML", () => {
    const hostile = '<script>window.hacked = true</script><img src=x onerror="alert(1)"> **not bold**';
    const { container } = renderItem(chatItem(grounded("See [S1].", { sources: [source({ excerpt: hostile })] })));
    expect(screen.getByText(hostile)).toBeTruthy();
    expect(container.querySelector("script, img, .source-excerpt strong")).toBeNull();
  });

  it("says when a source is no longer available, without a link", () => {
    renderItem(chatItem(grounded("Answer [S1] and [S4].", { sources: [q4, unavailableSource("S4")] })));
    const gone = card("S4");
    expect(within(gone).getByText("This source is no longer available.")).toBeTruthy();
    expect(within(gone).queryByRole("link")).toBeNull();
    expect(screen.getByRole("button", { name: "Source S4: no longer available" })).toBeTruthy();
  });

  it("expands a passage from the sources endpoint", async () => {
    vi.mocked(listMessageSources).mockResolvedValue([
      source({ excerpt: "Revenue grew 12% in the fourth quarter. The whole passage goes on from here." }),
    ]);
    renderItem(chatItem(grounded("Answer [S1].", { sources: [source({ excerpt_truncated: true })] })));
    fireEvent.click(within(card("S1")).getByRole("button", { name: "Expand passage" }));
    expect(await within(card("S1")).findByText(/The whole passage goes on from here\.$/)).toBeTruthy();
    expect(listMessageSources).toHaveBeenCalledWith("c1", "m4");
    expect(within(card("S1")).queryByRole("button", { name: "Expand passage" })).toBeNull();
  });

  it("warns about an answer that cites nothing, and about citations it could not verify", () => {
    renderItem(
      chatItem(
        grounded("Probably fine [S8].", {
          grounding: grounding({ outcome: "uncited", cited: 0, invalid_citations: ["S8"] }),
          sources: [source({ cited: false })],
        }),
      ),
    );
    expect(screen.getByText("This answer does not cite the selected documents; treat it as unverified.")).toBeTruthy();
    expect(screen.getByText("Some citations could not be verified and are shown as plain text.")).toBeTruthy();
    expect(screen.getByText("Probably fine [S8].")).toBeTruthy();
  });

  it("says when nothing relevant was found", () => {
    renderItem(
      chatItem(
        grounded("I could not find this in the selected documents.", {
          grounding: grounding({ outcome: "no_evidence", retrieved: 0, supplied: 0, cited: 0 }),
          sources: [],
        }),
      ),
    );
    expect(screen.getByText("No matching passages were found in the selected documents.")).toBeTruthy();
    expect(screen.queryByRole("list", { name: "Sources" })).toBeNull();
  });

  it("shows where the search stands while the reply has no text", () => {
    const pending = grounded("", { status: "pending", grounding: grounding({ outcome: "pending" }), sources: [] });
    const { rerender } = renderItem(
      chatItem(pending, { streaming: true, retrieval: { state: "searching", documents: 2 } }),
    );
    expect(screen.getByText("Searching 2 documents…")).toBeTruthy();
    rerender(
      <MemoryRouter>
        <ol>
          <MessageBubble
            item={chatItem(pending, { streaming: true, retrieval: { state: "found", searched: 2, found: 3 } })}
            canRegenerate={false}
            canRetry={false}
            onRegenerate={vi.fn()}
            onRetry={vi.fn()}
          />
        </ol>
      </MemoryRouter>,
    );
    expect(screen.getByText("Found 3 passages")).toBeTruthy();
  });

  it("says how its passages were found", () => {
    renderItem(chatItem(grounded("Answer [S1].")));
    expect(screen.getByText("Passages found by keywords and meaning.")).toBeTruthy();
    expect(screen.queryByText(/semantic search was unavailable/)).toBeNull();
  });

  it.each(["query_embedding_failed", "vector_search_failed", "semantic_disabled", "semantic_pgvector_missing"])(
    "says, without alarm, when the search fell back to keywords (%s)",
    (fallback) => {
      renderItem(
        chatItem(
          grounded("Answer [S1].", {
            grounding: grounding({ retrieval_requested: "semantic", retrieval_mode: "lexical", fallback }),
          }),
        ),
      );
      const notice = screen.getByText("Searched by keywords: semantic search was unavailable for this answer.");
      expect(notice.classList.contains("grounding-warning")).toBe(false);
      expect(screen.queryByText(fallback, { exact: false })).toBeNull();
      // The notice already says how: no second line about it.
      expect(screen.queryByText(/^Passages found by/)).toBeNull();
      expect(screen.getByRole("list", { name: "Sources" })).toBeTruthy();
    },
  );

  it("still shows a reply from before semantic search, with no notice", () => {
    const older: Grounding = grounding();
    delete older.retrieval_requested;
    delete older.retrieval_mode;
    delete older.fallback;
    renderItem(chatItem(grounded("Revenue grew [S1].", { grounding: older })));
    expect(screen.getByText("From your documents")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Source S1: Q4 report, page 3" })).toBeTruthy();
    expect(screen.queryByText(/semantic search was unavailable/)).toBeNull();
    expect(screen.queryByText(/^Passages found by/)).toBeNull();
  });

  it("leaves an ordinary reply exactly as before", () => {
    const { container } = renderItem(chatItem(chatMessage({ content: "Footnote style [S1] stays." })));
    expect(screen.queryByText("From your documents")).toBeNull();
    expect(screen.queryByRole("button", { name: /Source/ })).toBeNull();
    expect(container.querySelector(".markdown p")?.textContent).toBe("Footnote style [S1] stays.");
  });
});

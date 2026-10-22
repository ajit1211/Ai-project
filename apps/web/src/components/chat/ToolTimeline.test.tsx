import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChatItem } from "../../hooks/chatThread";
import {
  approval,
  awaitingActivity,
  chatItem,
  chatMessage,
  MEMORY_TEXT,
  toolActivity,
} from "../../test/chatFixtures";
import type { Message } from "../../types/chat";
import type { ToolActivity } from "../../types/tools";
import { MessageBubble, type MessageBubbleProps } from "./MessageBubble";

function renderItem(item: ChatItem, props: Partial<MessageBubbleProps> = {}) {
  return render(
    <MemoryRouter>
      <ol>
        <MessageBubble
          item={item}
          canRegenerate={false}
          canRetry={false}
          onRegenerate={vi.fn()}
          onRetry={vi.fn()}
          {...props}
        />
      </ol>
    </MemoryRouter>,
  );
}

function withTools(tools: ToolActivity[], overrides: Partial<Message> = {}): Message {
  return chatMessage({ content: "Here you go.", tools, ...overrides });
}

const timeline = () => screen.getByRole("list", { name: "Tool activity" });
const rows = () => within(timeline()).getAllByRole("listitem");

/** A row's text with runs of whitespace collapsed. */
const rowText = (row: HTMLElement) => row.textContent.replace(/\s+/g, " ").trim();

describe("the tool activity of a reply", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows nothing for a reply without tools", () => {
    renderItem(chatItem(chatMessage({ content: "Plain answer." })));
    expect(screen.queryByRole("list", { name: "Tool activity" })).toBeNull();
    renderItem(chatItem(chatMessage({ content: "Plain answer.", tools: [] })));
    expect(screen.queryByRole("list", { name: "Tool activity" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();
  });

  it("lists every stored call with its state, summary, duration and reason", () => {
    renderItem(
      chatItem(
        withTools([
          toolActivity(),
          toolActivity({
            invocation_id: "inv-2",
            tool_id: "current_time",
            tool_name: "Current time",
            summary: "Checking the time in Mars/Olympus",
            status: "failed",
            duration_ms: 1500,
            error_code: "unknown_timezone",
            error_message: "That time zone is not known.",
            result: null,
          }),
          toolActivity({
            invocation_id: "inv-3",
            tool_id: "delete_everything",
            tool_name: "delete_everything",
            summary: "",
            status: "rejected",
            risk: "",
            duration_ms: null,
            error_code: "unknown_tool",
            error_message: "No such tool.",
            result: null,
          }),
          toolActivity({
            invocation_id: "inv-4",
            summary: "Searching documents for “refunds”",
            tool_id: "search_documents",
            status: "timed_out",
            duration_ms: 10000,
            result: null,
          }),
          toolActivity({ invocation_id: "inv-5", status: "cancelled", duration_ms: null, result: null }),
          toolActivity({
            invocation_id: "inv-6",
            tool_id: "save_memory",
            summary: "Saving to your memories",
            status: "rejected",
            duration_ms: null,
            error_code: "declined_by_user",
            error_message: "The user declined this action.",
            result: null,
            approval: approval({ status: "rejected", decided_at: "2026-10-20T09:01:00+00:00" }),
          }),
          toolActivity({ invocation_id: "inv-7", status: "executing", duration_ms: null, result: null }),
        ]),
      ),
    );
    expect(rows().map(rowText)).toEqual([
      // The folded result is in the document, out of sight until "Show result" opens it.
      "Calculating 6*7Done3 msShow result of Calculating 6*76*7 = 42",
      "Checking the time in Mars/OlympusFailed1.5 sThat time zone is not known.",
      "delete_everythingRejectedThe model asked for a tool that does not exist.",
      "Searching documents for “refunds”Timed out10.0 sThe tool took too long and was stopped.",
      "Calculating 6*7Cancelled",
      "Saving to your memoriesDeclinedYou declined this action, so it was not carried out.",
      // The reply is over: a call that never ended did not finish.
      "Calculating 6*7Did not finish",
    ]);
    expect(timeline().querySelector(".tool-spinner")).toBeNull();
  });

  it("shows a call as running while its reply streams, until its result arrives", () => {
    const running = toolActivity({ status: "executing", duration_ms: null, result: null });
    const streaming = chatMessage({ status: "streaming", content: "", tools: [running] });
    const { rerender } = renderItem(chatItem(streaming, { streaming: true }));
    expect(rowText(rows()[0] as HTMLElement)).toBe("Calculating 6*7Running");
    expect(timeline().querySelector(".tool-spinner")).not.toBeNull();

    rerender(
      <MemoryRouter>
        <ol>
          <MessageBubble
            item={chatItem({ ...streaming, tools: [toolActivity()] }, { streaming: true })}
            canRegenerate={false}
            canRetry={false}
            onRegenerate={vi.fn()}
            onRetry={vi.fn()}
          />
        </ol>
      </MemoryRouter>,
    );
    expect(rowText(rows()[0] as HTMLElement)).toBe("Calculating 6*7Done3 msShow result of Calculating 6*76*7 = 42");
    expect(timeline().querySelector(".tool-spinner")).toBeNull();
  });

  it("folds out the calculator's result", () => {
    renderItem(chatItem(withTools([toolActivity()])));
    const details = screen.getByText("Show result").closest("details");
    expect(details?.open).toBe(false);
    fireEvent.click(screen.getByText("Show result"));
    expect(details?.open).toBe(true);
    expect(details?.querySelector(".tool-calculation")?.textContent).toBe("6*7 = 42");
  });

  it("shows search results with where they are and links to their documents", () => {
    renderItem(
      chatItem(
        withTools([
          toolActivity({
            tool_id: "search_documents",
            tool_name: "Search documents",
            summary: "Searching documents for “refund policy”",
            result: {
              query: "refund policy",
              results: [
                {
                  ref: "R1",
                  document_id: "doc-1",
                  chunk_id: "chunk-1",
                  title: "Handbook",
                  filename: "handbook.pdf",
                  section: "Billing > Refunds",
                  page_start: 3,
                  page_end: 4,
                  excerpt: "Refunds within 30 days.\nNo questions asked.",
                  excerpt_truncated: true,
                },
                {
                  ref: "R2",
                  document_id: "doc-2",
                  chunk_id: "chunk-9",
                  title: "notes.md",
                  filename: "notes.md",
                  section: null,
                  page_start: null,
                  page_end: null,
                  excerpt: "Ask finance.",
                  excerpt_truncated: false,
                },
              ],
              searched_documents: 4,
              retrieval_mode: "hybrid",
              note: null,
            },
            sources: [{ document_id: "doc-1", chunk_id: "chunk-1" }],
          }),
        ]),
      ),
    );
    expect(screen.getByText("Searched 4 documents for “refund policy”.")).toBeTruthy();
    const passages = screen.getByRole("list", { name: "Passages found" });
    const [first, second] = within(passages).getAllByRole("listitem");
    expect(within(first as HTMLElement).getByRole("link", { name: "Handbook" }).getAttribute("href")).toBe(
      "/documents/doc-1?page=3",
    );
    expect(within(first as HTMLElement).getByText("p. 3–4, Billing > Refunds")).toBeTruthy();
    expect(first?.querySelector(".tool-excerpt")?.textContent).toBe("Refunds within 30 days.\nNo questions asked.");
    expect(within(first as HTMLElement).getByText("Excerpt shortened.")).toBeTruthy();
    expect(within(second as HTMLElement).getByRole("link", { name: "notes.md" }).getAttribute("href")).toBe(
      "/documents/doc-2",
    );
  });

  it("lists documents with links, and says how many were left out or not found", () => {
    renderItem(
      chatItem(
        withTools([
          toolActivity({
            tool_id: "document_metadata",
            tool_name: "Document details",
            summary: "Looking up your documents",
            result: {
              documents: [
                {
                  id: "doc-1",
                  title: "Q4 report",
                  filename: "q4-report.pdf",
                  format: "pdf",
                  status: "completed",
                  size_bytes: 1024,
                  page_count: 12,
                  word_count: 3400,
                  created_at: "2026-10-01T08:00:00+00:00",
                  searchable: true,
                },
              ],
              more: true,
              total: 9,
              not_found: 2,
            },
          }),
        ]),
      ),
    );
    const documents = screen.getByRole("list", { name: "Documents" });
    expect(within(documents).getByRole("link", { name: "Q4 report" }).getAttribute("href")).toBe("/documents/doc-1");
    expect(within(documents).getByText("· PDF · Ready · 12 pages · 3,400 words")).toBeTruthy();
    expect(screen.getByText("Listed 1 of 9 documents.")).toBeTruthy();
    expect(screen.getByText("2 requested documents were not found or are not available to you.")).toBeTruthy();
  });

  it("shows the time, the workspace and a saved memory readably", () => {
    renderItem(
      chatItem(
        withTools([
          toolActivity({
            tool_id: "current_time",
            summary: "Checking the time",
            result: {
              utc: "2026-10-20T09:00:00Z",
              timezone: "UTC",
              unix_seconds: 1792486800,
              weekday: "Tuesday",
              local: { time: "2026-10-20T14:30:00+05:30", timezone: "Asia/Kolkata", weekday: "Tuesday" },
            },
          }),
          toolActivity({
            invocation_id: "inv-2",
            tool_id: "workspace_info",
            summary: "Looking up your workspace",
            result: {
              organization: { id: "org-1", name: "Acme" },
              role: { key: "admin", name: "Admin" },
              teams: [
                { id: "t1", name: "Finance" },
                { id: "t2", name: "Ops" },
              ],
              member_count: 12,
            },
          }),
          toolActivity({
            invocation_id: "inv-3",
            tool_id: "save_memory",
            summary: "Saving to your memories",
            result: { memory_id: "mem-1", content: MEMORY_TEXT, kind: "preference", status: "active" },
            approval: approval({ status: "approved", decided_at: "2026-10-20T09:01:00+00:00" }),
          }),
        ]),
      ),
    );
    const facts = [...document.querySelectorAll(".tool-facts div")].map((fact) => fact.textContent);
    expect(facts).toEqual([
      "UTCTuesday, 2026-10-20T09:00:00Z",
      "Asia/KolkataTuesday, 2026-10-20T14:30:00+05:30",
      "OrganizationAcme",
      "Your roleAdmin",
      "Your teamsFinance, Ops",
      "Members12",
    ]);
    expect(document.querySelector(".tool-quote")?.textContent).toBe(MEMORY_TEXT);
    expect(screen.getByRole("link", { name: "Manage your memories" }).getAttribute("href")).toBe("/memories");
    expect(screen.getByText("You approved this action.")).toBeTruthy();
  });

  it("shows any other result as JSON text, never as HTML", () => {
    const hostile = '<script>window.hacked = true</script><img src=x onerror="alert(1)">';
    const { container } = renderItem(
      chatItem(
        withTools([
          toolActivity({
            tool_id: "weather",
            tool_name: "Weather",
            summary: hostile,
            result: { forecast: hostile, nested: { ok: true } },
            result_truncated: true,
          }),
          // A built-in tool whose result lacks its usual shape falls back to JSON too.
          toolActivity({ invocation_id: "inv-2", result: { surprise: "<b>not bold</b>" } }),
        ]),
      ),
    );
    const json = [...container.querySelectorAll(".tool-json code")].map((code) => code.textContent);
    expect(json).toEqual([
      JSON.stringify({ forecast: hostile, nested: { ok: true } }, null, 2),
      JSON.stringify({ surprise: "<b>not bold</b>" }, null, 2),
    ]);
    expect(screen.getAllByText(hostile, { exact: false }).length).toBeGreaterThan(0);
    expect(container.querySelector("script, img, .tool-result b")).toBeNull();
    expect(screen.getByText("This result was too long and was truncated before the model read it.")).toBeTruthy();
  });

  it("hides a result whose source document the viewer can no longer read", () => {
    renderItem(
      chatItem(
        withTools([
          toolActivity({
            tool_id: "search_documents",
            summary: "Searching documents for “salaries”",
            result: null,
            result_hidden: true,
            sources: [{ document_id: "doc-gone", chunk_id: null }],
          }),
        ]),
      ),
    );
    expect(screen.getByText("Result hidden: a source document is no longer available to you.")).toBeTruthy();
    expect(screen.queryByText("Show result")).toBeNull();
  });
});

describe("a reply paused for approval", () => {
  afterEach(() => {
    cleanup();
  });

  const paused = (overrides: Partial<Message> = {}) =>
    chatMessage({
      status: "awaiting_approval",
      content: "I can save that for you.",
      completed_at: null,
      tools: [awaitingActivity()],
      ...overrides,
    });

  it("shows the action with its risk, details, the exact content and its expiry", () => {
    const { container } = renderItem(chatItem(paused()), { onDecide: vi.fn() });
    const card = screen.getByRole("region", { name: "Approval needed" });
    expect(within(card).getByText("High risk")).toBeTruthy();
    expect(within(card).getByText("Saving to your memories")).toBeTruthy();
    const terms = [...card.querySelectorAll("dt")].map((term) => term.textContent);
    expect(terms).toEqual(["Tool", "Action", "Target", "Content", "Kind", "Consequences", "Expires"]);
    const values = [...card.querySelectorAll("dd")].map((value) => value.textContent);
    expect(values.slice(0, 6)).toEqual([
      "Save to memory",
      "Save a memory",
      "Your private memories",
      MEMORY_TEXT,
      "preference",
      "NEXA will use this in your future conversations. Only you can see it, and you can delete it at any time on the Memories page.",
    ]);
    // The exact text, as text: its markup is not rendered.
    expect(card.querySelector("b")).toBeNull();
    expect(card.querySelector("time")?.getAttribute("dateTime")).toBe("2026-10-20T09:15:00+00:00");
    expect(within(card).getByText("Nothing is done unless you approve.")).toBeTruthy();
    // The timeline has the call, waiting; nothing says the reply failed or was cancelled.
    expect(rowText(rows()[0] as HTMLElement)).toBe("Saving to your memoriesNeeds your approval");
    expect(container.querySelector(".message-status")).toBeNull();
    expect(screen.getByText("I can save that for you.")).toBeTruthy();
  });

  it("shows details it does not know as plain text", () => {
    const odd = approval({
      details: { action: "Send an email", recipients: ["a@example.com"], body_html: "<script>x()</script>" },
    });
    const { container } = renderItem(chatItem(paused({ tools: [awaitingActivity(odd)] })), { onDecide: vi.fn() });
    const card = screen.getByRole("region", { name: "Approval needed" });
    expect([...card.querySelectorAll("dt")].map((term) => term.textContent)).toEqual([
      "Tool",
      "Action",
      "Recipients",
      "Body html",
      "Expires",
    ]);
    expect([...card.querySelectorAll("dd")].map((value) => value.textContent).slice(1, 4)).toEqual([
      "Send an email",
      JSON.stringify(["a@example.com"], null, 2),
      "<script>x()</script>",
    ]);
    expect(container.querySelector("script")).toBeNull();
  });

  it("approves or rejects through onDecide, and disables both while deciding", () => {
    const onDecide = vi.fn();
    const message = paused();
    const { rerender } = renderItem(chatItem(message), { onDecide });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenLastCalledWith("m4", approval(), "approve");
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecide).toHaveBeenLastCalledWith("m4", approval(), "reject");

    rerender(
      <MemoryRouter>
        <ol>
          <MessageBubble
            item={chatItem(message, { streaming: true, deciding: "approve" })}
            canRegenerate={false}
            canRetry={false}
            onRegenerate={vi.fn()}
            onRetry={vi.fn()}
            onDecide={onDecide}
          />
        </ol>
      </MemoryRouter>,
    );
    const approving = screen.getByRole<HTMLButtonElement>("button", { name: "Approving…" });
    expect(approving.disabled).toBe(true);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Reject" }).disabled).toBe(true);
  });

  it("offers no buttons where nothing can be decided", () => {
    renderItem(chatItem(paused()));
    expect(screen.getByRole("region", { name: "Approval needed" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("shows no card once the approval is no longer pending", () => {
    renderItem(
      chatItem(
        paused({
          status: "cancelled",
          error_code: "TOOL_APPROVAL_EXPIRED",
          tools: [
            {
              ...awaitingActivity(approval({ status: "expired" })),
              status: "cancelled",
            },
          ],
        }),
      ),
      { onDecide: vi.fn() },
    );
    expect(screen.queryByRole("region", { name: "Approval needed" })).toBeNull();
    expect(screen.getByText("The approval request expired before you decided.")).toBeTruthy();
    expect(screen.getByText("The action was not approved in time, so it expired and was not carried out.")).toBeTruthy();
  });
});

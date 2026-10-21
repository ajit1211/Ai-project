/** The chat answering from documents: choosing them, sending, the sources event, refusals. */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listModels } from "../api/ai";
import { ApiError } from "../api/client";
import {
  getConversation,
  listConversations,
  listMessages,
  parseChatEvent,
  sendMessage,
  type RequestControl,
} from "../api/conversations";
import { getDocument, listDocuments } from "../api/documents";
import { AuthContext } from "../auth/AuthContext";
import { chatMessage, grounding, source } from "../test/chatFixtures";
import { documentIndex, documentPage, documentRecord, signedIn } from "../test/documentFixtures";
import type { ChatStreamEvent, Conversation, Message } from "../types/chat";
import { ChatPage } from "./ChatPage";

vi.mock("../api/conversations", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/conversations")>();
  return {
    ...actual,
    listConversations: vi.fn(),
    createConversation: vi.fn(),
    getConversation: vi.fn(),
    updateConversation: vi.fn(),
    deleteConversation: vi.fn(),
    listMessages: vi.fn(),
    sendMessage: vi.fn(),
    regenerate: vi.fn(),
    cancelGeneration: vi.fn(),
    listMessageSources: vi.fn(),
  };
});

vi.mock("../api/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/ai")>();
  return { ...actual, listModels: vi.fn() };
});

vi.mock("../api/documents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/documents")>();
  return { ...actual, listDocuments: vi.fn(), getDocument: vi.fn() };
});

const conversation: Conversation = {
  id: "c1",
  title: "Quarterly numbers",
  model: "",
  status: "active",
  message_count: 0,
  generating: false,
  created_at: "2026-10-20T08:00:00+00:00",
  updated_at: "2026-10-20T08:00:00+00:00",
  last_message_at: null,
};

const report = documentRecord({ id: "doc-1", filename: "q4-report.pdf", title: "Q4 report" });
const sales = documentRecord({ id: "doc-2", filename: "sales.csv", format: "csv" });
const draftPlan = documentRecord({
  id: "doc-3",
  filename: "plan.md",
  format: "md",
  index: documentIndex({ status: "indexing", searchable: false }),
});

const question = chatMessage({ id: "m3", sequence: 3, role: "user", content: "How did revenue do?", model: "" });
const reply = chatMessage({ status: "pending", content: "", grounding: grounding({ outcome: "pending" }), sources: [] });

const TERMINAL = new Set<string>(["message_complete", "message_cancelled", "error"]);

/** A reply stream the test feeds event by event; it ends after a terminal event or on abort. */
function controlledStream() {
  const queue: ChatStreamEvent[] = [];
  let wake: () => void = () => undefined;
  return {
    push(...events: ChatStreamEvent[]) {
      queue.push(...events);
      wake();
    },
    async *events(control?: RequestControl): AsyncGenerator<ChatStreamEvent, void, undefined> {
      for (;;) {
        const next = queue.shift();
        if (next) {
          yield next;
          if (TERMINAL.has(next.type)) return;
          continue;
        }
        await new Promise<void>((resolve, reject) => {
          wake = resolve;
          control?.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      }
    },
  };
}

async function* refused(error: Error): AsyncGenerator<ChatStreamEvent, void, undefined> {
  await Promise.resolve();
  throw error;
  yield* []; // never reached; keeps the function a generator
}

function renderChat() {
  return render(
    <AuthContext.Provider value={signedIn}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:conversationId?" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const composer = () => screen.getByLabelText<HTMLTextAreaElement>("Message");
const documentsButton = () => screen.getByRole("button", { name: "Documents" });
const picker = () => screen.getByRole("dialog", { name: "Answer from documents" });
const messages = () => screen.getByRole("list", { name: "Messages" });

async function openChat(): Promise<void> {
  renderChat();
  await waitFor(() => {
    expect(composer().getAttribute("aria-disabled")).toBe("false");
  });
}

function sendText(text: string): void {
  fireEvent.change(composer(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

/** Open the picker, tick these documents and confirm. */
async function choose(...names: string[]): Promise<void> {
  fireEvent.click(documentsButton());
  for (const name of names) {
    fireEvent.click(await within(picker()).findByRole("checkbox", { name: new RegExp(name) }));
  }
  fireEvent.click(within(picker()).getByRole("button", { name: "Use selected documents" }));
}

async function feed(stream: ReturnType<typeof controlledStream>, ...events: ChatStreamEvent[]): Promise<void> {
  await act(async () => {
    stream.push(...events);
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

function sentBody(): Record<string, unknown> {
  const body = vi.mocked(sendMessage).mock.calls.at(-1)?.[1];
  if (!body) throw new Error("nothing was sent");
  return body as unknown as Record<string, unknown>;
}

describe("ChatPage answering from documents", () => {
  beforeEach(() => {
    vi.mocked(listModels).mockResolvedValue([]);
    vi.mocked(listConversations).mockResolvedValue({ data: [conversation], meta: { next_cursor: null, has_more: false } });
    vi.mocked(getConversation).mockResolvedValue(conversation);
    vi.mocked(listMessages).mockResolvedValue({ data: [], meta: { next_before: null, has_more: false } });
    vi.mocked(listDocuments).mockResolvedValue(documentPage([report, sales, draftPlan]));
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("lists only searchable documents and sends the chosen ones as grounding", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    await openChat();

    fireEvent.click(documentsButton());
    const dialog = picker();
    expect(await within(dialog).findByRole("checkbox", { name: /Q4 report/ })).toBeTruthy();
    expect(listDocuments).toHaveBeenCalledWith(
      { searchable: true, q: undefined, limit: 20 },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );
    // Not searchable yet: listed by an older answer, but never selectable.
    expect(within(dialog).getByRole("checkbox", { name: /plan\.md/ }).hasAttribute("disabled")).toBe(true);
    expect(within(dialog).getByRole("link", { name: "Manage documents" }).getAttribute("href")).toBe("/documents");
    expect(within(dialog).getByText("Documents appear here once they are indexed.", { exact: false })).toBeTruthy();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Q4 report/ }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /sales\.csv/ }));
    expect(within(dialog).getByText("2 of 20 selected")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Use selected documents" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(documentsButton());
    expect(screen.getByText("Answering from 2 documents")).toBeTruthy();
    const chips = screen.getByRole("list", { name: "Selected documents" });
    expect(within(chips).getAllByRole("listitem").map((chip) => chip.textContent)).toEqual(["Q4 report", "sales.csv"]);
    expect(composer().placeholder).toBe("Ask about your documents…");

    sendText("How did revenue do?");
    expect(sendMessage).toHaveBeenCalledWith(
      "c1",
      {
        content: "How did revenue do?",
        client_message_id: expect.any(String) as string,
        grounding: { document_ids: ["doc-1", "doc-2"] },
      },
      { signal: expect.any(AbortSignal) as AbortSignal },
    );
    // The selection stays for the next question.
    expect(screen.getByText("Answering from 2 documents")).toBeTruthy();
  });

  it("answers from all documents, and goes back to ordinary chat without grounding", async () => {
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => controlledStream().events(control));
    await openChat();

    fireEvent.click(documentsButton());
    fireEvent.click(within(picker()).getByRole("radio", { name: "All documents I can access" }));
    fireEvent.click(within(picker()).getByRole("button", { name: "Use all documents" }));
    expect(screen.getByText("Answering from all your documents")).toBeTruthy();
    sendText("Summarize everything");
    expect(sentBody().grounding).toEqual({ all_documents: true });
  });

  it("sends exactly the ordinary body once the selection is cleared", async () => {
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => controlledStream().events(control));
    await openChat();
    await choose("Q4 report");
    const chips = screen.getByRole("list", { name: "Selected documents" });
    fireEvent.click(within(chips).getByRole("button", { name: "Remove Q4 report" }));
    expect(screen.queryByText(/^Answering from/)).toBeNull();
    expect(document.activeElement).toBe(documentsButton());

    await choose("sales");
    fireEvent.click(screen.getByRole("button", { name: "Use ordinary chat" }));
    expect(screen.queryByRole("list", { name: "Selected documents" })).toBeNull();
    sendText("Hello");
    expect(Object.keys(sentBody())).toEqual(["content", "client_message_id"]);
  });

  it("streams the sources event before the answer, then cites them", async () => {
    const stream = controlledStream();
    vi.mocked(sendMessage).mockImplementation((_id, _body, control) => stream.events(control));
    await openChat();
    await choose("Q4 report");
    sendText("How did revenue do?");
    expect(within(messages()).getByText("Searching 1 document…")).toBeTruthy();

    const s1 = source();
    const s2 = source({ label: "S2", rank: 2, cited: false, page_start: 9, page_end: 9 });
    await feed(stream, {
      type: "message_start",
      protocol: "nexa.chat.v1",
      conversation_id: "c1",
      request_id: "req_1",
      user_message: question,
      assistant_message: reply,
    });
    expect(within(messages()).getByText("From your documents")).toBeTruthy();
    expect(within(messages()).getByText("Searching 1 document…")).toBeTruthy();

    await feed(stream, { type: "sources", message_id: "m4", searched_documents: 1, sources: [s1, s2] });
    expect(within(messages()).getByText("Found 2 passages")).toBeTruthy();

    await feed(stream, { type: "text_delta", message_id: "m4", text: "Revenue grew 12% [S1]." });
    expect(within(messages()).queryByText("Found 2 passages")).toBeNull();
    expect(within(messages()).getByRole("button", { name: "Source S1: Q4 report, page 3" })).toBeTruthy();

    const done: Message = {
      ...reply,
      status: "completed",
      content: "Revenue grew 12% [S1].",
      grounding: grounding({ outcome: "answered", supplied: 2, cited: 1 }),
      sources: [s1, s2],
    };
    await feed(stream, { type: "message_complete", message: done });
    expect(within(messages()).getByRole("list", { name: "Sources" })).toBeTruthy();
    expect(within(messages()).getByText("Other passages consulted (1)")).toBeTruthy();
  });

  it("explains a 409 in the composer, names the documents still being indexed and puts the question back", async () => {
    vi.mocked(sendMessage).mockImplementation(() =>
      refused(
        new ApiError(
          409,
          {
            code: "DOCUMENT_NOT_INDEXED",
            message: "Some of the selected documents are still being prepared for search.",
            details: [{ field: "grounding.document_ids", code: "not_indexed", message: "Still being prepared." }],
          },
          "req_5",
        ),
      ),
    );
    vi.mocked(getDocument).mockImplementation((id) =>
      Promise.resolve(id === "doc-2" ? { ...sales, index: documentIndex({ status: "indexing", searchable: false }) } : report),
    );
    await openChat();
    await choose("Q4 report", "sales");
    sendText("How did revenue do?");

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toBe(
        "Some of the selected documents are still being prepared for search. Still being prepared for search: sales.csv. Try again once indexing finishes.",
      );
    });
    expect(composer().value).toBe("How did revenue do?");
    // Explained in the composer instead of left as an unsent message (the thread stays empty).
    expect(screen.queryByText("Not sent")).toBeNull();
    expect(screen.queryByRole("list", { name: "Messages" })).toBeNull();
    expect(screen.getByText("Answering from 2 documents")).toBeTruthy();
  });

  it("drops the documents that are gone after a 404", async () => {
    vi.mocked(sendMessage).mockImplementation(() =>
      refused(
        new ApiError(
          404,
          { code: "DOCUMENT_UNAVAILABLE", message: "One or more of the selected documents are not available." },
          "req_6",
        ),
      ),
    );
    vi.mocked(getDocument).mockImplementation((id) =>
      id === "doc-1"
        ? Promise.reject(new ApiError(404, { code: "RESOURCE_NOT_FOUND", message: "Not found." }, "req_7"))
        : Promise.resolve(sales),
    );
    await openChat();
    await choose("Q4 report", "sales");
    sendText("How did revenue do?");

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toBe(
        "Q4 report is no longer available and was removed from the selection. Check the selection and send again.",
      );
    });
    const chips = screen.getByRole("list", { name: "Selected documents" });
    expect(within(chips).getAllByRole("listitem").map((chip) => chip.textContent)).toEqual(["sales.csv"]);
    expect(composer().value).toBe("How did revenue do?");
  });

  it("keeps grounded questions within 4,000 characters", async () => {
    await openChat();
    await choose("Q4 report");
    sendText("x".repeat(4001));
    expect(screen.getByRole("alert").textContent).toBe(
      "Questions about documents are limited to 4,000 characters; this one has 4,001. Shorten it, or switch to ordinary chat.",
    );
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("reads the sources event from the stream", () => {
    const event = parseChatEvent({
      event: "sources",
      id: "3",
      data: JSON.stringify({ message_id: "m4", searched_documents: 2, sources: [source()] }),
    });
    expect(event).toEqual({ type: "sources", message_id: "m4", searched_documents: 2, sources: [source()] });
  });
});

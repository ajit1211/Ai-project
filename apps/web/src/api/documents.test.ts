import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { describeDocumentError } from "../components/documents/labels";
import { ApiError, getAccessToken, onSessionLost, setAccessToken } from "./client";
import {
  downloadDocument,
  getDocumentContent,
  getIndexStatus,
  isAbortError,
  listDocuments,
  requestReindex,
  REVOKE_DOWNLOAD_URL_MS,
  uploadDocument,
} from "./documents";

type Call = { url: string; init: RequestInit };

let calls: Call[];
let responses: Response[];

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/** Just enough of XMLHttpRequest for the uploads: the test plays the server. */
class FakeXhr {
  static sent: FakeXhr[] = [];
  readonly upload = new EventTarget();
  private readonly events = new EventTarget();
  private responseHeaders: Record<string, string> = {};
  method = "";
  url = "";
  headers: Record<string, string> = {};
  body: FormData | null = null;
  status = 0;
  responseText = "";

  open(method: string, url: string): void {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string): void {
    this.headers[name] = value;
  }

  getResponseHeader(name: string): string | null {
    return this.responseHeaders[name.toLowerCase()] ?? null;
  }

  addEventListener(type: string, listener: EventListener): void {
    this.events.addEventListener(type, listener);
  }

  send(body: FormData): void {
    this.body = body;
    FakeXhr.sent.push(this);
  }

  abort(): void {
    this.events.dispatchEvent(new Event("abort"));
  }

  progress(loaded: number, total: number): void {
    this.upload.dispatchEvent(new ProgressEvent("progress", { loaded, total, lengthComputable: true }));
  }

  respond(status: number, body: string, headers: Record<string, string> = {}): void {
    this.status = status;
    this.responseText = body;
    for (const [name, value] of Object.entries(headers)) this.responseHeaders[name.toLowerCase()] = value;
    this.events.dispatchEvent(new Event("load"));
  }

  respondJson(status: number, body: unknown, headers: Record<string, string> = {}): void {
    this.respond(status, JSON.stringify(body), { "Content-Type": "application/json", ...headers });
  }

  fail(): void {
    this.events.dispatchEvent(new Event("error"));
  }
}

function lastXhr(): FakeXhr {
  const xhr = FakeXhr.sent.at(-1);
  if (!xhr) throw new Error("no upload was sent");
  return xhr;
}

const file = () => new File(["hello"], "notes.txt", { type: "text/plain" });

beforeEach(() => {
  calls = [];
  responses = [];
  FakeXhr.sent = [];
  setAccessToken("token");
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit) => {
      calls.push({ url, init });
      const next = responses.shift();
      if (!next) throw new Error(`unexpected request ${url}`);
      return Promise.resolve(next);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  onSessionLost(null);
});

describe("listDocuments", () => {
  it("builds the query string from the filters and reads the paging meta and limits", async () => {
    responses.push(
      json(200, {
        success: true,
        data: [{ id: "d1" }],
        meta: {
          next_cursor: "c2",
          has_more: true,
          limits: { max_upload_bytes: 1024, accepted_extensions: [".PDF", ".txt", 3] },
        },
      }),
    );
    const page = await listDocuments({
      status: "failed",
      format: "pdf",
      owner: "me",
      searchable: false,
      q: "q4 report",
      cursor: "c1",
      limit: 30,
    });
    expect(calls[0]?.url).toBe(
      "/api/v1/documents/?status=failed&format=pdf&owner=me&searchable=false&q=q4+report&cursor=c1&limit=30",
    );
    expect(page.data).toEqual([{ id: "d1" }]);
    expect(page.meta).toEqual({
      next_cursor: "c2",
      has_more: true,
      limits: { max_upload_bytes: 1024, accepted_extensions: [".pdf", ".txt"] },
    });
  });

  it("sends no query string without filters, and copes with missing meta", async () => {
    responses.push(json(200, { success: true, data: [] }));
    const page = await listDocuments();
    expect(calls[0]?.url).toBe("/api/v1/documents/");
    expect(page.meta).toEqual({ next_cursor: null, has_more: false, limits: null });
  });
});

describe("text, index and status endpoints", () => {
  it("asks for a page, a slice or the beginning of the text", async () => {
    const slice = { content: "x", offset: 0, limit: 1, total_chars: 1, has_more: false, next_offset: null, pages: [] };
    responses.push(json(200, { success: true, data: slice }));
    responses.push(json(200, { success: true, data: slice }));
    responses.push(json(200, { success: true, data: slice }));
    await getDocumentContent("d 1", { page: 3 });
    await getDocumentContent("d1", { offset: 20_000 });
    await getDocumentContent("d1");
    expect(calls.map((call) => call.url)).toEqual([
      "/api/v1/documents/d%201/content/?page=3",
      "/api/v1/documents/d1/content/?offset=20000",
      "/api/v1/documents/d1/content/",
    ]);
  });

  it("requests a re-index and reads the index status", async () => {
    responses.push(json(202, { success: true, data: { status: "pending" } }));
    responses.push(json(200, { success: true, data: { status: "indexing" } }));
    expect(await requestReindex("d1")).toEqual({ status: "pending" });
    expect(await getIndexStatus("d1")).toEqual({ status: "indexing" });
    expect(calls[0]?.url).toBe("/api/v1/documents/d1/index/");
    expect(calls[0]?.init.method).toBe("POST");
    expect(calls[1]?.url).toBe("/api/v1/documents/d1/index-status/");
  });
});

describe("uploadDocument", () => {
  it("posts the file with the token, reports progress and sends only the fields given", async () => {
    const progress = vi.fn();
    const pending = uploadDocument(file(), { title: " Notes ", category: "" }, { onProgress: progress });
    const xhr = lastXhr();
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/api/v1/documents/");
    expect(xhr.headers.Authorization).toBe("Bearer token");
    expect(xhr.body?.get("file")).toBeInstanceOf(File);
    expect(xhr.body?.get("title")).toBe("Notes");
    expect(xhr.body?.has("category")).toBe(false);
    expect(xhr.body?.has("visibility")).toBe(false);

    xhr.progress(40, 100);
    expect(progress).toHaveBeenCalledWith(40, 100);
    xhr.respondJson(201, { success: true, data: { id: "d1", status: "queued" } });
    await expect(pending).resolves.toEqual({ id: "d1", status: "queued" });
  });

  it.each([
    [
      "an unsupported type, with the server's own words",
      415,
      { code: "UNSUPPORTED_MEDIA_TYPE", message: "The file's content does not look like a PDF file." },
      {},
      "The file's content does not look like a PDF file.",
    ],
    [
      "a full storage quota",
      402,
      { code: "QUOTA_EXCEEDED", message: "The organization's document storage is full. Delete documents to make room." },
      {},
      "The organization's document storage is full. Delete documents to make room.",
    ],
    [
      "a file over the limit",
      413,
      { code: "PAYLOAD_TOO_LARGE", message: "The file is larger than the 50 MB limit." },
      {},
      "The file is larger than the 50 MB limit.",
    ],
    [
      "an invalid field, with its own message",
      422,
      {
        code: "VALIDATION_FAILED",
        message: "Some fields are invalid.",
        details: [{ field: "file", code: "empty", message: "The file is empty." }],
      },
      {},
      "The file is empty.",
    ],
    [
      "the rate limit, with the wait",
      429,
      { code: "RATE_LIMITED", message: "Too many requests." },
      { "Retry-After": "120" },
      "Too many requests. Try again in 2 min.",
    ],
    [
      "an unexpected failure, without the server's words",
      500,
      { code: "INTERNAL_ERROR", message: "KeyError: 'organization' in services.py" },
      {},
      "Something went wrong on NEXA's side. Try again in a moment. (ref req_9)",
    ],
  ])("rejects %s as an ApiError", async (_case, status, error, headers, expected) => {
    const pending = uploadDocument(file());
    lastXhr().respondJson(status, { success: false, error, request_id: "req_9" }, headers);
    const refusal = await pending.catch((err: unknown) => err);
    expect(refusal).toBeInstanceOf(ApiError);
    expect((refusal as ApiError).status).toBe(status);
    expect((refusal as ApiError).code).toBe(error.code);
    expect(describeDocumentError(refusal)).toBe(expected);
  });

  it("reports a proxy's refusal and a lost connection in plain words", async () => {
    const tooLarge = uploadDocument(file());
    lastXhr().respond(413, "<html>Request Entity Too Large</html>", { "Content-Type": "text/html" });
    const proxy = await tooLarge.catch((err: unknown) => err);
    expect((proxy as ApiError).code).toBe("NETWORK_ERROR");
    expect(describeDocumentError(proxy)).toBe("The file is too large for the server to accept.");

    const offline = uploadDocument(file());
    lastXhr().fail();
    const lost = await offline.catch((err: unknown) => err);
    expect((lost as ApiError).code).toBe("NETWORK_ERROR");
    expect(describeDocumentError(lost)).toBe(
      "NEXA could not be reached. Check your connection and try again.",
    );
  });

  it("refreshes an expired token once and sends the file again", async () => {
    responses.push(
      json(200, { success: true, data: { access_token: "fresh", active_organization_id: "o1" } }),
    );
    const pending = uploadDocument(file());
    lastXhr().respondJson(401, {
      success: false,
      error: { code: "AUTH_TOKEN_EXPIRED", message: "expired" },
      request_id: "req_1",
    });
    await vi.waitFor(() => {
      expect(FakeXhr.sent).toHaveLength(2);
    });
    expect(calls[0]?.url).toBe("/api/v1/auth/refresh/");
    expect(lastXhr().headers.Authorization).toBe("Bearer fresh");
    expect(lastXhr().body?.get("file")).toBeInstanceOf(File);
    lastXhr().respondJson(201, { success: true, data: { id: "d1" } });
    await expect(pending).resolves.toEqual({ id: "d1" });
    expect(getAccessToken()).toBe("fresh");
  });

  it("ends the session when the token cannot be refreshed", async () => {
    const lost = vi.fn();
    onSessionLost(lost);
    const pending = uploadDocument(file());
    lastXhr().respondJson(401, {
      success: false,
      error: { code: "AUTH_SESSION_REVOKED", message: "This session has ended." },
      request_id: "req_2",
    });
    const refusal = await pending.catch((err: unknown) => err);
    expect((refusal as ApiError).status).toBe(401);
    expect(lost).toHaveBeenCalledOnce();
    expect(FakeXhr.sent).toHaveLength(1);
  });

  it("is cancelled through its signal", async () => {
    const controller = new AbortController();
    const pending = uploadDocument(file(), {}, { signal: controller.signal });
    controller.abort();
    const cancelled = await pending.catch((err: unknown) => err);
    expect(isAbortError(cancelled)).toBe(true);
  });
});

describe("downloadDocument", () => {
  let created: Mock<(blob: Blob) => string>;
  let revoked: Mock<(url: string) => void>;
  let saved: { create?: PropertyDescriptor; revoke?: PropertyDescriptor };

  beforeEach(() => {
    created = vi.fn<(blob: Blob) => string>(() => "blob:nexa/1");
    revoked = vi.fn<(url: string) => void>();
    saved = {
      create: Object.getOwnPropertyDescriptor(URL, "createObjectURL"),
      revoke: Object.getOwnPropertyDescriptor(URL, "revokeObjectURL"),
    };
    Object.defineProperty(URL, "createObjectURL", { value: created, configurable: true, writable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: revoked, configurable: true, writable: true });
  });

  afterEach(() => {
    for (const [name, descriptor] of [
      ["createObjectURL", saved.create],
      ["revokeObjectURL", saved.revoke],
    ] as const) {
      if (descriptor) Object.defineProperty(URL, name, descriptor);
      else Reflect.deleteProperty(URL, name);
    }
  });

  it("fetches the bytes with the token and saves them under the document's name", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const clicked: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this);
    });
    responses.push(
      new Response(new Blob(["%PDF-1.7"]), {
        status: 200,
        headers: { "Content-Type": "application/pdf" },
      }),
    );

    await downloadDocument({ id: "d1", filename: "Q4 report.pdf" });

    expect(calls[0]?.url).toBe("/api/v1/documents/d1/download/");
    expect((calls[0]?.init.headers as Record<string, string>).Authorization).toBe("Bearer token");
    expect(created).toHaveBeenCalledOnce();
    expect(clicked).toHaveLength(1);
    expect(clicked[0]?.download).toBe("Q4 report.pdf");
    expect(clicked[0]?.getAttribute("href")).toBe("blob:nexa/1");
    expect(clicked[0]?.isConnected).toBe(false); // the temporary link is gone again
    expect(revoked).not.toHaveBeenCalled();
    vi.advanceTimersByTime(REVOKE_DOWNLOAD_URL_MS);
    expect(revoked).toHaveBeenCalledWith("blob:nexa/1");
  });

  it("throws the refusal and saves nothing", async () => {
    responses.push(
      json(404, {
        success: false,
        error: { code: "RESOURCE_NOT_FOUND", message: "The requested resource was not found." },
        request_id: "req_3",
      }),
    );
    const refusal = await downloadDocument({ id: "d1", filename: "x.pdf" }).catch((err: unknown) => err);
    expect(refusal).toBeInstanceOf(ApiError);
    expect(describeDocumentError(refusal)).toBe(
      "This document no longer exists, or you no longer have access to it.",
    );
    expect(created).not.toHaveBeenCalled();
  });
});

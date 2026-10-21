/**
 * The document endpoints (docs/api/documents.md). This is the only module that calls
 * `/documents/...`; the pages go through `src/hooks/useDocuments.ts` (the library) and
 * `src/hooks/useDocument.ts` (one document).
 *
 * Uploads use XMLHttpRequest, the one browser API that reports upload progress, with the same
 * bearer token and 401 recovery as `api()`. Downloads fetch the original bytes with the token
 * and hand them to the browser as a file to save; nothing from a document is ever rendered.
 */

import type {
  ContentRequest,
  DocumentContent,
  DocumentIndex,
  DocumentPage,
  DocumentRecord,
  DocumentStatusInfo,
  ListDocumentsParams,
  UpdateDocumentBody,
  UploadDocumentFields,
  UploadLimits,
} from "../types/document";
import {
  API_BASE,
  ApiError,
  api,
  authorizedFetch,
  getAccessToken,
  parseEnvelope,
  recoverFromUnauthorized,
  type ApiResponse,
} from "./client";

export interface RequestControl {
  signal?: AbortSignal;
}

export interface UploadControl {
  /** Bytes sent so far and in all, as the browser reports them. */
  onProgress?: (sent: number, total: number) => void;
  /** Cancels the upload: the promise then rejects with an AbortError. */
  signal?: AbortSignal;
}

/** How long a download's object URL outlives the click: the browser may still be reading it. */
export const REVOKE_DOWNLOAD_URL_MS = 10_000;

const documentPath = (id: string): string => `/documents/${encodeURIComponent(id)}/`;

/** True for the error of a request cancelled through its AbortSignal (fetch or upload). */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** The documents the caller can see, newest first, one page at a time. */
export async function listDocuments(
  params: ListDocumentsParams = {},
  control: RequestControl = {},
): Promise<DocumentPage> {
  const query = new URLSearchParams();
  if (params.status) query.set("status", params.status);
  if (params.format) query.set("format", params.format);
  if (params.owner) query.set("owner", params.owner);
  if (params.category) query.set("category", params.category);
  if (params.team_id) query.set("team_id", params.team_id);
  if (params.conversation_id) query.set("conversation_id", params.conversation_id);
  if (params.searchable !== undefined) query.set("searchable", String(params.searchable));
  if (params.q) query.set("q", params.q);
  if (params.cursor) query.set("cursor", params.cursor);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const { data, meta } = await api<DocumentRecord[]>(`/documents/${suffix}`, {
    signal: control.signal,
  });
  return {
    data,
    meta: {
      next_cursor: typeof meta?.next_cursor === "string" ? meta.next_cursor : null,
      has_more: meta?.has_more === true,
      limits: readLimits(meta?.limits),
    },
  };
}

/** The upload limits of a list's meta, or null when they are missing or malformed. */
function readLimits(value: unknown): UploadLimits | null {
  if (typeof value !== "object" || value === null) return null;
  const { max_upload_bytes: max, accepted_extensions: extensions } = value as Record<string, unknown>;
  if (typeof max !== "number" || !Array.isArray(extensions)) return null;
  const listed: unknown[] = extensions;
  return {
    max_upload_bytes: max,
    accepted_extensions: listed
      .filter((item): item is string => typeof item === "string")
      .map((item) => item.toLowerCase()),
  };
}

/** One document, with its processing attempts. */
export async function getDocument(id: string, control: RequestControl = {}): Promise<DocumentRecord> {
  const { data } = await api<DocumentRecord>(documentPath(id), { signal: control.signal });
  return data;
}

/** Where processing stands: the cheap call to poll while a document is in flight. */
export async function getDocumentStatus(
  id: string,
  control: RequestControl = {},
): Promise<DocumentStatusInfo> {
  const { data } = await api<DocumentStatusInfo>(`${documentPath(id)}status/`, {
    signal: control.signal,
  });
  return data;
}

/**
 * A slice of the extracted text, or one page of it. Refused with 409 CONFLICT while the
 * document has no text, and 404 for a page that does not exist.
 */
export async function getDocumentContent(
  id: string,
  request: ContentRequest = {},
  control: RequestControl = {},
): Promise<DocumentContent> {
  const query = new URLSearchParams();
  if ("page" in request) {
    query.set("page", String(request.page));
  } else {
    if (request.offset !== undefined) query.set("offset", String(request.offset));
    if (request.limit !== undefined) query.set("limit", String(request.limit));
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const { data } = await api<DocumentContent>(`${documentPath(id)}content/${suffix}`, {
    signal: control.signal,
  });
  return data;
}

export async function updateDocument(id: string, body: UpdateDocumentBody): Promise<DocumentRecord> {
  const { data } = await api<DocumentRecord>(documentPath(id), { method: "PATCH", body });
  return data;
}

export async function deleteDocument(id: string): Promise<void> {
  await api(documentPath(id), { method: "DELETE" });
}

/** Queues processing again; refused with 409 when the document cannot be retried. */
export async function retryDocument(id: string): Promise<DocumentRecord> {
  const { data } = await api<DocumentRecord>(`${documentPath(id)}retry/`, { method: "POST" });
  return data;
}

/**
 * Asks for the search index to be rebuilt; resolves to the index, now pending. Refused with
 * 409 while the document has no extracted text, and 429 when asked too often.
 */
export async function requestReindex(id: string): Promise<DocumentIndex> {
  const { data } = await api<DocumentIndex>(`${documentPath(id)}index/`, { method: "POST" });
  return data;
}

/**
 * Asks for the passages to be embedded again for semantic search, leaving the keyword index as
 * it is; resolves to the index, its semantic part now pending. Refused with 409 while the
 * passages are not indexed or semantic search does not run here, and 429 when asked too often.
 */
export async function requestReembed(id: string): Promise<DocumentIndex> {
  const { data } = await api<DocumentIndex>(`${documentPath(id)}index/`, {
    method: "POST",
    body: { reembed: true },
  });
  return data;
}

/**
 * Where the search index stands, semantic part included: the call to poll while either is
 * pending or being built.
 */
export async function getIndexStatus(id: string, control: RequestControl = {}): Promise<DocumentIndex> {
  const { data } = await api<DocumentIndex>(`${documentPath(id)}index-status/`, {
    signal: control.signal,
  });
  return data;
}

/** Uploads one file; it comes back "queued", and processing starts in the background. */
export async function uploadDocument(
  file: File,
  fields: UploadDocumentFields = {},
  control: UploadControl = {},
): Promise<DocumentRecord> {
  const form = new FormData();
  form.append("file", file, file.name);
  const optional: [string, string | undefined][] = [
    ["title", fields.title],
    ["category", fields.category],
    ["visibility", fields.visibility],
    ["team_id", fields.team_id],
    ["conversation_id", fields.conversation_id],
  ];
  for (const [name, value] of optional) {
    const trimmed = value?.trim();
    if (trimmed) form.append(name, trimmed);
  }
  const { data } = await uploadWithProgress<DocumentRecord>("/documents/", form, control);
  return data;
}

/**
 * POST a multipart form, reporting progress. Resolves to the unwrapped envelope; rejects with
 * the refusal's `ApiError` for any status but 2xx, an `ApiError` coded NETWORK_ERROR when the
 * server could not be reached, or an AbortError when `control.signal` cancelled it. A 401
 * refreshes the token and sends the form once more, as `api()` does.
 */
export async function uploadWithProgress<T>(
  path: string,
  form: FormData,
  control: UploadControl = {},
): Promise<ApiResponse<T>> {
  let response = await sendForm(path, form, control);
  if (response.status === 401) {
    const payload: unknown = await response
      .clone()
      .json()
      .catch(() => null);
    if (await recoverFromUnauthorized(payload)) response = await sendForm(path, form, control);
  }
  return parseEnvelope<T>(response);
}

function abortError(): DOMException {
  return new DOMException("The upload was cancelled.", "AbortError");
}

function networkError(): ApiError {
  return new ApiError(0, { code: "NETWORK_ERROR", message: "The upload could not reach NEXA." }, null);
}

/** One XHR attempt, answered as a `Response` so `parseEnvelope` can read it like any other. */
function sendForm(path: string, form: FormData, { onProgress, signal }: UploadControl): Promise<Response> {
  return new Promise<Response>((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const xhr = new XMLHttpRequest();
    const cancel = () => {
      xhr.abort();
    };
    const settle = () => {
      signal?.removeEventListener("abort", cancel);
    };
    xhr.open("POST", `${API_BASE}${path}`);
    xhr.setRequestHeader("Accept", "application/json");
    const token = getAccessToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded, event.total);
    });
    xhr.addEventListener("load", () => {
      settle();
      const response = toResponse(xhr);
      if (response) resolve(response);
      else reject(networkError());
    });
    xhr.addEventListener("error", () => {
      settle();
      reject(networkError());
    });
    xhr.addEventListener("abort", () => {
      settle();
      reject(abortError());
    });
    signal?.addEventListener("abort", cancel, { once: true });
    xhr.send(form);
  });
}

/** The finished XHR as a `Response`; null for a status no HTTP server sends (0 is a network failure). */
function toResponse(xhr: XMLHttpRequest): Response | null {
  if (xhr.status < 200 || xhr.status > 599) return null;
  const headers = new Headers();
  for (const name of ["Content-Type", "Retry-After", "X-Request-ID"]) {
    const value = xhr.getResponseHeader(name);
    if (value !== null) headers.set(name, value);
  }
  const bodiless = xhr.status === 204 || xhr.status === 205 || xhr.status === 304;
  return new Response(bodiless ? null : xhr.responseText, { status: xhr.status, headers });
}

/**
 * Saves the original file: fetched with the token (a plain link cannot carry it), then handed
 * to the browser through a temporary object URL and `<a download>`.
 */
export async function downloadDocument(record: Pick<DocumentRecord, "id" | "filename">): Promise<void> {
  const response = await authorizedFetch(`${documentPath(record.id)}download/`);
  if (!response.ok) {
    await parseEnvelope(response); // throws the refusal's ApiError
    throw new ApiError(
      response.status,
      { code: "NETWORK_ERROR", message: `Unexpected response (${String(response.status)}).` },
      response.headers.get("X-Request-ID"),
    );
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = window.document.createElement("a");
  link.href = url;
  link.download = record.filename;
  link.rel = "noopener";
  link.hidden = true;
  window.document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    window.setTimeout(() => {
      URL.revokeObjectURL(url);
    }, REVOKE_DOWNLOAD_URL_MS);
  }
}

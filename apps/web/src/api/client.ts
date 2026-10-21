/**
 * The NEXA API client.
 *
 * The access token lives in this module's memory and nowhere else: not in localStorage, not
 * in a cookie the page can read. The refresh token is an HttpOnly cookie the browser sends
 * only to /api/v1/auth, so a script injected into the page can never read either credential.
 *
 * When a request comes back 401, the client refreshes the access token once (one refresh in
 * flight at a time, however many requests failed together) and replays the request. If the
 * refresh fails, the session is over and `onSessionLost` is told.
 *
 * `api()` is the JSON envelope call every page uses. `authorizedFetch()` is the same
 * token-and-refresh behaviour around a raw `fetch`, for responses that are not JSON
 * envelopes (the AI event stream, document downloads). Document uploads use XHR for its
 * progress events and share the 401 handling through `recoverFromUnauthorized()`.
 */

import type { TokenPayload } from "./types";

export const API_BASE = "/api/v1";
const CSRF_HEADER = "X-NEXA-CSRF";

export interface FieldError {
  field: string;
  code: string;
  message: string;
}

interface SuccessEnvelope<T> {
  success: true;
  data: T;
  message?: string;
  meta?: Record<string, unknown>;
}

interface ErrorEnvelope {
  success: false;
  error: { code: string; message: string; details?: FieldError[] };
  request_id: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: FieldError[];
  readonly requestId: string | null;
  /** Seconds to wait before retrying, from the Retry-After header (429 responses). */
  readonly retryAfter: number | null;

  constructor(
    status: number,
    body: ErrorEnvelope["error"],
    requestId: string | null,
    retryAfter: number | null = null,
  ) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.details = body.details ?? [];
    this.requestId = requestId;
    this.retryAfter = retryAfter;
  }

  /** The message for one field, used next to form inputs. */
  fieldMessage(field: string): string | undefined {
    return this.details.find((detail) => detail.field === field)?.message;
  }
}

export interface ApiResponse<T> {
  data: T;
  message?: string;
  meta?: Record<string, unknown>;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  /** Attach the access token (default true). Public endpoints pass false. */
  auth?: boolean;
  /** Cancels the request, including a replay after a token refresh. */
  signal?: AbortSignal;
}

let accessToken: string | null = null;
let refreshInFlight: Promise<TokenPayload | null> | null = null;
let sessionLostHandler: (() => void) | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function onSessionLost(handler: (() => void) | null): void {
  sessionLostHandler = handler;
}

/** Seconds to wait, from a Retry-After header holding either delay-seconds or an HTTP date. */
function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  if (/^\d+$/.test(value)) return Number(value);
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : Math.max(0, Math.ceil((at - Date.now()) / 1000));
}

/**
 * Unwrap a JSON envelope: the data on success, an `ApiError` on failure. A body that is not
 * a NEXA envelope (a proxy or gateway answered) is reported as NETWORK_ERROR.
 */
export async function parseEnvelope<T>(response: Response): Promise<ApiResponse<T>> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (isEnvelope(body)) {
    if (body.success) return { data: body.data as T, message: body.message, meta: body.meta };
    throw new ApiError(
      response.status,
      body.error,
      body.request_id,
      parseRetryAfter(response.headers.get("Retry-After")),
    );
  }
  // A proxy or gateway answered instead of NEXA.
  throw new ApiError(
    response.status,
    { code: "NETWORK_ERROR", message: `Unexpected response (${String(response.status)}).` },
    response.headers.get("X-Request-ID"),
  );
}

function isEnvelope(value: unknown): value is SuccessEnvelope<unknown> | ErrorEnvelope {
  return typeof value === "object" && value !== null && "success" in value;
}

/**
 * Exchange the refresh cookie for a new access token. Concurrent callers share one request.
 * Resolves to null (and clears the token) when the session is gone.
 */
export function refreshSession(): Promise<TokenPayload | null> {
  refreshInFlight ??= (async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/refresh/`, {
        method: "POST",
        headers: { [CSRF_HEADER]: "1" },
      });
      const { data } = await parseEnvelope<TokenPayload>(response);
      accessToken = data.access_token;
      return data;
    } catch {
      accessToken = null;
      return null;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

function toHeaderRecord(headers: HeadersInit | undefined): Record<string, string> {
  const record: Record<string, string> = {};
  if (!headers) return record;
  if (headers instanceof Headers) {
    headers.forEach((value, key) => {
      record[key] = value;
    });
  } else if (Array.isArray(headers)) {
    for (const [key, value] of headers) record[key] = value;
  } else {
    Object.assign(record, headers);
  }
  return record;
}

/** One request to the API: the caller's init plus the access token and CSRF header. */
function sendRequest(path: string, init: RequestInit, auth: boolean): Promise<Response> {
  const headers = toHeaderRecord(init.headers);
  if (auth && accessToken) headers.Authorization = `Bearer ${accessToken}`;
  if (path.startsWith("/auth/")) headers[CSRF_HEADER] = "1";
  return fetch(`${API_BASE}${path}`, { ...init, headers });
}

/**
 * What to do after a 401 whose body was `payload`: refresh the token, unless the session was
 * revoked. True when the request may be replayed with the new token; false when the session
 * is over (the token is cleared and `onSessionLost` is told). Requests that do not go through
 * `authorizedFetch` (the XHR uploads) use this to behave the same way.
 */
export async function recoverFromUnauthorized(payload: unknown): Promise<boolean> {
  const code = isEnvelope(payload) && !payload.success ? payload.error.code : "";
  if (code !== "AUTH_SESSION_REVOKED" && (await refreshSession())) return true;
  accessToken = null;
  sessionLostHandler?.();
  return false;
}

/**
 * `fetch` with the access token attached. A 401 (other than a revoked session) refreshes
 * the token once and replays the request; a failed refresh ends the session. The response
 * is returned as is, so callers decide how to read it (JSON envelope, event stream, ...).
 */
export async function authorizedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  let response = await sendRequest(path, init, true);
  if (response.status === 401) {
    const payload: unknown = await response
      .clone()
      .json()
      .catch(() => null);
    if (await recoverFromUnauthorized(payload)) response = await sendRequest(path, init, true);
  }
  return response;
}

export async function api<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<ApiResponse<T>> {
  const { method = "GET", body, auth = true, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const init: RequestInit = {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  };
  const response = auth ? await authorizedFetch(path, init) : await sendRequest(path, init, false);
  return parseEnvelope<T>(response);
}

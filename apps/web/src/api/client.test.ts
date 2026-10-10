import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, getAccessToken, onSessionLost, setAccessToken } from "./client";

type Call = { url: string; init: RequestInit };

function envelope(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const okUser = { success: true, data: { email: "ada@example.com" } };
const expired = {
  success: false,
  error: { code: "AUTH_TOKEN_EXPIRED", message: "expired" },
  request_id: "req_1",
};
const refreshed = { success: true, data: { access_token: "fresh", active_organization_id: null } };
const refreshFailed = {
  success: false,
  error: { code: "AUTH_SESSION_REVOKED", message: "gone" },
  request_id: "req_2",
};

let calls: Call[];
let responses: Response[];

beforeEach(() => {
  calls = [];
  responses = [];
  setAccessToken("stale");
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
  onSessionLost(null);
});

function call(index: number): Call {
  const found = calls[index];
  if (!found) throw new Error(`no request at index ${String(index)}`);
  return found;
}

function header(index: number, name: string): string | undefined {
  return (call(index).init.headers as Record<string, string>)[name];
}

describe("api", () => {
  it("attaches the access token and unwraps the envelope", async () => {
    responses.push(envelope(200, okUser));
    const { data } = await api<{ email: string }>("/users/me/");
    expect(data.email).toBe("ada@example.com");
    expect(header(0, "Authorization")).toBe("Bearer stale");
  });

  it("refreshes once on 401 and replays with the new token", async () => {
    responses.push(envelope(401, expired), envelope(200, refreshed), envelope(200, okUser));
    await api("/users/me/");
    expect(calls.map((c) => c.url)).toEqual([
      "/api/v1/users/me/",
      "/api/v1/auth/refresh/",
      "/api/v1/users/me/",
    ]);
    expect(header(1, "X-NEXA-CSRF")).toBe("1");
    expect(header(2, "Authorization")).toBe("Bearer fresh");
    expect(getAccessToken()).toBe("fresh");
  });

  it("shares one refresh between concurrent failures", async () => {
    responses.push(
      envelope(401, expired),
      envelope(401, expired),
      envelope(200, refreshed),
      envelope(200, okUser),
      envelope(200, okUser),
    );
    await Promise.all([api("/users/me/"), api("/organizations/")]);
    const refreshes = calls.filter((c) => c.url.endsWith("/auth/refresh/"));
    expect(refreshes).toHaveLength(1);
  });

  it("reports a lost session when the refresh fails", async () => {
    const lost = vi.fn();
    onSessionLost(lost);
    responses.push(envelope(401, expired), envelope(401, refreshFailed));
    await expect(api("/users/me/")).rejects.toBeInstanceOf(ApiError);
    expect(lost).toHaveBeenCalledOnce();
    expect(getAccessToken()).toBeNull();
  });

  it("exposes field errors from validation failures", async () => {
    responses.push(
      envelope(422, {
        success: false,
        error: {
          code: "VALIDATION_FAILED",
          message: "Some fields are invalid.",
          details: [{ field: "email", code: "invalid", message: "Enter a valid email address." }],
        },
        request_id: "req_3",
      }),
    );
    const error = await api("/auth/register/", { method: "POST", body: {}, auth: false }).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).fieldMessage("email")).toBe("Enter a valid email address.");
  });
});

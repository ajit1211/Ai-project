import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toolInfo } from "../test/chatFixtures";
import { ApiError, setAccessToken } from "./client";
import { listTools, setToolEnabled } from "./tools";

type Call = { url: string; init: RequestInit };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

let calls: Call[];
let responses: Response[];

beforeEach(() => {
  calls = [];
  responses = [];
  setAccessToken("token");
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
});

describe("the tool endpoints", () => {
  it("lists the tools", async () => {
    const tools = [toolInfo(), toolInfo({ id: "save_memory", name: "Save to memory", risk: "high" })];
    responses.push(json(200, { success: true, data: tools }));
    expect(await listTools()).toEqual(tools);
    expect(calls[0]?.url).toBe("/api/v1/tools/");
    expect(calls[0]?.init.method).toBe("GET");
  });

  it("switches a tool off, and reports a refusal", async () => {
    responses.push(
      json(200, { success: true, data: toolInfo({ enabled: false, disabled_by: "organization" }) }),
      json(403, {
        success: false,
        error: { code: "PERMISSION_DENIED", message: "You do not have permission to do that." },
        request_id: "req_3",
      }),
    );
    expect(await setToolEnabled("calculator", false)).toMatchObject({ enabled: false });
    expect(calls[0]?.url).toBe("/api/v1/tools/calculator/");
    expect(calls[0]?.init.method).toBe("PATCH");
    expect(JSON.parse(calls[0]?.init.body as string)).toEqual({ enabled: false });

    const refused = await setToolEnabled("calculator", true).catch((err: unknown) => err);
    expect(refused).toBeInstanceOf(ApiError);
    expect(refused).toMatchObject({ status: 403, code: "PERMISSION_DENIED" });
  });
});

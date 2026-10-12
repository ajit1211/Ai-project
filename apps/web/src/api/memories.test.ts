import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { setAccessToken } from "./client";
import { clearMemories, listMemories, searchMemories, updateMemorySettings } from "./memories";

type Call = { url: string; init: RequestInit };

let calls: Call[];
let next: Response;

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  calls = [];
  setAccessToken("token");
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit) => {
      calls.push({ url, init });
      return Promise.resolve(next);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("lists with the chosen filters and reads the paging meta", async () => {
  next = json({ success: true, data: [{ id: "m1" }], meta: { next_cursor: "abc", has_more: true } });
  const page = await listMemories({ status: "candidate", kind: "fact", q: "pune", limit: 10 });
  expect(calls[0]?.url).toBe("/api/v1/memories/?kind=fact&status=candidate&q=pune&limit=10");
  expect(page.data).toEqual([{ id: "m1" }]);
  expect(page.meta).toEqual({ next_cursor: "abc", has_more: true });

  next = json({ success: true, data: [], meta: { next_cursor: null, has_more: false } });
  const empty = await listMemories();
  expect(calls[1]?.url).toBe("/api/v1/memories/");
  expect(empty.meta).toEqual({ next_cursor: null, has_more: false });
});

it("searches, saves settings and clears", async () => {
  next = json({ success: true, data: [{ memory: { id: "m1" }, score: 2.5 }] });
  const hits = await searchMemories("django", 5);
  expect(calls[0]?.url).toBe("/api/v1/memories/search/?q=django&limit=5");
  expect(hits[0]?.score).toBe(2.5);

  next = json({ success: true, data: { extraction_enabled: false, auto_save: true, retention_days: null } });
  await updateMemorySettings({ extraction_enabled: false });
  expect(calls[1]?.init.method).toBe("PATCH");
  expect(JSON.parse(calls[1]?.init.body as string)).toEqual({ extraction_enabled: false });

  next = json({ success: true, data: { deleted: 4 } });
  expect(await clearMemories("candidates")).toBe(4);
  expect(calls[2]?.url).toBe("/api/v1/memories/clear/");
  expect(JSON.parse(calls[2]?.init.body as string)).toEqual({ scope: "candidates" });
});

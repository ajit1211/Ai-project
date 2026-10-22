import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { agent, agentDetail, orgAgent } from "../test/agentFixtures";
import { createAgent, deleteAgent, getAgent, listAgents, updateAgent } from "./agents";
import { ApiError, setAccessToken } from "./client";

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

const body = (index: number): unknown => JSON.parse(calls[index]?.init.body as string);

describe("the agent endpoints", () => {
  it("lists the agents, all of them or only the ones switched on", async () => {
    const agents = [agent(), orgAgent()];
    responses.push(json(200, { success: true, data: agents }), json(200, { success: true, data: [agent()] }));
    expect(await listAgents()).toEqual(agents);
    expect(calls[0]?.url).toBe("/api/v1/agents/");
    expect(calls[0]?.init.method).toBe("GET");
    expect(new Headers(calls[0]?.init.headers).get("Authorization")).toBe("Bearer token");

    await listAgents({ enabled: true });
    expect(calls[1]?.url).toBe("/api/v1/agents/?enabled=true");
  });

  it("reads one agent with its configuration", async () => {
    const detail = agentDetail();
    responses.push(json(200, { success: true, data: detail }));
    expect(await getAgent("agent-1")).toEqual(detail);
    expect(calls[0]?.url).toBe("/api/v1/agents/agent-1/");
  });

  it("creates an agent with the fields given", async () => {
    const created = agentDetail(orgAgent());
    responses.push(json(201, { success: true, data: created, message: "Agent created." }));
    const input = {
      name: "Release notes writer",
      instructions: "Draft release notes.",
      allowed_tools: ["calculator"],
      max_steps: 12,
      approval_policy: "all_tool_calls" as const,
      input_schema: null,
    };
    expect(await createAgent(input)).toEqual(created);
    expect(calls[0]?.url).toBe("/api/v1/agents/");
    expect(calls[0]?.init.method).toBe("POST");
    expect(body(0)).toEqual(input);
  });

  it("changes only the fields sent, and reports field errors", async () => {
    responses.push(
      json(200, { success: true, data: agentDetail(orgAgent({ name: "Notes writer", version: 4 })) }),
      json(422, {
        success: false,
        error: {
          code: "VALIDATION_FAILED",
          message: "Some fields are invalid.",
          details: [{ field: "max_steps", code: "max_value", message: "Ensure this value is less than or equal to 50." }],
        },
        request_id: "req_2",
      }),
    );
    expect(await updateAgent("agent-2", { name: "Notes writer" })).toMatchObject({ name: "Notes writer", version: 4 });
    expect(calls[0]?.url).toBe("/api/v1/agents/agent-2/");
    expect(calls[0]?.init.method).toBe("PATCH");
    expect(body(0)).toEqual({ name: "Notes writer" });

    const refused = await updateAgent("agent-2", { max_steps: 99 }).catch((err: unknown) => err);
    expect(refused).toBeInstanceOf(ApiError);
    expect((refused as ApiError).fieldMessage("max_steps")).toBe("Ensure this value is less than or equal to 50.");
  });

  it("deletes an agent", async () => {
    responses.push(json(200, { success: true, data: null, message: "Agent deleted." }));
    await deleteAgent("agent-2");
    expect(calls[0]?.url).toBe("/api/v1/agents/agent-2/");
    expect(calls[0]?.init.method).toBe("DELETE");
  });
});

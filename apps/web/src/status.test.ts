import { describe, expect, it } from "vitest";

import { summarize, type Probe, type ReadinessReport } from "./status";

const targets = {
  web: "http://localhost:5173",
  core: "http://localhost:8000",
  runtime: "http://localhost:8001",
};

const report = (service: string, checks: ReadinessReport["checks"]): Probe => ({
  reachable: true,
  report: { service, status: "ok", checks },
});

const healthyCore = report("core", {
  database: { status: "ok", latency_ms: 3 },
  redis: { status: "ok", latency_ms: 1 },
  celery: { status: "ok", workers: 1 },
});
const healthyRuntime = report("runtime", {
  database: { status: "ok", latency_ms: 4 },
  redis: { status: "ok", latency_ms: 2 },
});

const states = (core: Probe | undefined, runtime: Probe | undefined) =>
  Object.fromEntries(summarize(core, runtime, targets).map((row) => [row.name, row.state]));

describe("summarize", () => {
  it("shows all six parts running when both services report healthy", () => {
    const rows = summarize(healthyCore, healthyRuntime, targets);
    expect(rows.map((row) => row.state)).toEqual(["up", "up", "up", "up", "up", "up"]);
    expect(rows.find((row) => row.name === "Celery")?.detail).toBe("running · 1 worker");
  });

  it("waits for reports before judging dependencies", () => {
    expect(states(undefined, undefined)).toMatchObject({
      React: "up",
      Django: "waiting",
      PostgreSQL: "waiting",
      Celery: "waiting",
    });
  });

  it("still reports PostgreSQL through FastAPI when Django is down", () => {
    const down: Probe = { reachable: false, error: "fetch failed" };
    expect(states(down, healthyRuntime)).toMatchObject({
      Django: "down",
      FastAPI: "up",
      PostgreSQL: "up",
      Celery: "waiting",
    });
  });

  it("marks Redis and Celery down when Redis is unreachable", () => {
    const core = report("core", {
      database: { status: "ok" },
      redis: { status: "error", error: "ConnectionError" },
      celery: { status: "skipped", reason: "redis unavailable" },
    });
    const rows = summarize(core, healthyRuntime, targets);
    expect(rows.find((row) => row.name === "Redis")).toMatchObject({
      state: "down",
      detail: "core: ConnectionError",
    });
    expect(rows.find((row) => row.name === "Celery")).toMatchObject({
      state: "down",
      detail: "redis unavailable",
    });
  });
});

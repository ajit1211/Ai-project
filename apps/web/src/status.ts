/** Turns the Core and Runtime readiness reports into one row per part of the local stack. */

export interface DependencyCheck {
  status: "ok" | "error" | "skipped";
  latency_ms?: number;
  error?: string;
  detail?: string;
  reason?: string;
  workers?: number;
}

export interface ReadinessReport {
  service: string;
  status: "ok" | "degraded" | "unavailable";
  checks: Partial<Record<string, DependencyCheck>>;
}

export type Probe = { reachable: true; report: ReadinessReport } | { reachable: false; error: string };

export type RowState = "up" | "down" | "waiting";

export interface StatusRow {
  name: string;
  state: RowState;
  detail: string;
}

export interface Targets {
  web: string;
  core: string;
  runtime: string;
}

const PROBE_TIMEOUT_MS = 4000;

/** Fetch a /readyz report. A 503 still carries a report; only a missing service is unreachable. */
export async function probe(url: string): Promise<Probe> {
  try {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    const body: unknown = await response.json();
    return isReadinessReport(body)
      ? { reachable: true, report: body }
      : { reachable: false, error: `unexpected response (${String(response.status)})` };
  } catch (error) {
    return { reachable: false, error: error instanceof Error ? error.message : String(error) };
  }
}

export function summarize(
  core: Probe | undefined,
  runtime: Probe | undefined,
  targets: Targets,
): StatusRow[] {
  return [
    { name: "React", state: "up", detail: targets.web },
    serviceRow("Django", core, targets.core),
    serviceRow("FastAPI", runtime, targets.runtime),
    dependencyRow("PostgreSQL", "database", [core, runtime]),
    dependencyRow("Redis", "redis", [core, runtime]),
    celeryRow(core),
  ];
}

function serviceRow(name: string, probe: Probe | undefined, url: string): StatusRow {
  if (!probe) return { name, state: "waiting", detail: `checking ${url}` };
  return probe.reachable
    ? { name, state: "up", detail: url }
    : { name, state: "down", detail: `${url} is not responding` };
}

/** Both services check the database and Redis; any failure reported by either one wins. */
function dependencyRow(
  name: string,
  check: "database" | "redis",
  probes: (Probe | undefined)[],
): StatusRow {
  const results = probes.flatMap((probe) =>
    probe?.reachable ? [{ service: probe.report.service, result: probe.report.checks[check] }] : [],
  );
  if (results.length === 0) return { name, state: "waiting", detail: "waiting for Django or FastAPI" };

  const failed = results.find(({ result }) => result?.status !== "ok");
  if (failed) {
    return { name, state: "down", detail: `${failed.service}: ${describeFailure(failed.result)}` };
  }
  const slowest = Math.max(...results.map(({ result }) => result?.latency_ms ?? 0));
  return { name, state: "up", detail: `running · ${String(slowest)} ms` };
}

function celeryRow(core: Probe | undefined): StatusRow {
  const name = "Celery";
  if (!core?.reachable) return { name, state: "waiting", detail: "waiting for Django" };

  const check = core.report.checks.celery;
  if (check?.status === "ok") {
    const workers = check.workers ?? 0;
    return { name, state: "up", detail: `running · ${String(workers)} worker${workers === 1 ? "" : "s"}` };
  }
  return { name, state: "down", detail: describeFailure(check) };
}

function describeFailure(check: DependencyCheck | undefined): string {
  if (!check) return "not reported";
  if (check.status === "skipped") return check.reason ?? "skipped";
  return check.detail ?? check.error ?? "error";
}

function isReadinessReport(value: unknown): value is ReadinessReport {
  return typeof value === "object" && value !== null && "service" in value && "checks" in value;
}

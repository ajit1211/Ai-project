/** The local-stack status page from Phase 02, kept at /status for development. */

import { useEffect, useState } from "react";

import { probe, summarize, type Probe, type RowState } from "../status";

const POLL_INTERVAL_MS = 5000;

const TARGETS = {
  web: window.location.origin,
  core: __NEXA_CORE_URL__,
  runtime: __NEXA_RUNTIME_URL__,
};

const LABELS: Record<RowState, string> = { up: "Running", down: "Down", waiting: "Checking" };

export function StatusPage() {
  const [core, setCore] = useState<Probe>();
  const [runtime, setRuntime] = useState<Probe>();
  const [checkedAt, setCheckedAt] = useState<Date>();

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      const [coreProbe, runtimeProbe] = await Promise.all([
        probe("/__core/readyz"),
        probe("/__runtime/readyz"),
      ]);
      if (cancelled) return;
      setCore(coreProbe);
      setRuntime(runtimeProbe);
      setCheckedAt(new Date());
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const rows = summarize(core, runtime, TARGETS);
  const running = rows.filter((row) => row.state === "up").length;

  return (
    <main className="page narrow">
      <header className="header">
        <h1>NEXA</h1>
        <p className="subtitle">Local development stack</p>
      </header>
      <section className="card" aria-labelledby="stack-heading">
        <div className="card-header">
          <h2 id="stack-heading">
            {running} of {rows.length} running
          </h2>
          <span className="checked" aria-live="polite">
            {checkedAt ? `Checked ${checkedAt.toLocaleTimeString()}` : "Checking…"}
          </span>
        </div>
        <ul className="rows">
          {rows.map((row) => (
            <li key={row.name} className="row">
              <span className={`dot dot-${row.state}`} aria-hidden="true" />
              <span className="name">{row.name}</span>
              <span className="detail" title={row.detail}>
                {row.detail}
              </span>
              <span className={`badge badge-${row.state}`}>{LABELS[row.state]}</span>
            </li>
          ))}
        </ul>
      </section>
      <footer className="footer">
        Refreshes every 5 seconds · <a href={`${TARGETS.runtime}/docs`}>Runtime API docs</a> ·{" "}
        <a href={`${TARGETS.core}/admin/`}>Django admin</a> · <a href="/login">Sign in</a>
      </footer>
    </main>
  );
}

/**
 * One task (`/agents/runs/:runId`), followed live: the agent and its version, where the task
 * stands and what it is doing (announced as it changes), the goal, the controls the server
 * allows now (pause, resume, cancel after a confirmation), how much of its budget it has used,
 * the action it waits for (approve or decline the exact action shown), every step so far, and
 * once it is over, the answer with its sources and structured output, or why it did not
 * finish.
 *
 * A task belongs to the organization it was started in: switching organization leaves for
 * the task history.
 */

import { Component, lazy, Suspense, useEffect, useRef, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useAuth } from "../auth/AuthContext";
import { ConfirmAction } from "../components/agents/ConfirmAction";
import { RunStatusBadge } from "../components/agents/badges";
import { describeRunFailure, formatDateTime, goalExcerpt, isFinal } from "../components/agents/labels";
import { RunBudget } from "../components/agents/RunBudget";
import { RunTimeline } from "../components/agents/RunTimeline";
import { ApprovalCard } from "../components/chat/ApprovalCard";
import { Banner } from "../components/chat/Banner";
import { formatCount } from "../components/chat/format";
import { Icon } from "../components/chat/Icon";
import { Alert } from "../components/forms";
import { useClock } from "../hooks/useClock";
import { useRun, type RunDetail } from "../hooks/useRun";
import type { Run } from "../types/agents";

// The answer brings Markdown and syntax highlighting: loaded only once a task has one, as the
// chat is (ChatRoute.tsx), so the rest of the app does not carry them.
const RunResult = lazy(() =>
  import("../components/agents/RunResult").then((module) => ({ default: module.RunResult })),
);

export function RunPage() {
  const { runId = "" } = useParams();
  const { activeOrganizationId } = useAuth();
  const navigate = useNavigate();
  const shown = useRef(activeOrganizationId);

  useEffect(() => {
    if (shown.current === activeOrganizationId) return;
    shown.current = activeOrganizationId;
    void navigate("/agents/runs", { replace: true });
  }, [activeOrganizationId, navigate]);

  return <RunView key={runId} runId={runId} />;
}

function BackLink() {
  return (
    <p className="doc-back">
      <Link to="/agents/runs" className="doc-icon-link">
        <Icon name="back" />
        All tasks
      </Link>
    </p>
  );
}

function RunView({ runId }: { runId: string }) {
  const detail = useRun(runId);
  const { run } = detail;
  const status = useRef<HTMLDivElement>(null);

  if (!run) {
    let body = <p className="muted">Loading task…</p>;
    if (detail.missing) {
      body = (
        <>
          <h1>Task not found</h1>
          <p className="muted">It may not exist, or it was started by someone else.</p>
        </>
      );
    } else if (detail.loadError) {
      body = (
        <>
          <h1>Task</h1>
          <Alert kind="error">{detail.loadError}</Alert>
          <button type="button" className="btn" onClick={detail.reload}>
            Try again
          </button>
        </>
      );
    }
    return (
      <div className="agents-page run-page">
        <BackLink />
        {body}
      </div>
    );
  }

  const over = isFinal(run.status);
  const title = goalExcerpt(run.goal, 120) || "Task";
  /** After a control or a decision, focus goes to the status, which says what happened. */
  const focusStatus = () => {
    status.current?.focus();
  };

  return (
    <div className="agents-page run-page">
      <BackLink />
      <header className="run-header">
        <div className="run-title">
          <p className="muted small run-agent">
            {run.agent_name || "Deleted agent"} · version {run.agent_version}
          </p>
          <h1>{title}</h1>
        </div>
        <Link to={`/agents/${encodeURIComponent(run.agent_id)}/run`} className="btn btn-quiet">
          Run this agent again
        </Link>
      </header>

      <div ref={status} tabIndex={-1} className="run-status" aria-live="polite" aria-atomic="true">
        <RunStatusBadge status={run.status} />
        {run.progress_message && <span className="run-progress">{run.progress_message}</span>}
        {run.pause_requested && run.status === "running" && (
          <span className="run-progress">It will pause at its next checkpoint.</span>
        )}
      </div>

      {title !== run.goal.trim() && (
        <details className="run-goal-full">
          <summary>Show the whole goal</summary>
          <p className="run-goal">{run.goal}</p>
        </details>
      )}
      <RunInput input={run.input} />

      <Controls detail={detail} run={run} onDone={focusStatus} />
      {detail.actionError && <Banner message={detail.actionError} onDismiss={detail.dismissActionError} />}

      <div className="run-layout">
        <div className="run-main">
          {run.status === "waiting_for_approval" && run.pending_approval && (
            <ApprovalCard
              approval={run.pending_approval}
              busy={detail.deciding !== null}
              deciding={detail.deciding ?? undefined}
              rejectVerb="Decline"
              onDecide={(choice) => {
                void detail.decide(choice).then((decided) => {
                  if (decided) focusStatus();
                });
              }}
            />
          )}
          {run.status === "completed" && (
            <ResultFailure>
              <Suspense fallback={<p className="muted">Loading the answer…</p>}>
                <RunResult run={run} />
              </Suspense>
            </ResultFailure>
          )}
          {over && run.status !== "completed" && <Failure run={run} />}
          <RunTimeline steps={detail.steps} running={!over} />
        </div>
        <aside className="run-side" aria-label="Budget and details">
          <Budget run={run} />
          <Details run={run} />
        </aside>
      </div>

      {detail.mode === "polling" && (
        <p className="muted small run-mode">Live updates are unavailable; checking for changes every few seconds.</p>
      )}
      <div className="visually-hidden" aria-live="polite">
        {detail.notice}
      </div>
    </div>
  );
}

/** The task's structured input, when it has any: plain text, as the user typed it. */
function RunInput({ input }: { input: Run["input"] }) {
  const entries = Object.entries(input);
  if (entries.length === 0) return null;
  return (
    <dl className="tool-facts run-input">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Controls({ detail, run, onDone }: { detail: RunDetail; run: Run; onDone: () => void }) {
  if (!run.can_pause && !run.can_resume && !run.can_cancel) return null;
  const busy = detail.busy !== null;
  const act = (action: () => Promise<boolean>) => {
    void action().then((done) => {
      if (done) onDone();
    });
  };
  return (
    <div className="run-controls" role="group" aria-label="Task controls">
      {run.can_pause && (
        <button
          type="button"
          className="btn agent-icon-button"
          disabled={busy}
          onClick={() => {
            act(detail.pause);
          }}
        >
          <Icon name="pause" />
          {detail.busy === "pause" ? "Pausing…" : "Pause"}
        </button>
      )}
      {run.can_resume && (
        <button
          type="button"
          className="btn btn-primary agent-icon-button"
          disabled={busy}
          onClick={() => {
            act(detail.resume);
          }}
        >
          <Icon name="play" />
          {detail.busy === "resume" ? "Resuming…" : "Resume"}
        </button>
      )}
      {run.can_cancel && (
        <ConfirmAction
          label="Cancel task"
          icon={<Icon name="stop" />}
          question="Cancel this task? It stops and cannot be resumed."
          confirm="Cancel task"
          busyText="Cancelling…"
          dismiss="Keep it running"
          disabled={busy}
          onConfirm={() =>
            detail.cancel().then((done) => {
              if (done) onDone();
              return done;
            })
          }
        />
      )}
    </div>
  );
}

interface ResultFailureState {
  failed: boolean;
}

/**
 * Should the answer's code fail to load (replaced by a new deployment, the network gone), the
 * rest of the task page stays, and says so.
 */
class ResultFailure extends Component<{ children: ReactNode }, ResultFailureState> {
  override state: ResultFailureState = { failed: false };

  static getDerivedStateFromError(): ResultFailureState {
    return { failed: true };
  }

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return <Alert kind="error">The answer could not be shown. Check your connection, then reload the page.</Alert>;
  }
}

function Failure({ run }: { run: Run }) {
  const failure = describeRunFailure(run);
  return (
    <section className={`run-failure run-failure-${run.status}`} aria-label="Why the task did not finish">
      <p className="run-failure-summary">{failure.summary}</p>
      {failure.detail && <p className="run-failure-detail">{failure.detail}</p>}
    </section>
  );
}

function Budget({ run }: { run: Run }) {
  // The clock runs from the deadline on, queued again after a pause or an approval included.
  const counting = !isFinal(run.status) && run.deadline_at !== null && run.time_left_seconds === null;
  const now = useClock(counting);
  return (
    <section className="card section run-budget-card" aria-labelledby="run-budget-title">
      <h2 id="run-budget-title">Budget</h2>
      <RunBudget run={run} now={now} />
    </section>
  );
}

function Details({ run }: { run: Run }) {
  const { usage } = run;
  return (
    <section className="card section" aria-labelledby="run-details-title">
      <h2 id="run-details-title">Details</h2>
      <dl className="doc-facts">
        <Fact label="Started">{formatDateTime(run.created_at)}</Fact>
        {run.completed_at && <Fact label="Ended">{formatDateTime(run.completed_at)}</Fact>}
        <Fact label="Model">{run.model || "Organization default"}</Fact>
        <Fact label="Model calls">{formatCount(usage.model_calls)}</Fact>
        <Fact label="Tokens">
          {formatCount(usage.input_tokens)} in, {formatCount(usage.output_tokens)} out
        </Fact>
        {usage.cost_usd !== null && <Fact label="Cost">${usage.cost_usd}</Fact>}
      </dl>
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="doc-fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

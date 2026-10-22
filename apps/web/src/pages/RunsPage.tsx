/**
 * The task history (`/agents/runs`): the caller's tasks, newest first, each with its agent,
 * its goal (a link to the task), where it stands, how many steps it took and when it was
 * started. Filters narrow it by status (in progress, finished, or one status) and by agent;
 * "Load more" pages on. Each organization has its own tasks, so switching organization starts
 * the page afresh.
 */

import { useEffect, useId, useState } from "react";
import { Link } from "react-router";

import { listAgents } from "../api/agents";
import { useAuth } from "../auth/AuthContext";
import { RunStatusBadge } from "../components/agents/badges";
import { formatDateTime, goalExcerpt } from "../components/agents/labels";
import { Banner } from "../components/chat/Banner";
import { formatCount } from "../components/chat/format";
import { Icon } from "../components/chat/Icon";
import { DEFAULT_RUN_FILTERS, useRuns, type RunFilters, type RunHistory } from "../hooks/useRuns";
import type { Agent, Run } from "../types/agents";

const STATUS_GROUPS: { value: RunFilters["status"]; label: string }[] = [
  { value: "", label: "All tasks" },
  { value: "active", label: "In progress" },
  { value: "finished", label: "Finished" },
];

const STATUSES: { value: RunFilters["status"]; label: string }[] = [
  { value: "queued", label: "Queued" },
  { value: "running", label: "Running" },
  { value: "waiting_for_approval", label: "Needs your approval" },
  { value: "paused", label: "Paused" },
  { value: "cancel_requested", label: "Cancelling" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
  { value: "timed_out", label: "Timed out" },
];

export function RunsPage() {
  const { activeOrganizationId } = useAuth();
  return <History key={activeOrganizationId ?? "none"} />;
}

/** The agents for the filter; an empty list when they cannot be loaded (the filter then offers none). */
function useAgentChoices(): Agent[] {
  const [agents, setAgents] = useState<Agent[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    listAgents({}, { signal: controller.signal })
      .then((list) => {
        if (!controller.signal.aborted) setAgents(list);
      })
      .catch(() => {
        // The history works without the filter.
      });
    return () => {
      controller.abort();
    };
  }, []);
  return agents;
}

function History() {
  const history = useRuns();
  const agents = useAgentChoices();

  return (
    <div className="agents-page runs-page">
      <p className="doc-back">
        <Link to="/agents" className="doc-icon-link">
          <Icon name="back" />
          All agents
        </Link>
      </p>
      <header className="header agents-header">
        <div className="agents-title">
          <h1>Task history</h1>
          <p className="muted">The tasks you started, newest first. Open one to follow it or to read its result.</p>
        </div>
        <div className="agents-header-actions">
          <button type="button" className="btn agent-icon-button" onClick={history.reload}>
            <Icon name="refresh" />
            Refresh
          </button>
        </div>
      </header>

      {history.error && (
        <Banner
          message={history.error}
          onDismiss={history.dismissError}
          onRetry={history.failed ? history.reload : undefined}
        />
      )}

      <Filters history={history} agents={agents} />
      <RunList history={history} />
    </div>
  );
}

function Filters({ history, agents }: { history: RunHistory; agents: readonly Agent[] }) {
  const id = useId();
  const { filters, setFilters } = history;
  return (
    <div className="doc-toolbar runs-toolbar" role="search" aria-label="Filter tasks">
      <label htmlFor={`${id}-status`} className="visually-hidden">
        Filter by status
      </label>
      <select
        id={`${id}-status`}
        value={filters.status}
        onChange={(event) => {
          setFilters({ status: event.target.value as RunFilters["status"] });
        }}
      >
        {STATUS_GROUPS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
        <optgroup label="One status">
          {STATUSES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </optgroup>
      </select>
      <label htmlFor={`${id}-agent`} className="visually-hidden">
        Filter by agent
      </label>
      <select
        id={`${id}-agent`}
        value={filters.agentId}
        onChange={(event) => {
          setFilters({ agentId: event.target.value });
        }}
      >
        <option value="">All agents</option>
        {agents.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.name}
          </option>
        ))}
        {filters.agentId && !agents.some((agent) => agent.id === filters.agentId) && (
          <option value={filters.agentId}>This agent</option>
        )}
      </select>
    </div>
  );
}

function RunList({ history }: { history: RunHistory }) {
  const filtered =
    history.filters.status !== DEFAULT_RUN_FILTERS.status || history.filters.agentId !== DEFAULT_RUN_FILTERS.agentId;

  let empty = null;
  if (history.loading && history.runs.length === 0) {
    empty = <p className="muted agents-empty">Loading tasks…</p>;
  } else if (!history.loading && history.failed) {
    empty = <p className="muted agents-empty">The tasks could not be loaded.</p>;
  } else if (!history.loading && history.runs.length === 0) {
    empty = filtered ? (
      <div className="agents-empty">
        <p className="muted">No tasks match these filters.</p>
        <button
          type="button"
          className="btn btn-quiet"
          onClick={() => {
            history.setFilters(DEFAULT_RUN_FILTERS);
          }}
        >
          Clear filters
        </button>
      </div>
    ) : (
      <div className="agents-empty">
        <p>No tasks yet.</p>
        <p className="muted">
          Start one from the <Link to="/agents">agent library</Link>: pick an agent and press Run.
        </p>
      </div>
    );
  }

  return (
    <div aria-busy={history.loading}>
      {empty}
      {!history.failed && history.runs.length > 0 && (
        <ul className="run-list" aria-label="Tasks">
          {history.runs.map((run) => (
            <RunItem key={run.id} run={run} />
          ))}
        </ul>
      )}
      {history.hasMore && (
        <button type="button" className="btn doc-more" onClick={history.loadMore} disabled={history.loadingMore}>
          {history.loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}

function RunItem({ run }: { run: Run }) {
  const goal = goalExcerpt(run.goal) || "(no goal)";
  return (
    <li className="run-item">
      <div className="run-item-main">
        <Link to={`/agents/runs/${encodeURIComponent(run.id)}`} className="run-item-goal">
          {goal}
        </Link>
        <p className="muted small run-item-meta">
          <span>{run.agent_name || "Deleted agent"}</span>
          <span>
            {formatCount(run.step_count)} of {formatCount(run.limits.max_steps)} steps
          </span>
          <span>
            Started <time dateTime={run.created_at}>{formatDateTime(run.created_at)}</time>
          </span>
        </p>
      </div>
      <RunStatusBadge status={run.status} />
    </li>
  );
}

/**
 * The agent library (`/agents`): the agents NEXA ships and the organization's own, each with
 * what it is for, the tools it may use (how risky each is, whether it asks first), its limits
 * and its version. Any agent that is switched on can be run; the organization's agents can be
 * edited or deleted by whoever the server allows. The Document Assistant comes first, as the
 * place to start. Each organization has its own agents, so switching organization starts the
 * page afresh.
 */

import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { useAuth } from "../auth/AuthContext";
import { ScopeBadge, ToolBadges } from "../components/agents/badges";
import { isDocumentAssistant, limitsSummary } from "../components/agents/labels";
import { Banner } from "../components/chat/Banner";
import { Icon } from "../components/chat/Icon";
import { ConfirmDelete } from "../components/documents/ConfirmDelete";
import { Alert } from "../components/forms";
import { useAgents, type AgentLibrary } from "../hooks/useAgents";
import type { Agent } from "../types/agents";

const isStartHere = (agent: Agent): boolean => isDocumentAssistant(agent) && agent.enabled;

/** The name of an agent saved on its form, handed over in the navigation state. */
function savedName(state: unknown): string | null {
  if (typeof state !== "object" || state === null || !("saved" in state)) return null;
  return typeof state.saved === "string" ? state.saved : null;
}

export function AgentsPage() {
  const { activeOrganizationId } = useAuth();
  return <Library key={activeOrganizationId ?? "none"} />;
}

function Library() {
  const library = useAgents();
  const location = useLocation();
  const navigate = useNavigate();
  const [saved] = useState(() => savedName(location.state));
  const heading = useRef<HTMLHeadingElement>(null);

  // Said once: the history entry forgets it, so a reload does not say it again.
  useEffect(() => {
    if (saved) void navigate("/agents", { replace: true, state: null });
  }, [saved, navigate]);

  return (
    <div className="agents-page">
      <header className="header agents-header">
        <div className="agents-title">
          <h1 ref={heading} tabIndex={-1}>
            Agents
          </h1>
          <p className="muted">
            An agent carries out a task for you, step by step: it searches, looks things up and
            uses the tools it is allowed, within the limits it is given, and asks you before any
            action that needs your approval.
          </p>
        </div>
        <div className="agents-header-actions">
          <Link to="/agents/runs" className="btn">
            Task history
          </Link>
          <Link to="/agents/new" className="btn btn-primary agent-icon-button">
            <Icon name="plus" />
            New agent
          </Link>
        </div>
      </header>

      {saved && <Alert kind="success">“{saved}” was saved.</Alert>}
      {library.error && (
        <Banner
          message={library.error}
          onDismiss={library.dismissError}
          onRetry={library.failed ? library.reload : undefined}
        />
      )}

      <AgentList
        library={library}
        onDeleted={() => {
          // The deleted card took the focused button with it.
          heading.current?.focus();
        }}
      />

      <div className="visually-hidden" aria-live="polite">
        {library.notice}
      </div>
    </div>
  );
}

function AgentList({ library, onDeleted }: { library: AgentLibrary; onDeleted: () => void }) {
  const first = library.agents.find(isStartHere);
  const agents = first ? [first, ...library.agents.filter((agent) => agent !== first)] : library.agents;

  let empty = null;
  if (library.loading && agents.length === 0) {
    empty = <p className="muted agents-empty">Loading agents…</p>;
  } else if (library.failed && agents.length === 0) {
    empty = <p className="muted agents-empty">The agents could not be loaded.</p>;
  } else if (agents.length === 0) {
    empty = (
      <div className="agents-empty">
        <p>No agents yet.</p>
        <p className="muted">Create one with New agent: give it instructions and the tools it may use.</p>
      </div>
    );
  }

  return (
    <div aria-busy={library.loading}>
      {empty}
      {agents.length > 0 && (
        <ul className="agent-list" aria-label="Agents">
          {agents.map((agent) => (
            <AgentCard
              key={agent.id}
              agent={agent}
              startHere={agent === first}
              library={library}
              onDeleted={onDeleted}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function AgentCard({
  agent,
  startHere,
  library,
  onDeleted,
}: {
  agent: Agent;
  startHere: boolean;
  library: AgentLibrary;
  onDeleted: () => void;
}) {
  const extras = [
    agent.has_input_schema ? "Takes structured input." : null,
    agent.has_output_schema ? "Answers with structured output." : null,
    agent.model ? `Model: ${agent.model}.` : null,
  ].filter((extra): extra is string => extra !== null);

  return (
    <li className={`agent-card${startHere ? " agent-card-start" : ""}`}>
      {startHere && <p className="agent-start">Start here</p>}
      <div className="agent-card-head">
        <h2 className="agent-name">{agent.name}</h2>
        <ScopeBadge scope={agent.scope} />
        {!agent.enabled && <span className="badge badge-neutral agent-badge">Switched off</span>}
        <span className="muted small">Version {agent.version}</span>
      </div>
      {agent.description && <p className="agent-description">{agent.description}</p>}
      {startHere && (
        <p className="small agent-start-hint">
          Ask it a question about your documents: it searches them and answers with citations to
          the passages it used.
        </p>
      )}
      <ToolBadges tools={agent.tools} policy={agent.approval_policy} />
      <p className="muted small agent-limits">
        {limitsSummary(agent.limits)}
        {extras.length > 0 && ` ${extras.join(" ")}`}
      </p>
      <div className="agent-actions">
        {agent.enabled && (
          <Link to={`/agents/${agent.id}/run`} className="btn btn-primary" aria-label={`Run ${agent.name}`}>
            Run
          </Link>
        )}
        {agent.can_edit && (
          <Link to={`/agents/${agent.id}/edit`} className="btn btn-quiet" aria-label={`Edit ${agent.name}`}>
            Edit
          </Link>
        )}
        {agent.can_delete && (
          <ConfirmDelete
            label={`Delete ${agent.name}`}
            question={`Delete “${agent.name}”? The tasks it carried out stay in their history.`}
            onConfirm={() =>
              library.remove(agent).then((removed) => {
                if (removed) onDeleted();
                return removed;
              })
            }
          />
        )}
      </div>
    </li>
  );
}

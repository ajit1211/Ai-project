/**
 * The small marks of agents and tasks: where a task stands, where an agent comes from, and
 * the tools an agent may use with how risky each is and whether it asks first.
 */

import type { AgentScope, AgentTool, ApprovalPolicy } from "../../types/agents";
import { riskLabel } from "../chat/tools";
import { runStatusLabel, runStatusTone } from "./labels";

/** A task status as a coloured badge: blue while it works, amber while it waits, green or red once over. */
export function RunStatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${runStatusTone(status)} agent-badge`}>{runStatusLabel(status)}</span>;
}

/** "Platform" for the agents NEXA ships, "Organization" for the organization's own. */
export function ScopeBadge({ scope }: { scope: AgentScope }) {
  return scope === "platform" ? (
    <span className="badge badge-info agent-badge" title="Made by NEXA; it cannot be changed">
      Platform
    </span>
  ) : (
    <span className="badge badge-neutral agent-badge" title="Made in your organization">
      Organization
    </span>
  );
}

const RISK_TONES: Readonly<Record<string, string>> = { low: "up", medium: "waiting", high: "down" };

/**
 * The agent's tools, each with its risk and, when it will wait for the user's approval, a
 * "Needs approval" mark (every tool has one under the "all_tool_calls" policy).
 */
export function ToolBadges({ tools, policy }: { tools: readonly AgentTool[]; policy: ApprovalPolicy }) {
  if (tools.length === 0) {
    return <p className="muted small agent-no-tools">No tools: it answers from the goal alone.</p>;
  }
  return (
    <ul className="agent-tools" aria-label="Tools">
      {tools.map((tool) => {
        const risk = riskLabel(tool.risk);
        const asks = tool.requires_approval || policy === "all_tool_calls";
        return (
          <li key={tool.id} className="agent-tool">
            <span className="agent-tool-name">{tool.name}</span>
            {risk && (
              <span className={`badge badge-${RISK_TONES[tool.risk] ?? "neutral"} agent-badge`}>{risk}</span>
            )}
            {asks && <span className="badge badge-waiting agent-badge">Needs approval</span>}
          </li>
        );
      })}
    </ul>
  );
}

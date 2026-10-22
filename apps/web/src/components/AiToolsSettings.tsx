/**
 * The organization's AI tools, each with an on/off switch, for members who may manage them
 * (`ai.tool.manage`). The list and the switches act on the active organization: the access
 * token names it, so the organization page shows this only for that one. A tool the platform
 * switched off cannot be switched on here. The server decides; a refusal is shown as sent.
 */

import { useEffect, useState } from "react";

import { listTools, setToolEnabled } from "../api/tools";
import type { ToolInfo } from "../types/tools";
import { riskLabel } from "./chat/tools";
import { Alert, describeError } from "./forms";

export function AiToolsSettings() {
  const [tools, setTools] = useState<ToolInfo[] | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listTools({ signal: controller.signal })
      .then((found) => {
        setTools(found);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setFeedback({ kind: "error", text: describeError(err) });
      });
    return () => {
      controller.abort();
    };
  }, []);

  const toggle = (tool: ToolInfo, enabled: boolean) => {
    setSaving(tool.id);
    setFeedback(null);
    setToolEnabled(tool.id, enabled)
      .then((updated) => {
        setTools((all) => all?.map((item) => (item.id === updated.id ? updated : item)) ?? all);
        setFeedback({
          kind: "success",
          text: `${updated.name} is ${updated.enabled ? "on" : "off"} for this organization.`,
        });
      })
      .catch((err: unknown) => {
        setFeedback({ kind: "error", text: describeError(err) });
      })
      .finally(() => {
        setSaving(null);
      });
  };

  return (
    <section className="card section" aria-labelledby="ai-tools-title">
      <h2 id="ai-tools-title">AI tools</h2>
      <p className="muted small">
        What the assistant may use in ordinary chat. Actions that change data always ask the user first.
      </p>
      {feedback && <Alert kind={feedback.kind}>{feedback.text}</Alert>}
      {tools === null ? (
        !feedback && <p className="muted">Loading…</p>
      ) : (
        <ul className="list" aria-label="AI tools">
          {tools.map((tool) => {
            const risk = riskLabel(tool.risk);
            const lockedOff = tool.disabled_by === "platform";
            return (
              <li key={tool.id} className="list-row tool-setting">
                <div>
                  <div className="tool-setting-name">
                    <strong>{tool.name}</strong>
                    {risk && (
                      <span className={`badge ${tool.risk === "high" ? "badge-down" : "badge-neutral"}`}>{risk}</span>
                    )}
                    {tool.requires_approval && <span className="badge badge-waiting">Asks for approval</span>}
                  </div>
                  <div className="muted small">{tool.description}</div>
                  {lockedOff && <div className="muted small">Switched off for the whole platform.</div>}
                </div>
                <label className="check tool-switch">
                  <input
                    type="checkbox"
                    role="switch"
                    checked={tool.enabled}
                    disabled={lockedOff || saving === tool.id}
                    aria-label={`Let AI use ${tool.name}`}
                    onChange={(event) => {
                      toggle(tool, event.target.checked);
                    }}
                  />
                  <span aria-hidden="true">{tool.enabled ? "On" : "Off"}</span>
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

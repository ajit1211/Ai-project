/**
 * Creating an agent (`/agents/new`) and changing one (`/agents/:agentId/edit`): its name and
 * description, the instructions it follows, the model, the tools it may use (only the ones
 * switched on here and available to you, each with its risk and whether it asks first), its
 * limits, when it asks for approval, and optional JSON Schemas for a task's input and the
 * answer's structured output.
 *
 * The form checks what it can before sending (required fields, ranges, schemas that are JSON
 * objects); the server checks everything again, and its field errors are shown next to their
 * fields, a refused tool under the tools. After a failed check, focus moves to the first field
 * with a problem. Saving returns to the library.
 */

import { useEffect, useEffectEvent, useId, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { createAgent, getAgent, updateAgent } from "../api/agents";
import { ApiError } from "../api/client";
import { listTools } from "../api/tools";
import { useAuth } from "../auth/AuthContext";
import {
  DESCRIPTION_MAX,
  DRAFT_FIELDS,
  INSTRUCTIONS_MAX,
  LIMIT_RULES,
  NAME_MAX,
  changedFields,
  checkDraft,
  draftFrom,
  isDraftField,
  type AgentDraft,
  type DraftErrors,
  type DraftField,
} from "../components/agents/agentDraft";
import { FormField } from "../components/agents/FormField";
import { describeAgentError } from "../components/agents/labels";
import { Icon } from "../components/chat/Icon";
import { riskLabel } from "../components/chat/tools";
import { Alert } from "../components/forms";
import { useModels } from "../hooks/useModels";
import type { AgentDetail, AgentTool } from "../types/agents";
import type { ToolInfo } from "../types/tools";

const CREATE_FORBIDDEN = "You do not have permission to create agents in this organization.";
const EDIT_FORBIDDEN = "You cannot change this agent.";

export function AgentFormPage() {
  const { agentId } = useParams();
  const { activeOrganizationId } = useAuth();
  const navigate = useNavigate();
  const shown = useRef(activeOrganizationId);

  // An agent belongs to the organization it was opened in: switching leaves for the library.
  useEffect(() => {
    if (shown.current === activeOrganizationId) return;
    shown.current = activeOrganizationId;
    void navigate("/agents", { replace: true });
  }, [activeOrganizationId, navigate]);

  return agentId ? <EditAgent key={agentId} id={agentId} /> : <AgentForm agent={null} />;
}

function BackLink() {
  return (
    <p className="doc-back">
      <Link to="/agents" className="doc-icon-link">
        <Icon name="back" />
        All agents
      </Link>
    </p>
  );
}

interface Loaded {
  agent: AgentDetail | null;
  missing: boolean;
  error: string | null;
}

function EditAgent({ id }: { id: string }) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [reloads, setReloads] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getAgent(id, { signal: controller.signal })
      .then((agent) => {
        if (!controller.signal.aborted) setLoaded({ agent, missing: false, error: null });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const missing = err instanceof ApiError && err.status === 404;
        setLoaded({ agent: null, missing, error: missing ? null : describeAgentError(err) });
      });
    return () => {
      controller.abort();
    };
  }, [id, reloads]);

  if (!loaded) {
    return (
      <div className="agents-page agent-form-page">
        <BackLink />
        <p className="muted">Loading agent…</p>
      </div>
    );
  }
  if (!loaded.agent) {
    return (
      <div className="agents-page agent-form-page">
        <BackLink />
        <h1>{loaded.missing ? "Agent not found" : "Edit agent"}</h1>
        {loaded.missing ? (
          <p className="muted">It may have been deleted, or it is not available in this organization.</p>
        ) : (
          <>
            <Alert kind="error">{loaded.error}</Alert>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setLoaded(null);
                setReloads((count) => count + 1);
              }}
            >
              Try again
            </button>
          </>
        )}
      </div>
    );
  }
  if (!loaded.agent.can_edit) {
    return (
      <div className="agents-page agent-form-page">
        <BackLink />
        <h1>{loaded.agent.name}</h1>
        <p className="muted">
          {loaded.agent.scope === "platform"
            ? "This agent comes with NEXA and cannot be changed. You can create your own agent instead."
            : "You cannot change this agent: only its author, or someone who manages all agents, can."}
        </p>
      </div>
    );
  }
  return <AgentForm agent={loaded.agent} />;
}

interface Catalog {
  tools: ToolInfo[] | null;
  error: string | null;
}

/** The tools NEXA has (`GET /tools/`), loaded once. */
function useToolCatalog(): Catalog {
  const [catalog, setCatalog] = useState<Catalog>({ tools: null, error: null });
  useEffect(() => {
    const controller = new AbortController();
    listTools({ signal: controller.signal })
      .then((tools) => {
        if (!controller.signal.aborted) setCatalog({ tools, error: null });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setCatalog({ tools: null, error: describeAgentError(err) });
      });
    return () => {
      controller.abort();
    };
  }, []);
  return catalog;
}

interface ToolOption {
  id: string;
  name: string;
  description: string;
  risk: ToolInfo["risk"];
  requires_approval: boolean;
  /** Offered now; a tool the agent already has may no longer be (it can only be removed). */
  available: boolean;
}

/**
 * The tools to offer: the available ones, and any the agent has that no longer are. Until the
 * catalog is loaded (or when it cannot be), the agent's own tools are all there is to show,
 * and nothing says they are unavailable.
 */
function toolOptions(catalog: ToolInfo[] | null, current: readonly AgentTool[]): ToolOption[] {
  const options: ToolOption[] = (catalog ?? [])
    .filter((tool) => tool.available)
    .map((tool) => ({
      id: tool.id,
      name: tool.name,
      description: tool.description,
      risk: tool.risk,
      requires_approval: tool.requires_approval,
      available: true,
    }));
  for (const tool of current) {
    if (options.some((option) => option.id === tool.id)) continue;
    const known = catalog?.find((item) => item.id === tool.id);
    options.push({
      id: tool.id,
      name: known?.name ?? tool.name,
      description: known?.description ?? "",
      risk: known?.risk ?? tool.risk,
      requires_approval: known?.requires_approval ?? tool.requires_approval,
      available: catalog === null,
    });
  }
  return options;
}

const RISK_TONES: Readonly<Record<string, string>> = { low: "up", medium: "waiting", high: "down" };

function AgentForm({ agent }: { agent: AgentDetail | null }) {
  const prefix = useId();
  const navigate = useNavigate();
  const { models, error: modelsError } = useModels();
  const catalog = useToolCatalog();
  const [draft, setDraft] = useState<AgentDraft>(() => draftFrom(agent));
  const [errors, setErrors] = useState<DraftErrors>({});
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  /** Bumped after each failed check, so focus moves to the first problem once per attempt. */
  const [attempt, setAttempt] = useState(0);
  const form = useRef<HTMLFormElement>(null);
  const options = toolOptions(catalog.tools, agent?.tools ?? []);
  const fieldId = (field: DraftField): string => `${prefix}-${field}`;

  // Only a new attempt moves focus: the first field with a problem, else the message.
  const focusProblem = useEffectEvent(() => {
    const first = DRAFT_FIELDS.find((field) => errors[field] !== undefined);
    const target = first
      ? document.getElementById(`${prefix}-${first}`)
      : form.current?.querySelector(".agent-form-alert");
    if (target instanceof HTMLElement) target.focus();
  });
  useEffect(() => {
    if (attempt > 0) focusProblem();
  }, [attempt]);

  const change = <K extends DraftField>(field: K, value: AgentDraft[K]) => {
    setDraft((current) => ({ ...current, [field]: value }));
  };

  const toggleTool = (id: string, on: boolean) => {
    setDraft((current) => ({
      ...current,
      allowed_tools: on
        ? [...current.allowed_tools.filter((tool) => tool !== id), id]
        : current.allowed_tools.filter((tool) => tool !== id),
    }));
  };

  const fail = (fieldErrors: DraftErrors, message: string | null) => {
    setErrors(fieldErrors);
    setProblem(message);
    setAttempt((count) => count + 1);
  };

  const save = async () => {
    const checked = checkDraft(draft);
    if (!checked.ok) {
      fail(checked.errors, "Some fields need your attention.");
      return;
    }
    const body = agent ? changedFields(checked.body, agent) : checked.body;
    if (agent && Object.keys(body).length === 0) {
      void navigate("/agents", { state: { saved: agent.name } });
      return;
    }
    setSaving(true);
    setErrors({});
    setProblem(null);
    try {
      const saved = agent ? await updateAgent(agent.id, body) : await createAgent(checked.body);
      void navigate("/agents", { state: { saved: saved.name } });
    } catch (err) {
      setSaving(false);
      if (!(err instanceof ApiError)) {
        fail({}, describeAgentError(err));
        return;
      }
      if (err.code === "TOOL_NOT_ALLOWED") {
        fail({ allowed_tools: err.message }, null);
        return;
      }
      const fieldErrors: DraftErrors = {};
      for (const field of DRAFT_FIELDS) {
        const message = err.fieldMessage(field);
        if (message) fieldErrors[field] = message;
      }
      const others = err.details.filter((detail) => !isDraftField(detail.field)).map((detail) => detail.message);
      let message: string | null = others.length > 0 ? others.join(" ") : null;
      if (Object.keys(fieldErrors).length === 0 && !message) {
        message = describeAgentError(err, agent ? EDIT_FORBIDDEN : CREATE_FORBIDDEN);
      }
      fail(fieldErrors, message);
    }
  };

  // The agent's model, when the list does not have it: still loading, or no longer available.
  let otherModel: string | null = null;
  if (draft.model && !models) {
    otherModel = draft.model;
  } else if (draft.model && !models?.some((model) => model.name === draft.model)) {
    otherModel = `${draft.model} (unavailable)`;
  }
  let modelHint = "The organization's default model is used unless you choose one.";
  if (!models) modelHint = modelsError ? "The models could not be loaded." : "Loading models…";

  return (
    <div className="agents-page agent-form-page">
      <BackLink />
      <header className="header">
        <h1>{agent ? `Edit ${agent.name}` : "New agent"}</h1>
        <p className="muted">
          {agent
            ? `Version ${String(agent.version)}. A change to how it behaves makes a new version; ` +
              "tasks already started keep theirs."
            : "Describe what the agent does and what it may use. You can change it later."}
        </p>
      </header>

      <form
        ref={form}
        className="card section agent-form"
        noValidate
        aria-label={agent ? "Edit agent" : "New agent"}
        onSubmit={(event) => {
          event.preventDefault();
          if (!saving) void save();
        }}
      >
        {problem && (
          <div tabIndex={-1} className="agent-form-alert">
            <Alert kind="error">{problem}</Alert>
          </div>
        )}

        <FormField id={fieldId("name")} label="Name" required error={errors.name}>
          {(control) => (
            <input
              {...control}
              value={draft.name}
              maxLength={NAME_MAX}
              autoComplete="off"
              onChange={(event) => {
                change("name", event.target.value);
              }}
            />
          )}
        </FormField>

        <FormField
          id={fieldId("description")}
          label="Description"
          hint="One or two sentences: what the agent is for. Shown in the library."
          error={errors.description}
        >
          {(control) => (
            <textarea
              {...control}
              rows={2}
              value={draft.description}
              maxLength={DESCRIPTION_MAX}
              onChange={(event) => {
                change("description", event.target.value);
              }}
            />
          )}
        </FormField>

        <FormField
          id={fieldId("instructions")}
          label="Instructions"
          required
          hint="How the agent works: what to look for, which tools to use and when, how to answer."
          error={errors.instructions}
        >
          {(control) => (
            <textarea
              {...control}
              rows={10}
              value={draft.instructions}
              maxLength={INSTRUCTIONS_MAX}
              onChange={(event) => {
                change("instructions", event.target.value);
              }}
            />
          )}
        </FormField>

        <FormField id={fieldId("model")} label="Model" hint={modelHint} error={errors.model}>
          {(control) => (
            <select
              {...control}
              value={draft.model}
              onChange={(event) => {
                change("model", event.target.value);
              }}
            >
              <option value="">Organization default</option>
              {models?.map((model) => (
                <option key={model.name} value={model.name}>
                  {model.display_name}
                  {model.is_default ? " (default)" : ""}
                </option>
              ))}
              {otherModel !== null && <option value={draft.model}>{otherModel}</option>}
            </select>
          )}
        </FormField>

        <fieldset
          id={fieldId("allowed_tools")}
          tabIndex={-1}
          className="agent-fieldset"
          aria-describedby={`${prefix}-tools-hint${errors.allowed_tools ? ` ${prefix}-tools-error` : ""}`}
        >
          <legend className="field-label">Tools</legend>
          <p id={`${prefix}-tools-hint`} className="field-hint">
            The agent may use only these, and only as far as you may yourself. Tools that change
            data always ask for approval first.
          </p>
          {catalog.tools === null && !catalog.error && <p className="muted small">Loading tools…</p>}
          {catalog.error && <p className="muted small">The tools could not be loaded: {catalog.error}</p>}
          {options.length > 0 && (
            <ul className="agent-tool-options">
              {options.map((tool) => {
                const checked = draft.allowed_tools.includes(tool.id);
                const risk = riskLabel(tool.risk);
                return (
                  <li key={tool.id}>
                    <label className="agent-tool-option">
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={!tool.available && !checked}
                        onChange={(event) => {
                          toggleTool(tool.id, event.target.checked);
                        }}
                      />
                      <span className="agent-tool-option-text">
                        <span className="agent-tool-option-head">
                          <span className="agent-tool-name">{tool.name}</span>
                          {risk && (
                            <span className={`badge badge-${RISK_TONES[tool.risk] ?? "neutral"} agent-badge`}>
                              {risk}
                            </span>
                          )}
                          {tool.requires_approval && (
                            <span className="badge badge-waiting agent-badge">Needs approval</span>
                          )}
                          {!tool.available && (
                            <span className="badge badge-neutral agent-badge">No longer available</span>
                          )}
                        </span>
                        {tool.description && <span className="muted small">{tool.description}</span>}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
          {catalog.tools !== null && options.length === 0 && (
            <p className="muted small">No tools are available to you here.</p>
          )}
          {errors.allowed_tools && (
            <p id={`${prefix}-tools-error`} className="field-error">
              {errors.allowed_tools}
            </p>
          )}
        </fieldset>

        <fieldset className="agent-fieldset">
          <legend className="field-label">Limits per task</legend>
          <p className="field-hint">A task stops as soon as it reaches any of these.</p>
          <div className="agent-limits-grid">
            {LIMIT_RULES.map((rule) => (
              <FormField
                key={rule.name}
                id={fieldId(rule.name)}
                label={rule.label}
                hint={rule.hint}
                error={errors[rule.name]}
              >
                {(control) => (
                  <input
                    {...control}
                    type="number"
                    inputMode="numeric"
                    min={rule.min}
                    max={rule.max}
                    step={1}
                    value={draft[rule.name]}
                    onChange={(event) => {
                      change(rule.name, event.target.value);
                    }}
                  />
                )}
              </FormField>
            ))}
          </div>
        </fieldset>

        <fieldset id={fieldId("approval_policy")} tabIndex={-1} className="agent-fieldset">
          <legend className="field-label">Approval</legend>
          <label className="check">
            <input
              type="radio"
              name={`${prefix}-policy`}
              checked={draft.approval_policy === "registry"}
              onChange={() => {
                change("approval_policy", "registry");
              }}
            />
            Ask only before tools that always need approval (such as saving to memory)
          </label>
          <label className="check">
            <input
              type="radio"
              name={`${prefix}-policy`}
              checked={draft.approval_policy === "all_tool_calls"}
              onChange={() => {
                change("approval_policy", "all_tool_calls");
              }}
            />
            Ask before every tool call
          </label>
          {errors.approval_policy && <p className="field-error">{errors.approval_policy}</p>}
        </fieldset>

        <fieldset className="agent-fieldset">
          <legend className="field-label">Structured input and output (optional)</legend>
          <p className="field-hint">
            JSON Schemas of objects. With an input schema, starting a task asks for these values
            besides the goal; with an output schema, the answer also comes as data of that shape.
          </p>
          <FormField id={fieldId("input_schema")} label="Input schema" error={errors.input_schema}>
            {(control) => (
              <textarea
                {...control}
                className="agent-code"
                rows={6}
                spellCheck={false}
                placeholder='{"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}'
                value={draft.input_schema}
                onChange={(event) => {
                  change("input_schema", event.target.value);
                }}
              />
            )}
          </FormField>
          <FormField id={fieldId("output_schema")} label="Output schema" error={errors.output_schema}>
            {(control) => (
              <textarea
                {...control}
                className="agent-code"
                rows={6}
                spellCheck={false}
                placeholder='{"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}'
                value={draft.output_schema}
                onChange={(event) => {
                  change("output_schema", event.target.value);
                }}
              />
            )}
          </FormField>
        </fieldset>

        <label className="check">
          <input
            id={fieldId("enabled")}
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) => {
              change("enabled", event.target.checked);
            }}
          />
          Switched on: tasks can be started with this agent
        </label>
        {errors.enabled && <p className="field-error">{errors.enabled}</p>}

        <div className="actions">
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? "Saving…" : agent ? "Save changes" : "Create agent"}
          </button>
          <Link to="/agents" className="btn btn-quiet">
            Cancel
          </Link>
        </div>
      </form>
    </div>
  );
}

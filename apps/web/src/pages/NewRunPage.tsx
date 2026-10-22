/**
 * Starting a task (`/agents/:agentId/run`): the agent (what it is for, its tools and limits),
 * the goal, and, when the agent has an input schema, its inputs: one control per property of
 * a simple object schema (text, a list to choose from, a number, a checkbox, with required
 * marks), the input as JSON for any other schema.
 *
 * One idempotency key is made per form, so however often it is submitted (a double click, a
 * retry after a lost answer) at most one task starts; the button is disabled while it is
 * sent. The server's field errors go next to their controls ("input.<name>" to that input); a
 * refusal (too many tasks in progress, the agent switched off, no permission) is said above.
 * A started task opens its page.
 */

import { useEffect, useEffectEvent, useId, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { getAgent } from "../api/agents";
import { ApiError } from "../api/client";
import { newIdempotencyKey, submitRun } from "../api/runs";
import { useAuth } from "../auth/AuthContext";
import { ScopeBadge, ToolBadges } from "../components/agents/badges";
import { FormField } from "../components/agents/FormField";
import {
  buildInput,
  initialValues,
  inputFormFor,
  parseJsonObject,
  type InputField,
  type InputValues,
} from "../components/agents/inputSchema";
import {
  describeAgentError,
  describeInputProblem,
  isDocumentAssistant,
  limitsSummary,
} from "../components/agents/labels";
import { Icon } from "../components/chat/Icon";
import { Alert } from "../components/forms";
import type { AgentDetail } from "../types/agents";

const GOAL_MAX = 10_000;

const FORBIDDEN = "You do not have permission to run agents in this organization.";

/** Problems by place: "goal", "input" (the input as a whole), "input.<name>" (one input). */
type Problems = Record<string, string>;

export function NewRunPage() {
  const { agentId = "" } = useParams();
  const { activeOrganizationId } = useAuth();
  const navigate = useNavigate();
  const shown = useRef(activeOrganizationId);

  // An agent belongs to the organization it was opened in: switching leaves for the library.
  useEffect(() => {
    if (shown.current === activeOrganizationId) return;
    shown.current = activeOrganizationId;
    void navigate("/agents", { replace: true });
  }, [activeOrganizationId, navigate]);

  return <NewRun key={agentId} agentId={agentId} />;
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

function NewRun({ agentId }: { agentId: string }) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [reloads, setReloads] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getAgent(agentId, { signal: controller.signal })
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
  }, [agentId, reloads]);

  if (!loaded) {
    return (
      <div className="agents-page run-form-page">
        <BackLink />
        <p className="muted">Loading agent…</p>
      </div>
    );
  }
  const { agent } = loaded;
  if (!agent) {
    return (
      <div className="agents-page run-form-page">
        <BackLink />
        <h1>{loaded.missing ? "Agent not found" : "New task"}</h1>
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

  return (
    <div className="agents-page run-form-page">
      <BackLink />
      <header className="header run-form-header">
        <div className="agent-card-head">
          <h1>{agent.name}</h1>
          <ScopeBadge scope={agent.scope} />
          <span className="muted small">Version {agent.version}</span>
        </div>
        {agent.description && <p className="muted">{agent.description}</p>}
      </header>
      <section className="card section run-form-agent" aria-label="What this agent may do">
        <ToolBadges tools={agent.tools} policy={agent.approval_policy} />
        <p className="muted small agent-limits">{limitsSummary(agent.limits)}</p>
      </section>
      {agent.enabled ? (
        <RunForm agent={agent} />
      ) : (
        <Alert kind="info">This agent is switched off, so no task can be started with it.</Alert>
      )}
    </div>
  );
}

/** One input made from a property of the agent's input schema. */
function InputControl({
  field,
  id,
  value,
  error,
  onChange,
}: {
  field: InputField;
  id: string;
  value: string | boolean | undefined;
  error: string | undefined;
  onChange: (value: string | boolean) => void;
}) {
  if (field.kind === "boolean") {
    return (
      <div className={`field${error ? " field-invalid" : ""}`}>
        <label className="check run-input-check">
          <input
            id={id}
            type="checkbox"
            checked={value === true}
            aria-describedby={
              [field.description ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean).join(" ") ||
              undefined
            }
            aria-invalid={error ? true : undefined}
            onChange={(event) => {
              onChange(event.target.checked);
            }}
          />
          {field.label}
        </label>
        {field.description && (
          <span id={`${id}-hint`} className="field-hint">
            {field.description}
          </span>
        )}
        {error && (
          <span id={`${id}-error`} className="field-error">
            {error}
          </span>
        )}
      </div>
    );
  }
  const text = typeof value === "string" ? value : "";
  let hint = field.description ?? undefined;
  if (field.kind === "number") {
    const range = [
      field.minimum !== null ? `at least ${field.minimum.toLocaleString()}` : null,
      field.maximum !== null ? `at most ${field.maximum.toLocaleString()}` : null,
    ].filter(Boolean);
    const kind = field.integer ? "A whole number" : "A number";
    const bounds = range.length > 0 ? `${kind}, ${range.join(" and ")}.` : null;
    hint = [field.description, bounds].filter(Boolean).join(" ") || undefined;
  }
  return (
    <FormField id={id} label={field.label} hint={hint} error={error} required={field.required}>
      {(control) => {
        if (field.kind === "choice") {
          return (
            <select
              {...control}
              value={text}
              onChange={(event) => {
                onChange(event.target.value);
              }}
            >
              <option value="">{field.required ? "Choose…" : "None"}</option>
              {field.options.map((option, index) => (
                <option key={`${String(index)}:${String(option)}`} value={String(index)}>
                  {String(option)}
                </option>
              ))}
            </select>
          );
        }
        if (field.kind === "number") {
          return (
            <input
              {...control}
              type="number"
              inputMode={field.integer ? "numeric" : "decimal"}
              step={field.integer ? 1 : "any"}
              min={field.minimum ?? undefined}
              max={field.maximum ?? undefined}
              value={text}
              onChange={(event) => {
                onChange(event.target.value);
              }}
            />
          );
        }
        return field.multiline ? (
          <textarea
            {...control}
            rows={3}
            maxLength={field.maxLength ?? undefined}
            value={text}
            onChange={(event) => {
              onChange(event.target.value);
            }}
          />
        ) : (
          <input
            {...control}
            maxLength={field.maxLength ?? undefined}
            value={text}
            onChange={(event) => {
              onChange(event.target.value);
            }}
          />
        );
      }}
    </FormField>
  );
}

function RunForm({ agent }: { agent: AgentDetail }) {
  const prefix = useId();
  const navigate = useNavigate();
  const form = useMemo(() => inputFormFor(agent.input_schema), [agent.input_schema]);
  const fields = form.kind === "fields" ? form.fields : [];
  const [goal, setGoal] = useState("");
  const [values, setValues] = useState<InputValues>(() => initialValues(fields));
  const [json, setJson] = useState("");
  const [problems, setProblems] = useState<Problems>({});
  const [refusal, setRefusal] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  /** Bumped each time problems are shown, so focus moves to the first one once per attempt. */
  const [attempt, setAttempt] = useState(0);
  /** One key per form: a repeated submission starts no second task. */
  const [idempotencyKey] = useState(newIdempotencyKey);
  /** Set at once on submit, before React re-renders the disabled button. */
  const inFlight = useRef(false);
  const formElement = useRef<HTMLFormElement>(null);

  const inputId = (name: string): string => `${prefix}-input-${name}`;

  // The first problem's control gets focus (the JSON input or the inputs for one about the
  // input as a whole), else the message above the form.
  const focusProblem = useEffectEvent(() => {
    let id: string | null = null;
    if (problems.goal !== undefined) id = `${prefix}-goal`;
    else {
      const field = fields.find((item) => problems[`input.${item.name}`] !== undefined);
      if (field) id = inputId(field.name);
      else if (problems.input !== undefined) id = form.kind === "json" ? `${prefix}-json` : `${prefix}-inputs`;
    }
    const target = id ? document.getElementById(id) : formElement.current?.querySelector(".run-form-alert");
    if (target instanceof HTMLElement) target.focus();
  });
  useEffect(() => {
    if (attempt > 0) focusProblem();
  }, [attempt]);

  const show = (found: Problems, message: string | null) => {
    setProblems(found);
    setRefusal(message);
    setAttempt((count) => count + 1);
  };

  /** The input to send, or null after showing what is wrong with it. */
  const collect = (): { goal: string; input: Record<string, unknown> } | null => {
    const found: Problems = {};
    if (!goal.trim()) found.goal = "Describe the task.";
    else if (goal.length > GOAL_MAX) found.goal = `Use at most ${GOAL_MAX.toLocaleString()} characters.`;
    let input: Record<string, unknown> = {};
    if (form.kind === "fields") {
      const built = buildInput(fields, values);
      if (built.ok) input = built.input;
      else for (const [name, problem] of Object.entries(built.errors)) found[`input.${name}`] = problem;
    } else if (form.kind === "json") {
      const parsed = parseJsonObject(json);
      if (parsed.ok) input = parsed.value ?? {};
      else found.input = parsed.error;
    }
    if (Object.keys(found).length > 0) {
      show(found, null);
      return null;
    }
    return { goal, input };
  };

  /** The server's refusal, placed where it belongs. */
  const refused = (err: unknown) => {
    if (!(err instanceof ApiError) || err.code !== "VALIDATION_FAILED" || err.details.length === 0) {
      show({}, describeAgentError(err, FORBIDDEN));
      return;
    }
    const found: Problems = {};
    const elsewhere: string[] = [];
    const known = new Set(fields.map((field) => field.name));
    /** The first problem of a place is the one shown. */
    const add = (place: string, problem: string) => {
      if (!Object.hasOwn(found, place)) found[place] = problem;
    };
    for (const detail of err.details) {
      if (detail.field === "goal") {
        add("goal", detail.message);
      } else if (detail.field === "input" || detail.field.startsWith("input.")) {
        const name = detail.field.slice("input.".length).split(".")[0] ?? "";
        const problem = describeInputProblem(detail);
        if (detail.field !== "input" && known.has(name)) {
          add(`input.${name}`, problem);
        } else {
          // The input as a whole (a missing value, JSON input): said once, under the inputs.
          const where = detail.field === "input" ? "" : `${detail.field.slice("input.".length)}: `;
          const line = `${where}${problem}`;
          found.input = found.input ? `${found.input} ${line}` : line;
        }
      } else {
        elsewhere.push(detail.message);
      }
    }
    show(found, elsewhere.length > 0 ? elsewhere.join(" ") : null);
  };

  const submit = () => {
    if (inFlight.current) return;
    const ready = collect();
    if (!ready) return;
    inFlight.current = true;
    setSubmitting(true);
    setProblems({});
    setRefusal(null);
    submitRun({ agent_id: agent.id, goal: ready.goal, input: ready.input, idempotency_key: idempotencyKey })
      .then((run) => {
        void navigate(`/agents/runs/${encodeURIComponent(run.id)}`);
      })
      .catch((err: unknown) => {
        inFlight.current = false;
        setSubmitting(false);
        refused(err);
      });
  };

  const asksQuestions = isDocumentAssistant(agent);
  let goalHint = asksQuestions
    ? "Ask a question your documents can answer, for example: What is our refund policy for annual plans?"
    : "What should the agent do? Be specific about the outcome you want.";
  // Near the limit, how much room is left.
  if (goal.length > GOAL_MAX * 0.9) {
    goalHint += ` ${goal.length.toLocaleString()} of ${GOAL_MAX.toLocaleString()} characters.`;
  }

  return (
    <form
      ref={formElement}
      className="card section run-form"
      noValidate
      aria-label="New task"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <h2>New task</h2>
      {refusal && (
        <div tabIndex={-1} className="run-form-alert">
          <Alert kind="error">{refusal}</Alert>
        </div>
      )}
      <FormField
        id={`${prefix}-goal`}
        label={asksQuestions ? "Question" : "Goal"}
        required
        hint={goalHint}
        error={problems.goal}
      >
        {(control) => (
          <textarea
            {...control}
            rows={4}
            maxLength={GOAL_MAX}
            value={goal}
            onChange={(event) => {
              setGoal(event.target.value);
            }}
          />
        )}
      </FormField>

      {form.kind === "fields" && (
        <fieldset
          id={`${prefix}-inputs`}
          tabIndex={-1}
          className="agent-fieldset run-inputs"
          aria-describedby={problems.input ? `${prefix}-inputs-error` : undefined}
        >
          <legend className="field-label">Inputs</legend>
          {fields.map((field) => (
            <InputControl
              key={field.name}
              field={field}
              id={inputId(field.name)}
              value={values[field.name]}
              error={problems[`input.${field.name}`]}
              onChange={(value) => {
                setValues((current) => ({ ...current, [field.name]: value }));
              }}
            />
          ))}
          {problems.input && (
            <p id={`${prefix}-inputs-error`} className="field-error">
              {problems.input}
            </p>
          )}
        </fieldset>
      )}

      {form.kind === "json" && (
        <FormField
          id={`${prefix}-json`}
          label="Input (JSON)"
          hint="A JSON object matching the agent's input schema (below)."
          error={problems.input}
        >
          {(control) => (
            <textarea
              {...control}
              className="agent-code"
              rows={6}
              spellCheck={false}
              placeholder="{}"
              value={json}
              onChange={(event) => {
                setJson(event.target.value);
              }}
            />
          )}
        </FormField>
      )}
      {form.kind === "json" && agent.input_schema && (
        <details className="run-schema">
          <summary>Show the input schema</summary>
          <pre className="run-output" tabIndex={0}>
            <code>{JSON.stringify(agent.input_schema, null, 2)}</code>
          </pre>
        </details>
      )}

      <div className="actions">
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? "Starting…" : "Start task"}
        </button>
        <Link to="/agents" className="btn btn-quiet">
          Cancel
        </Link>
      </div>
    </form>
  );
}

/**
 * The agent form's state as typed (`AgentDraft`: text for every number and schema), checked
 * and turned into the body the server takes. The checks mirror the server's (agents
 * serializers): required name and instructions, the lengths, whole numbers within each
 * limit's range, schemas that are JSON objects. The server checks everything again, the
 * schemas' validity as JSON Schema included.
 */

import type { AgentDetail, AgentInput, ApprovalPolicy, UpdateAgentBody } from "../../types/agents";
import { parseJsonObject } from "./inputSchema";

export const NAME_MAX = 80;
export const DESCRIPTION_MAX = 500;
export const INSTRUCTIONS_MAX = 20_000;
export const TOOLS_MAX = 32;

export type LimitName = "max_steps" | "max_tool_calls" | "max_duration_seconds" | "max_tokens";

export interface LimitRule {
  name: LimitName;
  label: string;
  hint: string;
  min: number;
  max: number;
  fallback: number;
}

/** The limits of a task, with the server's ranges and defaults. */
export const LIMIT_RULES: readonly LimitRule[] = [
  {
    name: "max_steps",
    label: "Steps",
    hint: "Decisions the agent may make, 1 to 50.",
    min: 1,
    max: 50,
    fallback: 12,
  },
  {
    name: "max_tool_calls",
    label: "Tool calls",
    hint: "0 to 100; 0 lets it use no tools.",
    min: 0,
    max: 100,
    fallback: 20,
  },
  {
    name: "max_duration_seconds",
    label: "Time limit (seconds)",
    hint: "10 to 3,600 (one hour).",
    min: 10,
    max: 3600,
    fallback: 600,
  },
  {
    name: "max_tokens",
    label: "Token budget",
    hint: "Tokens the model may read and write in all, 1,000 to 1,000,000.",
    min: 1000,
    max: 1_000_000,
    fallback: 200_000,
  },
];

export interface AgentDraft {
  name: string;
  description: string;
  instructions: string;
  /** "" for the organization's default model. */
  model: string;
  allowed_tools: string[];
  max_steps: string;
  max_tool_calls: string;
  max_duration_seconds: string;
  max_tokens: string;
  approval_policy: ApprovalPolicy;
  enabled: boolean;
  /** JSON text; "" for none. */
  input_schema: string;
  output_schema: string;
}

export type DraftField = keyof AgentDraft;

/** The problems of a draft by field, written next to each field. */
export type DraftErrors = Partial<Record<DraftField, string>>;

/** The fields in the order the form shows them: the first one with a problem gets focus. */
export const DRAFT_FIELDS: readonly DraftField[] = [
  "name",
  "description",
  "instructions",
  "model",
  "allowed_tools",
  "max_steps",
  "max_tool_calls",
  "max_duration_seconds",
  "max_tokens",
  "approval_policy",
  "enabled",
  "input_schema",
  "output_schema",
];

export function isDraftField(name: string): name is DraftField {
  return (DRAFT_FIELDS as readonly string[]).includes(name);
}

const schemaText = (schema: Record<string, unknown> | null): string =>
  schema === null ? "" : JSON.stringify(schema, null, 2);

/** A new agent's draft, or an existing agent's as it is now. */
export function draftFrom(agent: AgentDetail | null): AgentDraft {
  if (!agent) {
    return {
      name: "",
      description: "",
      instructions: "",
      model: "",
      allowed_tools: [],
      max_steps: "12",
      max_tool_calls: "20",
      max_duration_seconds: "600",
      max_tokens: "200000",
      approval_policy: "registry",
      enabled: true,
      input_schema: "",
      output_schema: "",
    };
  }
  return {
    name: agent.name,
    description: agent.description,
    instructions: agent.instructions,
    model: agent.model,
    allowed_tools: agent.tools.map((tool) => tool.id),
    max_steps: String(agent.limits.max_steps),
    max_tool_calls: String(agent.limits.max_tool_calls),
    max_duration_seconds: String(agent.limits.max_duration_seconds),
    max_tokens: String(agent.limits.max_tokens),
    approval_policy: agent.approval_policy,
    enabled: agent.enabled,
    input_schema: schemaText(agent.input_schema),
    output_schema: schemaText(agent.output_schema),
  };
}

export type DraftResult = { ok: true; body: AgentInput } | { ok: false; errors: DraftErrors };

/** The body to send, or what is wrong with the draft. */
export function checkDraft(draft: AgentDraft): DraftResult {
  const errors: DraftErrors = {};
  const name = draft.name.trim();
  if (!name) errors.name = "Enter a name.";
  else if (name.length > NAME_MAX) errors.name = `Use at most ${String(NAME_MAX)} characters.`;
  if (draft.description.length > DESCRIPTION_MAX) {
    errors.description = `Use at most ${String(DESCRIPTION_MAX)} characters.`;
  }
  if (!draft.instructions.trim()) errors.instructions = "Enter the instructions.";
  else if (draft.instructions.length > INSTRUCTIONS_MAX) {
    errors.instructions = `Use at most ${INSTRUCTIONS_MAX.toLocaleString()} characters.`;
  }
  if (draft.allowed_tools.length > TOOLS_MAX) {
    errors.allowed_tools = `Choose at most ${String(TOOLS_MAX)} tools.`;
  }

  const limits: Partial<Record<LimitName, number>> = {};
  for (const rule of LIMIT_RULES) {
    const text = draft[rule.name].trim();
    const value = Number(text);
    if (text === "" || !Number.isInteger(value)) {
      errors[rule.name] = "Enter a whole number.";
    } else if (value < rule.min || value > rule.max) {
      errors[rule.name] = `Enter a number from ${rule.min.toLocaleString()} to ${rule.max.toLocaleString()}.`;
    } else {
      limits[rule.name] = value;
    }
  }

  const inputSchema = parseJsonObject(draft.input_schema);
  if (!inputSchema.ok) errors.input_schema = inputSchema.error;
  const outputSchema = parseJsonObject(draft.output_schema);
  if (!outputSchema.ok) errors.output_schema = outputSchema.error;

  if (Object.keys(errors).length > 0 || !inputSchema.ok || !outputSchema.ok) return { ok: false, errors };
  return {
    ok: true,
    body: {
      name,
      // The server trims both, as it does every text field.
      description: draft.description.trim(),
      instructions: draft.instructions.trim(),
      model: draft.model,
      allowed_tools: draft.allowed_tools,
      ...limits,
      approval_policy: draft.approval_policy,
      enabled: draft.enabled,
      input_schema: inputSchema.value,
      output_schema: outputSchema.value,
    },
  };
}

const sameMembers = (a: readonly string[], b: readonly string[]): boolean =>
  a.length === b.length && a.every((item) => b.includes(item));

const sameJson = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

/**
 * Only what differs from the agent as it is: an unchanged tool list is not sent, so a tool
 * switched off since does not stop renaming the agent.
 */
export function changedFields(body: AgentInput, agent: AgentDetail): UpdateAgentBody {
  const changed: UpdateAgentBody = {};
  if (body.name !== agent.name) changed.name = body.name;
  if (body.description !== undefined && body.description !== agent.description) {
    changed.description = body.description;
  }
  if (body.instructions !== agent.instructions) changed.instructions = body.instructions;
  if (body.model !== undefined && body.model !== agent.model) changed.model = body.model;
  if (body.allowed_tools && !sameMembers(body.allowed_tools, agent.tools.map((tool) => tool.id))) {
    changed.allowed_tools = body.allowed_tools;
  }
  for (const rule of LIMIT_RULES) {
    const value = body[rule.name];
    if (value !== undefined && value !== agent.limits[rule.name]) changed[rule.name] = value;
  }
  if (body.approval_policy !== undefined && body.approval_policy !== agent.approval_policy) {
    changed.approval_policy = body.approval_policy;
  }
  if (body.enabled !== undefined && body.enabled !== agent.enabled) changed.enabled = body.enabled;
  if (body.input_schema !== undefined && !sameJson(body.input_schema, agent.input_schema)) {
    changed.input_schema = body.input_schema;
  }
  if (body.output_schema !== undefined && !sameJson(body.output_schema, agent.output_schema)) {
    changed.output_schema = body.output_schema;
  }
  return changed;
}

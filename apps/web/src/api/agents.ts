/**
 * The agent endpoints (docs/api/agents.md#agents), served by Core: the agents of the platform
 * and the organization, and creating, changing and deleting them. This is the only module
 * that calls `/agents/...`; the tasks agents carry out are `./runs`.
 *
 * The server decides what the caller may do with each agent (`can_edit`, `can_delete`) and
 * checks a configuration when it is saved: 422 VALIDATION_FAILED with field details (the
 * fields of `AgentInput`), 422 TOOL_NOT_ALLOWED for a tool the author may not hand over, 403
 * for a caller without the permission.
 */

import type {
  Agent,
  AgentDetail,
  AgentInput,
  ListAgentsParams,
  UpdateAgentBody,
} from "../types/agents";
import { api } from "./client";

export interface RequestControl {
  signal?: AbortSignal;
}

const agentPath = (id: string): string => `/agents/${encodeURIComponent(id)}/`;

/** The agents the caller can see: the platform's first, then the organization's, by name. */
export async function listAgents(
  params: ListAgentsParams = {},
  control: RequestControl = {},
): Promise<Agent[]> {
  // The server knows only `enabled=true`; anything else lists every agent.
  const suffix = params.enabled ? "?enabled=true" : "";
  const { data } = await api<Agent[]>(`/agents/${suffix}`, { signal: control.signal });
  return data;
}

/** One agent with its instructions and schemas; 404 for one the caller cannot see. */
export async function getAgent(id: string, control: RequestControl = {}): Promise<AgentDetail> {
  const { data } = await api<AgentDetail>(agentPath(id), { signal: control.signal });
  return data;
}

export async function createAgent(body: AgentInput): Promise<AgentDetail> {
  const { data } = await api<AgentDetail>("/agents/", { method: "POST", body });
  return data;
}

/** Changes the fields sent; a change to how the agent behaves gives it a new version. */
export async function updateAgent(id: string, body: UpdateAgentBody): Promise<AgentDetail> {
  const { data } = await api<AgentDetail>(agentPath(id), { method: "PATCH", body });
  return data;
}

/** Deletes the agent; the tasks it already carried out stay in their owners' history. */
export async function deleteAgent(id: string): Promise<void> {
  await api(agentPath(id), { method: "DELETE" });
}

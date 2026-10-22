/**
 * The tool endpoints (docs/api/tools.md): the tools AI may use in this organization, and
 * switching one on or off. This is the only module that calls `/tools/...`. Deciding on a
 * paused action is a conversation endpoint (`decideApproval` in `./conversations`).
 */

import type { ToolInfo } from "../types/tools";
import { api } from "./client";

export interface RequestControl {
  signal?: AbortSignal;
}

/** Every tool NEXA has, whether it is switched on here and whether the caller may let AI use it. */
export async function listTools(control: RequestControl = {}): Promise<ToolInfo[]> {
  const { data } = await api<ToolInfo[]>("/tools/", { signal: control.signal });
  return data;
}

/** Switch a tool on or off for the organization (needs `ai.tool.manage`; 403 otherwise). */
export async function setToolEnabled(id: string, enabled: boolean): Promise<ToolInfo> {
  const { data } = await api<ToolInfo>(`/tools/${encodeURIComponent(id)}/`, {
    method: "PATCH",
    body: { enabled },
  });
  return data;
}

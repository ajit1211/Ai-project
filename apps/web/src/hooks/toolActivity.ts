/**
 * A reply's tool calls as pure functions: applying the `tool_call` and `tool_result` events of
 * a stream to the calls a reply already had, and finding the approval a paused reply waits
 * for. Only calls the server reported exist here: nothing is added that no event or stored
 * record describes.
 */

import type { Message, ToolCallEvent, ToolResultEvent } from "../types/chat";
import type { Approval, ToolActivity } from "../types/tools";

/** A call first seen in a stream; what only the stored record knows stays empty. */
function newActivity(
  fields: Pick<ToolActivity, "invocation_id" | "call_id" | "tool_id" | "tool_name" | "status"> &
    Partial<ToolActivity>,
  position: number,
): ToolActivity {
  return {
    tool_version: "",
    step: 0,
    position,
    summary: "",
    risk: "",
    duration_ms: null,
    error_code: null,
    error_message: null,
    result: null,
    result_truncated: false,
    result_hidden: false,
    sources: [],
    approval: null,
    ...fields,
  };
}

/** `tools` with the call of `invocationId` changed by `patch`, or with `added` appended. */
function upsert(
  tools: readonly ToolActivity[],
  invocationId: string,
  patch: (existing: ToolActivity) => ToolActivity,
  added: () => ToolActivity,
): ToolActivity[] {
  const index = tools.findIndex((tool) => tool.invocation_id === invocationId);
  if (index === -1) return [...tools, added()];
  return tools.map((tool, at) => (at === index ? patch(tool) : tool));
}

/** A call was checked: about to run, refused, or waiting for the user. */
export function applyToolCall(tools: readonly ToolActivity[], event: ToolCallEvent): ToolActivity[] {
  const checked = {
    status: event.status,
    risk: event.risk,
    error_code: event.error_code,
    error_message: event.error_message,
  };
  return upsert(
    tools,
    event.invocation_id,
    (existing) => ({ ...existing, ...checked, summary: event.summary || existing.summary }),
    () =>
      newActivity(
        {
          invocation_id: event.invocation_id,
          call_id: event.call_id,
          tool_id: event.tool_id,
          tool_name: event.tool_name,
          step: event.step,
          summary: event.summary,
          ...checked,
        },
        tools.length,
      ),
  );
}

/** A call ended: its outcome, duration and result. */
export function applyToolResult(tools: readonly ToolActivity[], event: ToolResultEvent): ToolActivity[] {
  const ended = {
    status: event.status,
    duration_ms: event.duration_ms,
    result: event.result,
    result_truncated: event.result_truncated,
    error_code: event.error_code,
    error_message: event.error_message,
    sources: event.sources,
  };
  return upsert(
    tools,
    event.invocation_id,
    (existing) => ({ ...existing, ...ended }),
    // A result without its call (never sent by the server): show it under the tool's id.
    () =>
      newActivity(
        {
          invocation_id: event.invocation_id,
          call_id: event.call_id,
          tool_id: event.tool_id,
          tool_name: event.tool_id,
          ...ended,
        },
        tools.length,
      ),
  );
}

/** The paused reply with `approval` on the call it is about (the event's copy is the latest). */
export function withApproval(message: Message, approval: Approval): Message {
  const tools = message.tools ?? [];
  if (!tools.some((tool) => tool.invocation_id === approval.invocation_id)) return message;
  return {
    ...message,
    tools: tools.map((tool) => (tool.invocation_id === approval.invocation_id ? { ...tool, approval } : tool)),
  };
}

/** The approval a paused reply waits for, or null when it is not paused on one. */
export function pendingApproval(message: Message): Approval | null {
  if (message.status !== "awaiting_approval") return null;
  const waiting = (message.tools ?? []).find((tool) => tool.approval?.status === "pending");
  return waiting?.approval ?? null;
}

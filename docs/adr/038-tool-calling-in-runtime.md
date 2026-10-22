# ADR-038: Tool calling in Runtime: gateway-normalized calls, a code-defined registry, one checked executor, approvals that pause the reply

Status: accepted

## Context

Phase 10 lets a model request tools and continue with their results. The model is untrusted:
it may ask for any tool with any arguments, and tool output (documents especially) may contain
instructions. Choices to make: where calls are normalized and where they are run, how tools
are defined and switched off, how a call is authorized, how repeated requests are kept from
repeating effects, how an action is approved, and how this fits the existing chat stream.

## Decision

1. **The AI Gateway normalizes, Runtime executes.** `nexa_ai` carries tool definitions,
   provider-neutral tool calls and results for Anthropic, OpenAI and the mock, validates the
   history and refuses models without `tool_calling`; it never runs anything. The tool
   executor lives in Runtime next to the chat engine, which owns the turn; there is no
   separate tool service and no service-to-service call.
2. **Tools are code.** A `ToolDefinition` maps a stable id and version to an application-owned
   async handler with strict JSON Schemas, permissions, risk, limits and an audit policy;
   registration refuses unsafe definitions. The database (Core's `tool_settings`) can only
   switch a tool off, for the platform (Django admin) or an organization (`ai.tool.manage`).
3. **One path for every call.** Offer by permission; on every call check the id, the tool
   (offered, enabled), permissions re-read from the database, the arguments, the budgets and
   rate limits; record it under a unique idempotency key; claim it with one conditional update;
   re-check; run under a timeout; validate and bound the output; record and audit. Results go
   back to the model as JSON data in tool messages, never as instructions.
4. **Approvals pause the reply.** A tool that changes data needs approval: the call and an
   approval bound to the hash of its exact arguments are recorded, the reply ends the stream
   paused (`awaiting_approval`, terminal `approval_required` event), and the user's decision
   (`POST /conversations/{id}/approvals/{approval_id}/`) continues the same reply. Approvals
   expire, are single-use and only the requester's, and never bypass a permission check.
5. **A bounded loop inside the existing stream**: the same message, events `tool_call` and
   `tool_result`, limits on steps, calls, time, output, concurrency and rate, a forced final
   answer at the limit, and stop at any point.
6. **Core owns the schema**: `tool_invocations`, `tool_approvals`, `tool_settings`,
   `messages.tool_trace`; Runtime gets write grants (and INSERT on `audit_logs` for tool
   events, with Core's action names). Composite tenant keys cascade in the database.

## Alternatives

- **Tools executed by the gateway.** Would put authorization, persistence and approvals in a
  framework-free package that knows no users or tables. Rejected; the gateway stays a model
  client.
- **Tools defined in the database** (handler names or URLs in rows). Turns configuration into
  code execution; rejected by the brief and by us.
- **A separate tool-execution service or Celery for every call.** Adds a network hop and a
  queue for calls that take milliseconds; no Phase 10 tool is long-running. A queued tool
  can be added later as a handler that starts a job and reports its state.
- **Approvals as a blocking wait inside the stream.** Holds a connection, a conversation lock
  and a quota reservation while a person decides. Rejected for pause-and-resume.
- **Provider-native parallel tool execution semantics.** Each provider differs; the executor
  decides concurrency from the tools' own declarations instead.

## Consequences

- A streamed ordinary reply may take several model calls (each metered); limits keep the cost
  bounded and visible on the message.
- Grounded replies and the JSON send path do not use tools; earlier turns' tool calls are not
  replayed to the model.
- Exactly-once is guaranteed for recording and claiming a call, not for side effects of a
  process that crashes mid-handler; the one mutating tool is idempotent itself.
- New tools need code, review and tests; there are no user-defined tools.

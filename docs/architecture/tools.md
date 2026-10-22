# AI tool calling (Phase 10)

A model can ask NEXA to run a tool (a calculator, the clock, a search of the user's documents,
saving a memory) while it answers, read the result, and continue. The model plans; NEXA decides
what may run. Every call is checked, authorized, run by trusted application code under limits,
validated, recorded and, for anything that changes data, approved by the user first.

Decision record: [ADR-038](../adr/038-tool-calling-in-runtime.md). API:
[tools.md](../api/tools.md) and [conversations.md](../api/conversations.md#tool-calling).

## Overview

```text
user message ─► chat engine (Runtime) ─► tools offered: registry ∩ switches ∩ permissions ∩ model can call tools
                    │
                    ▼
            ┌─► AI Gateway (one model call, streamed) ──► text ──────────────► reply text
            │       │
            │       └─► tool calls (normalized, whole, at the end of the stream)
            │                │
            │                ▼
            │     tool executor: plan (check + record) ─► needs approval? ─► pause the reply
            │                │                                                  (user decides; the
            │                ▼                                                   decision resumes it)
            │     run (claim, re-check, handler under timeout, validate + bound output, record)
            │                │
            └── tool results appended to the history as data (bounded loop) ◄─┘
```

| Piece | Where | What it does |
| --- | --- | --- |
| Tool definitions in the gateway's types | `nexa_ai.types` (`ToolSpec`, `ToolCallPart`, `ToolResultPart`) | Provider-neutral tool definitions, calls and results |
| Gateway | `nexa_ai.gateway`, `nexa_ai.tools`, the adapters | Carries tools to the model and tool calls back; never executes anything |
| Registry | `nexa_runtime.tools.registry` | The trusted tools: definitions mapped to application-owned handlers |
| Executor | `nexa_runtime.tools.executor` | Checks, authorizes, records, runs, validates and bounds every call |
| Orchestration | `nexa_runtime.chat.service` | The bounded model/tool loop inside the existing chat reply |
| Schema | Core app `nexa_core.tools` | `tool_invocations`, `tool_approvals`, `tool_settings` (+ `messages.tool_trace`) |

## Gateway

The gateway normalizes tool calling across providers; it does not run tools.

- **Requests** may offer `tools` (`ToolSpec`: name, description, JSON Schema of the arguments)
  and `tool_choice` (`auto`, or `none`: answer in text while the definitions stay available
  for a history that contains tool calls).
- **Answers**: non-streaming responses carry `tool_calls`; streamed answers report them whole,
  once, in the final `message.end` event. Each call has the provider's id, the tool name and
  the decoded arguments. Arguments that are not a JSON object (or too large or too deep) are
  kept as `raw_arguments` so the caller rejects the call rather than running it empty.
- **History**: an assistant message may hold text and tool calls; the next message is a `tool`
  message answering every one of those calls, once. The gateway refuses a malformed history
  (results without calls, unanswered calls, duplicate ids, tool parts without tools) before any
  provider is called, and refuses tools combined with structured output.
- **Capability detection**: a request that offers tools needs `tool_calling`; only targets with
  it are tried, and a model without any is refused (`AI_MODEL_UNAVAILABLE`) without a provider
  call. `Gateway.model_capabilities(ctx, model)` tells a caller in advance, so the chat engine
  simply does not offer tools to such a model.
- **Adapters**: Anthropic (`tools` with `input_schema`, `tool_use` / `tool_result` blocks,
  streamed `input_json_delta` fragments joined at the end) and OpenAI Chat Completions
  (`function` tools, `tool_calls` with JSON-string arguments, `tool` messages, streamed
  fragments joined by index). The mock model calls tools deterministically (control phrases
  for tests, a few plain requests for local use) and answers from the results.
- Streaming, timeouts, retries, fallback, quotas and usage metering are unchanged: every model
  call of a tool loop is an ordinary gateway call with its own usage row.
- The public `/ai/chat` routes do not accept tools: tools exist only where NEXA authorizes them.

## Registry

A tool is a `ToolDefinition`:

| Field | Meaning |
| --- | --- |
| `id`, `version` | Stable id (`[a-z][a-z0-9_]{1,63}`) and a version (`1`, `1.2`); both are recorded with every call |
| `name`, `description`, `category` | For people (`name`) and for the model (`description`) |
| `input_schema` | JSON Schema of the arguments; `additionalProperties: false` is required |
| `output_schema` | JSON Schema the handler's output must match |
| `handler` | An async function `(ToolContext, validated arguments) -> ToolOutput` |
| `permissions` | Needed on top of `ai.tool.execute`, checked when offered and again when run |
| `scopes` | Where it may be used (`conversation` in Phase 10) |
| `risk`, `read_only`, `requires_approval` | `low` / `medium` / `high`; high risk requires approval |
| `timeout_seconds`, `max_retries` | At most 60 s; retries only for read-only tools |
| `max_input_bytes`, `max_output_bytes` | Size limits of arguments and output |
| `concurrency_safe` | May run alongside the other calls of the same step |
| `audit` | `failures` (low risk) or `all` |
| `redact` | Argument names whose values are not stored |
| `describe`, `approval_details` | The one-line summary, and what an approval shows |

Registration refuses duplicate ids, invalid or open-ended schemas, limits out of range, a
synchronous handler, a mutating tool that would be retried or run concurrently, a high-risk tool
without approval, and an approval without a description of what is approved. The registry is
built at start-up from code; the database can only switch a tool off.

### Adding a read-only tool

A new tool is one definition and one line in the registry; the loop, the executor, the API and
the UI pick it up without changes.

```python
# apps/runtime/nexa_runtime/tools/builtin/units.py
from typing import Any

from nexa_runtime.tools.registry import ToolContext, ToolDefinition, ToolFailure, ToolOutput

_TO_METRES = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048}


async def convert(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    try:
        metres = arguments["value"] * _TO_METRES[arguments["from"]]
    except KeyError as exc:
        raise ToolFailure("unknown_unit", "That unit is not supported.") from exc
    return ToolOutput(data={"value": metres / _TO_METRES[arguments["to"]], "unit": arguments["to"]})


UNITS = ToolDefinition(
    id="convert_length",
    name="Length converter",
    description="Convert a length between m, km, mi and ft.",
    version="1",
    category="math",
    input_schema={
        "type": "object",
        "properties": {
            "value": {"type": "number"},
            "from": {"enum": list(_TO_METRES)},
            "to": {"enum": list(_TO_METRES)},
        },
        "required": ["value", "from", "to"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"value": {"type": "number"}, "unit": {"type": "string"}},
        "required": ["value", "unit"],
    },
    handler=convert,
    describe=lambda a: f"Converting {a.get('value')} {a.get('from')} to {a.get('to')}",
)
```

Then add `UNITS` to `builtin_tools()` in `nexa_runtime/tools/builtin/__init__.py`, and test it
like `apps/runtime/tests/test_tools_unit.py`. A handler raises `ToolFailure(code, message)` for
expected failures; the message reaches the model and the user, so it must be safe to show. A
tool that reaches an external service must also bring its own allowlist, authorization and
network controls (none exists in Phase 10).

## Discovery and authorization

For each streamed reply of ordinary chat (not a reply grounded in selected documents, and not
one sent with `tools: "none"`), the chat engine offers the tools that are:

1. registered for the `conversation` scope and enabled in their definition;
2. not switched off for the platform (a `tool_settings` row without organization, Django admin)
   or for the organization (`PATCH /tools/{id}/`, `ai.tool.manage`); a platform "off" wins;
3. permitted: the user has `ai.tool.execute` (members and above; not viewers) and every
   permission the tool lists (`documents.view`, `memory.create`, `organization.view`);
4. only when the chosen model can call tools at all.

The permissions come from the database (the authenticated principal), never from the client.
Being offered a tool authorizes nothing: every call is checked again when it is planned and
when it runs, with the principal re-read from the database at that moment.

## Validation

Arguments are model output. `validation.validate_arguments` requires a JSON object (never the
raw text), within the tool's size limit and eight levels of nesting, valid against the schema
(unknown properties refused), and fills in the schema's top-level defaults. Errors name the
field and the rule, never the value. The handler only ever receives validated arguments.

Output is untrusted too. `validate_output` requires an object valid against the output schema
and within the size limit; the longest top-level list is shortened from the end until it fits
and the result is marked `truncated`; anything that still does not fit is a failure
(`output_too_large`). A turn also has a budget for all its tool output together
(`TOOLS_MAX_TOTAL_OUTPUT_BYTES`).

## Execution

```text
requested ─► validated ─► (awaiting_approval ─► approved) ─► executing ─► succeeded
     │            │                 │                            ├──► failed
     └────────────┴─────────────────┴──► rejected                ├──► timed_out
                                                                 └──► cancelled
```

**Plan** (per model step, in the model's order): the call id is valid and new in this turn;
the tool exists, was offered in this turn and is switched on; the user may use it now; at most
one action needing approval per step; the turn's call budget (`TOOLS_MAX_CALLS_PER_TURN`) and
the user's rate limit (`TOOLS_CALLS_PER_MINUTE`) allow it; the arguments are valid. The call is
recorded under an idempotency key derived from the reply, the step, the position and the call
id (a unique column): repeating a step records nothing new. A rejected call never runs; the
model is told why.

**Run**: one conditional `UPDATE … SET status='executing' WHERE status IN ('validated',
'approved')` claims the call, so only one request ever runs it; a second sees the recorded
outcome instead. Then the tool's switch and the user's permissions are checked again, and an
approval must be the requesting user's, for exactly these arguments (their hash), decided
before it expired. The handler runs under the tool's timeout. Read-only tools may be re-run
after a retryable failure or a timeout, up to `max_retries`; tools that change data never are.
Expected failures (`ToolFailure`), errors of services the handler used, and bugs become
`failed` with a safe code and message (never a traceback); a timeout is `timed_out`; a stop
is `cancelled`. Only a handler that returned, with output that passed validation, is
`succeeded`.

**Exactly once?** Recording and claiming are exactly once per (reply, step, position, call id).
A handler that crashes the process mid-way may have had effects without a recorded success;
the only mutating tool (`save_memory`) is idempotent itself (the same statement is one memory).
NEXA does not claim exactly-once side effects beyond that.

**Concurrency**: the calls of one step run together (at most `TOOLS_MAX_CONCURRENCY`) only when
every one is read-only and concurrency-safe; otherwise one at a time, in the model's order.
Calls that depend on each other come in different steps, and steps are sequential.

## Orchestration

The reply is a bounded loop inside the existing chat generation (same stream, same message):

1. Build the context as before (instructions, memories, summary, history), add the rules for
   tools as a system block, offer the tools.
2. Call the model. Text streams to the client as usual.
3. No tool calls: the reply is complete. Tool calls: plan them, send a `tool_call` event per
   call, run the runnable ones (a `tool_result` event as each ends), append the model's turn
   and the results to the history, and call the model again.
4. After `TOOLS_MAX_ITERATIONS` tool steps, or once `TOOLS_TURN_TIMEOUT_SECONDS` have passed,
   the model is called once more with `tool_choice: "none"` and must answer with what it has
   (`tool_trace.limit_reached` records it). There is no unbounded retry or loop.

Results go back to the model as JSON data in a `tool` message (`{"status": "ok", "tool": …,
"result": …}`, or `{"status": "error", "error": {code, message}}`, or `declined`), never as
instructions. The system rules say so, and that document excerpts often contain text that looks
like instructions. A failure is never presented as a result.

A stop request is noticed within a quarter of a second, also while a tool runs: running calls
are cancelled and recorded as such, calls not yet run are closed as cancelled, and the reply is
stored as cancelled with the text so far. A reply that ends in any way closes the calls that
never ran; a reply taken over after its process died has its unfinished calls closed as
cancelled too. Each model call of the loop renews the conversation's generation claim, so a
long loop is never mistaken for a dead one. Each model call is metered separately in the
usage ledger; the reply carries their total. `messages.tool_trace` keeps each model call's text
and tool calls (with redacted arguments replaced, as in the invocation records) so a paused
reply can resume exactly.

Replies grounded in selected documents (Phase 08) do not use tools; the JSON (non-streaming)
send path does not either. Earlier turns' tool calls are not replayed into later turns: the
model sees the earlier answers' text.

## Approvals

A tool with `requires_approval` (in Phase 10: `save_memory`) never runs on the model's say-so:

1. Planning records the call as `awaiting_approval` and a `tool_approvals` row: the tool and
   version, a summary, what will happen (`details`: action, target, the exact content,
   consequences), the hash of the exact arguments, the risk, and an expiry
   (`TOOLS_APPROVAL_TTL_SECONDS`, 15 minutes by default). Other calls of that step run.
2. The reply pauses: status `awaiting_approval`, the conversation is released, and the stream
   ends with the terminal `approval_required` event. Sending another message is refused
   (`TOOL_APPROVAL_PENDING`) until the user decides or the approval expires.
3. `POST /conversations/{id}/approvals/{approval_id}/` with `{decision, arguments_hash}`. Only
   the requesting user may decide (404 otherwise); the hash must be the one shown (otherwise
   `TOOL_APPROVAL_STALE`: a changed action needs a new decision); an expired approval cannot be
   used (`TOOL_APPROVAL_EXPIRED`, the reply is closed); a decided one cannot be reused
   (`TOOL_APPROVAL_CLOSED`).
4. The decision continues the same reply: an approved call runs (permissions re-checked: an
   approval never grants more than the user may do at that moment), a rejected one is reported
   to the model as declined, and the loop goes on.

## Built-in tools

| Tool | What it really does | Risk | Changes data | Approval | Permissions |
| --- | --- | --- | --- | --- | --- |
| `calculator` | Arithmetic with its own parser (`+ - * / // % ^`, parentheses, `pi`, `e`, sqrt, abs, round, floor, ceil, min, max, ln, log, log10, log2, exp, sin, cos, tan); no `eval`, bounded length, depth and magnitude; 15 significant digits | low | no | no | — |
| `current_time` | The server clock in UTC, optionally in an IANA time zone | low | no | no | — |
| `document_metadata` | Titles, file names, formats, sizes, status and searchability of readable documents (never text) | low | no | no | `documents.view` |
| `search_documents` | The Phase 08/09 retrieval engine over readable documents; bounded excerpts with references | medium (audited) | no | no | `documents.view` |
| `workspace_info` | The organization, the user's role, their teams (member count if `members.view`) | low | no | no | `organization.view` |
| `save_memory` | Saves a statement to the user's private memories through the memory engine | high | yes | yes | `memory.create` |

Not implemented (out of scope or no existing integration): code execution, shell, SQL, web
requests, browsing, e-mail or any external side effect, and the optional data-analysis tool.

## Persistence, audit and observability

- `tool_invocations`: the call (id, tool, version), its step and position, the validated and
  redacted arguments, their hash, the status, the bounded result or a safe error, references
  to source documents (never their text), timings, attempts. Document results are
  re-authorized whenever they are read: if a source document is no longer readable, the result
  is hidden. Rows are deleted with their reply or conversation.
- `tool_approvals`: as above. `tool_settings`: the switches.
- Audit (`audit_logs`, written by Runtime with Core's action names): `TOOL_INVOCATION_REJECTED`,
  `TOOL_PERMISSION_DENIED`, `TOOL_RATE_LIMITED`, `TOOL_APPROVAL_REQUESTED`,
  `TOOL_APPROVAL_DECIDED`, `TOOL_EXECUTION_FAILED`, `TOOL_EXECUTED` (medium and high risk; low
  risk successes are in `tool_invocations` only), `TOOL_SETTINGS_CHANGED`. Metadata holds ids,
  the tool, the version, the outcome and codes: never arguments, results or document text.
- Logs: `chat.tools.offered`, `tools.invocation.rejected`, `tools.invocation.completed` (tool,
  version, status, error code, duration, attempts, truncation), `tools.approval.decided`,
  `security.event` for warnings. No arguments or outputs are logged.
- Usage: every model call of the loop is a usage-ledger row linked to the reply; tools
  themselves cost nothing beyond what they call (`search_documents` records its query embedding
  as before).

## Settings (Runtime)

| Setting | Default | Meaning |
| --- | --- | --- |
| `TOOLS_ENABLED` | true | Off: no tools are offered anywhere |
| `TOOLS_MAX_ITERATIONS` | 4 | Tool steps per reply before the model must answer |
| `TOOLS_MAX_CALLS_PER_TURN` | 8 | Tool calls per reply |
| `TOOLS_MAX_CONCURRENCY` | 4 | Calls of one step running at once (read-only, concurrency-safe) |
| `TOOLS_TURN_TIMEOUT_SECONDS` | 120 | After this, no new tool step starts |
| `TOOLS_MAX_TOTAL_OUTPUT_BYTES` | 48 000 | All tool output one reply may use |
| `TOOLS_CALLS_PER_MINUTE` | 30 | Tool calls per user |
| `TOOLS_APPROVAL_TTL_SECONDS` | 900 | How long an approval stays open |

Each tool's own timeout, retries and size limits are part of its definition.

## Security

| Threat | Control |
| --- | --- |
| Arbitrary code, shell, SQL, files, URLs | No such tool exists; handlers are application code; the calculator parses its own grammar (no `eval`/`exec`/AST) |
| Tool-call injection, made-up tools | Only tools offered in this turn and registered can run; unknown names are rejected and audited |
| Prompt injection in documents or results | Results are JSON data in `tool` messages; the rules say they are not instructions; control flow never depends on result text; mutating tools need the user's approval |
| Unauthorized discovery or execution | Offered by permission; permissions re-read from the database when planning and running; principal and organization come from the token |
| Cross-tenant access | Every query is scoped by the principal's organization; document tools use the shared document policy as SQL; composite tenant keys on every tool table |
| Forged or repeated call ids | Ids validated and unique per turn; the idempotency key is unique; claiming is one conditional update |
| Invalid JSON, schema attacks, oversized data | Strict schemas (no unknown properties), size and depth limits, output validation and bounds |
| Loops and cost | Iteration, call, time, output and rate limits; one forced final answer; gateway quotas apply to every model call |
| Approval reuse or changed arguments | Approvals bound to the arguments hash, single use, expiring, for the requesting user only, permissions re-checked at run time |
| Duplicate side effects | Unique idempotency key, single claim, no automatic retries of mutating tools, idempotent `save_memory` |
| Secrets in logs or results | No arguments or outputs in logs or audit metadata; `redact` for sensitive argument names; safe error messages only |
| Race conditions | Conditional status transitions only (no transition out of a final state); conversation lock for resumption |

## Testing

```text
uv run pytest packages/nexa-ai/tests/test_tool_calling.py   # gateway, adapters, mock
uv run pytest apps/runtime/tests/test_tools_unit.py         # registry, validation, calculator, clock
uv run pytest apps/runtime/tests/test_tools_chat.py         # end to end: loop, limits, auth, approvals
uv run pytest apps/core/tests/test_tools_models.py          # schema, tenancy, cascades, admin
uv run python scripts/e2e_tools.py                          # against the running stack
```

CI uses the mock model only; no paid API is called.

## Known limitations

- Tool calling was exercised with the mock model and with recorded Anthropic/OpenAI HTTP
  exchanges, not against the live providers.
- Only streamed replies of ordinary chat use tools; grounded replies and the JSON send path
  do not. Earlier turns' tool calls are not replayed to the model.
- One action needing approval per model step; approvals expire lazily (on the next access).
- No long-running (queued) tools yet: every tool completes within its timeout (≤ 60 s).
- Organization switches only turn tools off or back on; there are no per-team or per-user
  tool policies.

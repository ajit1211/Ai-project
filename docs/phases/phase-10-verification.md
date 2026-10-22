# Phase 10 verification report

AI tool calling: a model can ask NEXA to run a tool while it answers, read the result and
continue, within a checked, authorized, bounded and recorded execution engine; actions that
change data wait for the user's approval. Design: [tools.md](../architecture/tools.md); API:
[tools.md](../api/tools.md) and [conversations.md](../api/conversations.md#tool-calling);
decision: [ADR-038](../adr/038-tool-calling-in-runtime.md).

Every number below comes from runs on 2026-10-22 against the code being committed.

## 1. Repository audit

Phases 01 to 09 were inspected in code. What existed and was reused as it is: the AI Gateway
with its provider adapters, retries, fallback, quotas and usage ledger (`nexa_ai`), which
already declared `ToolCallPart`, `ToolResultPart`, `Role.TOOL` and `Capability.TOOL_CALLING`
but refused them everywhere; the chat engine's one-stream-per-reply generation, cancellation
and persistence (Phase 05); memory recall and context building (Phase 06) and the memory
engine's idempotent `create` (reused by `save_memory`); the document permission predicate as
SQL and the Phase 08/09 retrieval engine (`RagEngine.resolve` + `retrieve`, reused by
`search_documents` without a second pipeline); the shared access policy, the permission catalog
(`ai.tool.execute` existed, unused) and Runtime's principal loading; the audit log (Core only,
until now); Redis rate limits with the broker-less fallback; Core's schema ownership with
per-table Runtime grants.

Prerequisites that were missing and were added as minimal corrections: the mock model lacked
`tool_calling` (seed and a data migration); Runtime had no way to re-read a principal
mid-request (`reload_principal`) and no write access to `audit_logs`; the document predicate
could not be used without the "ready to search" condition (`readable_documents` split out);
the Vite proxy did not route `/api/v1/tools` to Runtime. No new service, broker or dependency
(`jsonschema` was already used for structured output).

## 2. Implemented

| Brief area | Status | Where |
| --- | --- | --- |
| Tool definition contract and registry | ✓ | `nexa_runtime/tools/registry.py`: id, name, description, version, input and output JSON Schemas, handler, permissions, scopes, risk, read-only, approval, timeout, retries, size limits, concurrency, enabled, audit policy, redaction; registration refuses duplicates, invalid or open schemas, unsafe combinations; definitions are code, the database only switches tools off |
| Gateway tool calling | ✓ | `nexa_ai`: `ToolSpec`, `tools` / `tool_choice` on requests, `tool_calls` on responses and stream ends, history validation (`nexa_ai.tools`), capability routing and `model_capabilities`, Anthropic and OpenAI translation both ways incl. streamed fragments, deterministic mock tool calling; `/ai/chat` refuses tools |
| Argument and output validation | ✓ | `nexa_runtime/tools/validation.py`: JSON object only, size and depth limits, schema with `additionalProperties: false`, defaults, field-only errors; output schema, size bound with truncation marking |
| Discovery by permission and policy | ✓ | `ToolExecutor.offer`: enabled definition, platform and organization switches, `ai.tool.execute` plus the tool's permissions, model capability; `tools: "none"` and grounded replies get none |
| Execution engine and states | ✓ | `ToolExecutor.plan` / `run` / `execute`: the 15 steps of the brief, explicit states with conditional transitions only, single claim, re-check at run time, timeouts, bounded retries for read-only tools, validated and bounded output, records and audit |
| Orchestrator | ✓ | `ChatService._events` / `_model_step`: bounded loop (iterations, calls, time, output, concurrency), forced final text answer, provider-neutral history, stop at any point, failures reported as failures |
| Idempotency and reliability | ✓ | Unique idempotency key per (reply, step, position, call id); `INSERT … ON CONFLICT DO NOTHING`; `UPDATE … WHERE status IN (runnable)` claim; mutating tools never retried; `save_memory` idempotent by content |
| Risk policies and approval | ✓ | `tool_approvals`: tool and version, summary, details (action, target, content, consequences), requester, organization, arguments hash, expiry, status, decider and time; pause/resume; stale, expired, closed, foreign decisions refused; permissions re-checked when the approved call runs |
| Output handling | ✓ | Normalized `tool_result` (call id, tool, version, status, data, error code, duration, sources, truncation); results are JSON data in `tool` messages; failures never presented as results |
| Persistence, audit, usage | ✓ | `tool_invocations`, `tool_approvals`, `messages.tool_trace`; audit events `TOOL_*` written by Runtime with Core's names; every model call of a loop metered |
| API | ✓ | `GET /tools/`, `GET /tools/{id}/`, `PATCH /tools/{id}/` (`ai.tool.manage`), `GET /tools/invocations/{id}/`, `POST /conversations/{id}/approvals/{approval_id}/`; `tools` on send; `tools` on messages; stable error codes |
| React | ✓ | Activity timeline (running, done, failed, timed out, cancelled, rejected, declined, needs approval), safe result rendering, approval card, pending-approval notice, error wording, organization tool switches |
| Security | ✓ | See [the security table](../architecture/tools.md#security) and the tests below |
| Observability and settings | ✓ | Structured logs with request ids, no arguments or outputs logged; `TOOLS_*` settings validated |
| Documentation | ✓ | tools.md (architecture, lifecycle, adding a tool, approvals, idempotency, errors, settings, security, limitations), API docs, ADR-038, README, `.env.example` |

Not implemented, by the brief's scope or because no safe integration exists: code or shell
execution, SQL, web requests, browser automation, e-mail or any external side effect, queued
long-running tools, the optional data-analysis tool, agent planning.

## 3. Lifecycle

```text
send ─► chat engine: context + tool rules + tools offered (registry ∩ switches ∩ permissions ∩ model capability)
  ─► gateway stream (text to the client; tool calls whole at the end)
  ─► executor.plan: id, tool, switch, permissions (DB), one approval per step, budget, rate limit,
     arguments ─► recorded (idempotency key) as validated | rejected | awaiting_approval
  ─► executor.run: claim (conditional UPDATE) ─► re-check switch, permissions, approval hash
     ─► handler under timeout ─► output validated and bounded ─► succeeded | failed | timed_out | cancelled
  ─► results as JSON data in a tool message ─► next model call … (≤ iterations, ≤ calls, ≤ time, ≤ output)
  ─► final text (forced with tool_choice "none" at the limit) ─► message_complete
awaiting_approval ─► stream ends (approval_required) ─► POST .../approvals/{id}/ {decision, arguments_hash}
  ─► same reply resumes: earlier steps replayed from records, the approved call runs now, loop continues
```

## 4. New and modified files

99 files: 50 added and 49 modified (11 251 insertions, 183 deletions); the web client is 28
of them (3 152 insertions), the Python services, migrations, tests and docs the other 71
(8 099 insertions). Main additions:

| Area | Files |
| --- | --- |
| Gateway | `packages/nexa-ai/src/nexa_ai/tools.py` (new), `types.py`, `gateway.py`, `router.py`, `tokens.py`, `providers/base.py`, `anthropic.py`, `openai.py`, `mock.py`; `tests/test_tool_calling.py` (new) |
| Runtime tools | `apps/runtime/nexa_runtime/tools/` (new: `registry.py`, `validation.py`, `executor.py`, `repository.py`, `tables.py`, `audit.py`, `activity.py`, `schemas.py`, `service.py`, `router.py`, `errors.py`, `builtin/{calculator,clock,documents,workspace,memory}.py`) |
| Runtime chat | `chat/service.py` (loop, pause/resume, decide), `chat/events.py`, `chat/schemas.py`, `chat/router.py`, `chat/prompts.py`, `chat/tables.py`, `auth/dependencies.py` (`reload_principal`), `rag/retrieval.py` (`readable_documents`), `rag/tables.py`, `ai/router.py`, `api.py`, `main.py`, `config.py` |
| Core | `nexa_core/tools/` (new app: models, admin, migrations `0001`, `0002`), `chat/models.py` + migration `0004`, `ai/migrations/0004_mock_tool_calling.py`, `ai/catalog.py`, `audit/actions.py`, `access/catalog.py`, `settings.py`; `nexa_common/errors.py` |
| Tests | `apps/runtime/tests/test_tools_unit.py`, `test_tools_chat.py`, `apps/core/tests/test_tools_models.py`; two existing tests adjusted for the tool rules block; `scripts/e2e_tools.py` |
| Web | `src/types/tools.ts`, `api/tools.ts`, `components/chat/{ToolTimeline,ToolResult,ApprovalCard}.tsx`, `components/chat/tools.ts`, `components/AiToolsSettings.tsx`, `hooks/toolActivity.ts`, changes to the chat stream client, hooks, message components, `ChatPage`, `OrganizationPage`, `services/ai/errors.ts`, styles, `vite.config.ts`; 48 new tests |
| Docs | `docs/architecture/tools.md`, `docs/api/tools.md`, `docs/api/conversations.md`, `docs/adr/038-…`, `docs/adr/README.md`, `README.md`, `.env.example`, this report |

## 5. Database migrations

| Migration | What it does |
| --- | --- |
| `ai/0004_mock_tool_calling` | Adds `tool_calling` to the existing `mock-1` model row (data migration, reversible) |
| `chat/0004_message_tool_trace` | `messages.tool_trace` (nullable JSON) and the `awaiting_approval` status (column widened) |
| `tools/0001_initial` | `tool_invocations`, `tool_approvals`, `tool_settings` with their constraints and indexes |
| `tools/0002_tenant_fks_and_runtime_grants` | Composite tenant foreign keys (invocation → message and conversation of the same organization, approval → invocation) cascading in the database; `nexa_runtime` gets INSERT/UPDATE on the three tables and INSERT on `audit_logs` (skipped where the role does not exist) |

Applied on the local PostgreSQL 18 and on the PostgreSQL 16 + pgvector verification server;
`makemigrations --check` reports no changes.

## 6. Registry and schema design

See [tools.md#registry](../architecture/tools.md#registry). Argument schemas are JSON Schema
2020-12 checked at registration with the gateway's own schema checker (size, depth, no
`$id`/`pattern`/remote references) and must forbid unknown properties; output schemas are
checked the same way. A tool contract is versioned: the version is recorded on every
invocation and approval and is part of the approval's argument hash, so a changed tool never
runs under an old approval (tested).

## 7. AI Gateway integration

See [tools.md#gateway](../architecture/tools.md#gateway). Verified with recorded HTTP
exchanges against the real SDKs (Anthropic `tool_use`/`tool_result` and streamed
`input_json_delta`; OpenAI `tool_calls` and streamed fragments by index), with the mock on
both the non-streaming and streaming paths, and with a model lacking the capability (refused
before any provider call). Live provider calls were not made (no API key in this environment).

## 8. Built-in tools

| Tool | Actual capability | Risk | Approval |
| --- | --- | --- | --- |
| `calculator` | `+ - * / // % ^ **`, parentheses, unary signs, `pi`, `e`, sqrt, abs, round, floor, ceil, min, max, ln, log, log10, log2, exp, sin, cos, tan; own tokenizer and recursive-descent parser; ≤ 200 characters, ≤ 120 tokens, ≤ 24 nesting levels, exponents ≤ 1 000, magnitudes ≤ 1e300; 15 significant digits | low | no |
| `current_time` | Injected clock; UTC and one IANA zone; unknown zones are errors | low | no |
| `document_metadata` | Readable documents (owner, organization, team, `documents.manage_all`): title, file name, format, status, size, pages, words, searchable; by id or title filter; never text | low | no |
| `search_documents` | `RagEngine.resolve` + `retrieve` with the same authorization, modes and fallbacks as grounded chat; ≤ 8 results, 700-character excerpts, references | medium, audited | no |
| `workspace_info` | Organization name, the user's role, their teams; member count only with `members.view` | low | no |
| `save_memory` | `MemoryEngine.create` in the user's private scope (same refusals and normalization as manual saving; idempotent) | high, audited | yes |

## 9. Authorization, approval and idempotency

See [tools.md#execution](../architecture/tools.md#execution) and
[#approvals](../architecture/tools.md#approvals). Tested: a viewer is offered nothing; a
member without `documents.view` is not offered the document tools; a permission removed after
the tools were offered (and after an approval was given) stops the call at run time; another
organization's documents are neither listed nor searched; forged document ids are "not found";
a result is hidden once its document is unshared; only the requesting user may decide, with the
exact hash, before expiry, once; a tool version change invalidates an approval; repeated
planning and execution of the same call record and run it once; mutating tools are never
retried; text inside tool results cannot trigger a tool; arguments and queries do not reach
audit metadata; invocation records are private to the conversation's owner.

## 10. Tests and results

| Suite | Database | Count | Result |
| --- | --- | --- | --- |
| Gateway: `packages/nexa-ai` (25 new in `test_tool_calling.py`: validation, routing, mock loop, adapters) | — | 217 | pass |
| Runtime: `test_tools_unit.py` (registry rules, arguments, output bounds, calculator incl. injection attempts and no-eval check, clock, result framing) | — | 76 | pass |
| Runtime: `test_tools_chat.py` (discovery, switches, one call, parallel calls, ordered steps and limits, rejections, failures, timeout, provider failure after a tool, re-check at run time, rate limit, idempotency, document tools and isolation, injection in results, hidden results, approvals: pause, approve once, reject, foreign user, expiry, revoked permission, version change, archived conversation, one at a time, JSON path; stop during a tool; redaction everywhere; takeover after a crash; cascades) | local | 38 | pass |
| Core: `test_tools_models.py` (tenant keys, cascades in SQL, uniqueness, statuses, permissions, mock capability, admin audit and privacy) | local | 9 | pass |
| Python, all (`uv run pytest`) | PostgreSQL 16 + pgvector 0.6.2 | 874 | 873 pass, 1 skipped (live embedding evaluation, opt-in) |
| Python, all | local PostgreSQL 18, no pgvector | 874 | 838 pass, 36 skipped (35 need pgvector, 1 live evaluation) |
| Web: Vitest (48 new in 7 files) | — | 271 in 24 files | pass |
| `scripts/e2e_tools.py` against the running local stack (mock model) | local | 45 checks | 45 pass |
| `scripts/e2e_rag.py`, `scripts/e2e_documents.py`, `scripts/e2e_vector.py` (Phases 07–09, re-run) | local | 31 + 75 + 20 checks | all pass |
| ruff, ruff format, mypy (strict, 258 files); ESLint, `tsc --noEmit`, `vite build` | | | clean |
| Migrations applied on both databases; `makemigrations --check` | | | no changes |

Python test counts by package: `nexa-common` 17, `nexa-ai` 217 (25 new), `apps/core` 276
(9 new), `apps/runtime` 364 (114 new). Two existing tests were adjusted because every
tool-enabled request now carries a second system block (the rules for tools): one counted
messages, one asserted that nothing after the memory block is a system message. CI uses the
mock model only.

### Review

A multi-agent adversarial review (six lenses, each finding to be checked by a skeptic) was
started, but every reviewer stopped on the account's session limit before reporting, so it
produced no findings. The high-risk paths were then reviewed by hand. Defects found and
fixed, with a regression test where the path is reachable:

| Finding | Severity | Fix |
| --- | --- | --- |
| A long tool loop could outlive the conversation's generation claim (stale after `AI_TIMEOUT_TOTAL` + 60 s) and be taken over by another request while still running | medium | Each model call of the loop renews the claim (not covered by a dedicated test) |
| `messages.tool_trace` kept the values of redacted arguments (no built-in tool redacts, so no data was affected) | medium | The trace stores redacted values, as the invocation records do; tested |
| An approval was checked against the hash stored at proposal; a tool whose version changed meanwhile would have run under the old approval | medium | The hash is recomputed from what is about to run, including the tool version; tested. A tool needing approval may no longer redact arguments (registration refuses it); tested |
| A decision in an archived conversation resumed its reply | low | 409 `CONFLICT`; tested |
| A continued stream's `message_start` lacked the reply's tool activity | low | Attached; tested |
| A reply taken over after its process died left its tool calls `executing` | low | They are closed as cancelled; tested |
| A call neither runnable nor final would have left its step without a result | low | Reported as not run (not reachable through the API) |

Checked by inspection and on the live stack: Anthropic accepts `tool_choice: {"type": "none"}`
(installed SDK types); the cascades work through the restricted `nexa_runtime` role
(regenerating a reply with tool records and a paused reply, deleting the conversation); web
links are built from encoded ids through the router and nothing is rendered as HTML.

## 11. Build, lint and migration results

See the table above: all clean on the code being committed.

## 12. Manual configuration

- Nothing is required for the mock model: tools work in development and tests without keys.
- A real provider needs its key (`AI_ANTHROPIC_API_KEY` / `AI_OPENAI_API_KEY`); the seeded
  Claude and GPT models already declare `tool_calling`.
- Optional: `TOOLS_*` settings (defaults are documented); platform-wide switches in the
  Django admin (`Tool settings`); organization switches through `PATCH /tools/{id}/` or the
  organization page (owners and admins).
- A deployed edge must route `/api/v1/tools/*` to Runtime (the Vite proxy was updated; the
  architecture document's routing table lists Runtime prefixes).

## 13. Known limitations and unresolved issues

- Tool calling was exercised with the mock model and recorded Anthropic/OpenAI exchanges, not
  against the live providers.
- Only streamed replies of ordinary chat use tools: replies grounded in selected documents and
  the non-streaming (JSON) send path do not. Earlier turns' tool calls are not replayed to the
  model in later turns (it sees the earlier answers' text).
- One action needing approval per model step; approvals expire lazily (checked on the next
  access); a paused reply blocks new messages in that conversation until decided or expired.
- No long-running or queued tools; every tool finishes within its timeout (≤ 60 s).
- Exactly-once is guaranteed for recording and claiming a call, not for the side effects of a
  handler interrupted by a process crash; the one mutating tool is idempotent itself.
- The web UI was tested in jsdom and the API end to end, not in a real browser.
- Organization switches only turn tools off or on; no per-team or per-user tool policies.

## 14. Phase 11 readiness

Ready, no blocker. Phase 11 (agent runtime) can reuse the registry, the executor (one checked
path for every call, with the principal re-read at run time; `Principal.for_agent` already
narrows permissions for an agent), the approval model (bound to exact arguments, pause and
resume), the audit actions and the gateway's tool calling. Two extensions it will need, by
design rather than as defects: invocations are tied to a conversation message
(`tool_invocations.message_id` is required and `ToolContext` carries the conversation), so agent
runs need a run-scoped record; and the scope `conversation` is the only one any tool declares
today, so agent-usable tools must declare an agent scope.

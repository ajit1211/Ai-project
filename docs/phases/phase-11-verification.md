# Phase 11 verification report: AI agents and task execution

Agents are configured assistants; a task (a *run*) gives one goal to an agent, which then works
through steps on its own in the background (one structured decision per step, tool calls through
the Phase 10 executor, approvals, budgets), can be watched live, paused, resumed and cancelled,
survives worker crashes, and never acts with more authority than the user who started it.
Design: [agents.md](../architecture/agents.md); API: [agents.md](../api/agents.md); decision:
[ADR-039](../adr/039-agent-runs-in-runtime.md).

Every result below comes from commands run on 2026-10-22 and 2026-10-24 against the code being committed.
Nothing here was run against a paid model: every model call in tests and end-to-end checks used
the deterministic mock provider.

## A. Implementation summary

**Repository audit (what was reused).** The Phase 10 tool executor (resolution, authorization
with permissions re-read from the database, argument validation, idempotency keys, conditional
claims, timeouts, output bounds, approvals bound to an arguments hash, audit), the AI Gateway's
structured output with one repair attempt and its usage ledger, the Phase 08/09 retrieval engine
(through `search_documents`), Runtime's principal reloading and Redis limiter (with its
in-process fallback), Core's schema ownership with per-table Runtime grants, the audit log, the
shared envelope and error codes, the SSE response helpers, and the permission catalog, which
already defined `agents.view/create/update/delete/execute/manage_all` and `ai.agent.execute`.

**Minimal corrections to earlier phases.** The tool executor's `Turn` was owned by a chat
message only; it now has exactly one owner, a message or a run (`run_id`), and
`tool_invocations` gained `run_id` with a check constraint (one owner) and a second ordering
key. `offer()` takes a scope (`conversation` or `agent`), an allow-list and whether native tool
calling is required. Runtime's audit writer learned the `AGENT_RUN_*` actions. The mock model
learned agent decisions. `Feature.AGENT` was added to the gateway. No new service, broker or
dependency.

**What was built.**

| Area | Result |
| --- | --- |
| Agent definitions | Core app `nexa_core.agents`: `/api/v1/agents/` (list, detail, create, change, soft delete), configuration-only validation (tools against the catalog, switches and the author's permissions; models available to the organization; local JSON Schemas; limits; unknown fields refused), versioning, creator rule, audit, admin |
| Persistent tasks | `agent_runs` and `agent_steps` with an explicit state machine (`queued`, `running`, `waiting_for_approval`, `paused`, `cancel_requested`, `completed`, `failed`, `cancelled`, `timed_out`); every transition one conditional statement |
| Decision loop | `AgentEngine`: prepare (permissions re-read, tools offered), settle leftovers, then checkpoint, decide (structured output `agent_decision`, validated twice), record, act |
| Decision schema | `call_tool`, `request_approval`, `report_progress`, `complete`, `fail`; bounded fields; no hidden-reasoning field |
| Tools | Only through the Phase 10 executor, re-authorized for every call; offered = registry ∩ agent scope ∩ agent's tools ∩ switches ∩ the user's permissions |
| Budgets | Steps, tool calls, wall-clock time (frozen while paused or waiting), tokens, retries, identical calls, invalid decisions, active runs per user and organization, submissions per hour, worker concurrency |
| Approvals | Registry policy, the agent's `all_tool_calls` policy, or the model's own `request_approval`; the run releases its worker while it waits; decisions bound to the arguments hash, requester-only, single-use, expiring; an expiry sweep |
| Pause, resume, cancel | Cooperative at checkpoints; cancel interrupts the current model or tool call within `AGENTS_CONTROL_POLL_SECONDS` |
| Background execution | In-process worker loops in Runtime claiming runs with `FOR UPDATE SKIP LOCKED` and leases; graceful shutdown; a standalone worker process (`python -m nexa_runtime.agents.worker`) |
| Recovery | Expired leases are taken over; open calls are settled from the executor's records (approved or validated calls run, interrupted read-only calls are reported, interrupted changes end the run instead of repeating); in-memory state rebuilt from the record; a crash-looping run times out |
| Usage accounting | Every model call metered by the gateway (success, invalid answer or failure) is added to the run (`feature = agent`, `source_type = agent_run`); a call interrupted by a stop is recorded as `cancelled` in the ledger only |
| Demonstration agent | Platform agent "Document Assistant" (`search_documents`, `document_metadata`; output schema `{answer, answered}`; cites `[R1]` references validated against what its searches returned) |
| Frontend | `/agents` (library), `/agents/new` and `/agents/:id/edit` (agent form), `/agents/:id/run` (new task, form generated from the input schema), `/agents/runs` (history with filters and pagination), `/agents/runs/:id` (live timeline over SSE with polling fallback, pause/resume/cancel, approval card, answer with sources, failure reasons); 85 new web tests |
| Observability | Structured logs per run, step, tool call, claim and release; audit for agent changes and run lifecycle; usage per run and per step |

**Decisions made without asking** (recorded in ADR-039): the runs API is `/api/v1/runs/` as in
the architecture document §18.7, not the brief's suggested `/agent-tasks`; one structured
decision per step instead of native tool calling; a configuration snapshot per run instead of
an `agent_versions` table; workers inside Runtime (it has the gateway and executor; Core's
Celery has neither) with the queue in PostgreSQL; the run's clock stops while it is paused or
waits for a person.

## B. Files changed

New:

| Path | What |
| --- | --- |
| `apps/core/nexa_core/agents/` | `models.py`, `apps.py`, `catalog.py`, `services.py`, `serializers.py`, `views.py`, `urls.py`, `admin.py`, migrations `0001_initial`, `0002_tenant_fks_and_runtime_grants`, `0003_run_time_left`, `0004_review_fixes` |
| `apps/core/nexa_core/tools/migrations/0003_invocation_run.py` | `tool_invocations.run_id`, nullable message and conversation, owner check, run ordering key |
| `apps/runtime/nexa_runtime/agents/` | `tables.py`, `repository.py`, `decisions.py`, `context.py`, `engine.py`, `runner.py`, `worker.py`, `service.py`, `schemas.py`, `errors.py`, `router.py` |
| `packages/nexa-common/src/nexa_common/tool_catalog.py` | The built-in tools as Core needs to know them |
| `apps/core/tests/test_agents_api.py` | 36 tests |
| `apps/runtime/tests/test_agents_unit.py` | 38 tests |
| `apps/runtime/tests/test_agents_runs.py` | 62 tests |
| `scripts/e2e_agents.py` | End-to-end check through the Vite proxy |
| `docs/architecture/agents.md`, `docs/api/agents.md`, `docs/adr/039-agent-runs-in-runtime.md`, this report | Documentation |
| `apps/web/src/` | `types/agents.ts`; `api/agents.ts`, `api/runs.ts` (+ tests); `components/agents/` (`labels.ts`, `inputSchema.ts`, `agentDraft.ts`, `FormField.tsx`, `badges.tsx`, `ConfirmAction.tsx`, `RunTimeline.tsx`, `RunResult.tsx`, `RunBudget.tsx`, + tests); `hooks/useAgents.ts`, `useRuns.ts`, `useRun.ts`, `useClock.ts`; pages `AgentsPage`, `AgentFormPage`, `NewRunPage`, `RunsPage`, `RunPage` (+ tests); `test/agentFixtures.ts`. Changed: `App.tsx` (routes), `AppShell.tsx` (nav link), `styles.css`, and backward-compatible options in the chat's `ApprovalCard`, `citations`, `Markdown`, `Icon` |

Modified:

| Path | Change |
| --- | --- |
| `apps/core/nexa_core/settings.py`, `urls.py` | The agents app and its URLs |
| `apps/core/nexa_core/audit/actions.py` | `AGENT_CREATED`, `AGENT_UPDATED`, `AGENT_DELETED`, `AGENT_RUN_STARTED`, `_COMPLETED`, `_FAILED`, `_CANCELLED`, `_PAUSED`, `_RESUMED` |
| `apps/core/nexa_core/tools/models.py` | `ToolInvocation.run`; message and conversation nullable; constraints |
| `apps/runtime/nexa_runtime/main.py`, `api.py`, `config.py` | Engine, runner and service wiring; `/runs` router; `AGENTS_*` settings |
| `apps/runtime/nexa_runtime/tools/executor.py`, `registry.py`, `repository.py`, `tables.py`, `activity.py`, `schemas.py`, `service.py`, `audit.py` | A `Turn` owned by a message or a run; `scopes`; run-level queries; agent audit actions; run ownership for invocation records |
| `apps/runtime/nexa_runtime/chat/service.py` | Counts calls per assistant message (owner change) |
| `packages/nexa-ai/src/nexa_ai/types.py`, `providers/mock.py` | `Feature.AGENT`; the mock's agent decisions and control phrases |
| `packages/nexa-ai/tests/test_building_blocks.py` | `agent` is now a valid feature (the invalid example became `workflow`) |
| `packages/nexa-common/src/nexa_common/errors.py` | `AGENT_UNAVAILABLE`, `RUN_NOT_ACTIVE`, `RUN_LIMIT_REACHED`, `TOOL_NOT_ALLOWED` |
| `conftest.py` | Re-seeds the platform agents for tests that flush the database |
| `.env.example`, `README.md`, `docs/adr/README.md` | Settings, current phase, commands, ADR index |

## C. Database changes

| Migration | Change |
| --- | --- |
| `agents.0001_initial` | Tables `agents`, `agent_runs`, `agent_steps`; unique slugs per organization while alive (`agents_org_slug_uniq`, `agents_platform_slug_uniq`), `agents_limits_chk`, `agent_runs_status_chk`, one idempotency key per user (`agent_runs_idempotency_uniq`), unique `(run, sequence)`; indexes for the owner's listing, the queue and per agent |
| `tools.0003_invocation_run` | `tool_invocations.run_id` (FK to `agent_runs`), `conversation_id` and `message_id` nullable, `tool_invocations_owner_chk` (a message or a run, never both), `tool_invocations_run_order_uniq`; the message ordering key became partial |
| `agents.0002_tenant_fks_and_runtime_grants` | Composite keys `(organization_id, run_id)` from `agent_steps` and `tool_invocations` to `agent_runs`, `ON DELETE CASCADE`; `GRANT SELECT, INSERT, UPDATE ON agent_runs, agent_steps TO nexa_runtime` |
| `agents.0003_run_time_left` | `agent_runs.time_left_seconds` (the frozen clock) |
| `agents.0004_review_fixes` | Idempotency keys unique per (organization, user, key); `agent_runs_conversation_same_org_fk` `(organization_id, conversation_id)` → `conversations` with `ON DELETE SET NULL (conversation_id)` |

Seed data: the platform agent "Document Assistant" (`post_migrate`, created when missing, never
overwritten). Applied with `manage.py migrate` on both local databases (PostgreSQL 18 on 5433;
PostgreSQL 16 + pgvector on 5434); `makemigrations --check` reports no changes.

## D. API reference

Full reference: [docs/api/agents.md](../api/agents.md).

| Method and path | Service | Purpose |
| --- | --- | --- |
| `GET /api/v1/agents/` | Core | Platform and organization agents (`?enabled=true`) |
| `GET /api/v1/agents/{id}/` | Core | Detail with instructions and schemas |
| `POST /api/v1/agents/` | Core | Create (201) |
| `PATCH /api/v1/agents/{id}/` | Core | Change; behaviour changes bump `version` |
| `DELETE /api/v1/agents/{id}/` | Core | Soft delete |
| `POST /api/v1/runs/` | Runtime | Submit a task (202; 200 for a repeated idempotency key) |
| `GET /api/v1/runs/` | Runtime | Your tasks, filters `status` (`active`, `finished`, any status) and `agent_id`, cursor pagination |
| `GET /api/v1/runs/{id}/` | Runtime | One task with usage, limits, result, sources, pending approval and allowed controls |
| `GET /api/v1/runs/{id}/steps/` | Runtime | The timeline (`after`, `limit`) |
| `GET /api/v1/runs/{id}/events/` | Runtime | Server-Sent Events: `run`, `step`, `end` |
| `POST /api/v1/runs/{id}/pause/`, `/resume/`, `/cancel/` | Runtime | Controls |
| `POST /api/v1/runs/{id}/approvals/{approval_id}/` | Runtime | Approve or reject the pending action |

New error codes: `AGENT_UNAVAILABLE` (404), `RUN_NOT_ACTIVE` (409), `RUN_LIMIT_REACHED` (429),
`TOOL_NOT_ALLOWED` (422).

## E. Frontend

| Page | Route | What it does |
| --- | --- | --- |
| Agent library | `/agents` | Platform and organization agents as cards (scope, tools with risk and approval badges, limits, version); Run; New agent; edit and delete where `can_edit` / `can_delete`; the Document Assistant first |
| Agent form | `/agents/new`, `/agents/:agentId/edit` | Name, description, instructions, model, tools (from `/tools/`), limits within server ranges, approval policy, optional input/output JSON schemas (checked as JSON client-side, validated by the server); server field errors shown next to their fields; edits send only changed fields |
| New task | `/agents/:agentId/run` | Goal plus inputs generated from the agent's input schema (strings, enums, numbers, booleans; JSON fallback); one idempotency key per form, so a double click starts one task; 422 `input.<field>` errors placed on the field; 429 explained |
| Task history | `/agents/runs` | Newest first, status and agent filters, cursor pagination ("Load more") |
| Task | `/agents/runs/:runId` | Status (live region), progress, budget meters (steps, tool calls, tokens, time left, frozen while paused or waiting), the step timeline with tool results, the approval card (approve/decline with the approval's `arguments_hash`; stale, closed and expired decisions explained), pause/resume/cancel driven by `can_*`, the answer (safe Markdown, `[R1]` citations linked to the sources, unavailable sources marked) and structured output, human failure reasons |

Live updates use `GET /runs/{id}/events/`; a stream that fails falls back to polling every two
seconds until the task ends, and everything stops when the page closes. Model-written text is
only rendered through the existing safe `Markdown` component (no `dangerouslySetInnerHTML`).
The answer view is lazy-loaded, like the chat, so the main bundle stays under Vite's 500 kB
warning (448 kB).

Not built: a comment box on approval decisions (the API accepts `comment`), a dashboard link,
and automatic refresh of the history list (it has a Refresh button). The pages were not opened
in a browser during this phase's checks; they are covered by component tests (jsdom) only.

## F. Security

| Requirement | How it is met | Verified by |
| --- | --- | --- |
| Never more authority than the user | The run's principal is the user's own permissions (`for_agent`), re-read from the database at every run start and every tool call; offered tools are an intersection; approvals never bypass permissions; losing `agents.execute` fails the run | `test_permissions_are_checked_again_when_the_run_executes`, `test_who_may_run_agents` |
| No user-submitted code | Agents hold configuration only; unknown fields refused; tool ids resolved by the code registry; no shell, eval, SQL or browser tool exists | `test_invalid_configurations_are_refused` (`handler`, `code`, `shell_exec`), `TestDecisions` (`run_shell`, `handler`) |
| No tool beyond the author's rights | `TOOL_NOT_ALLOWED` on save | `test_an_agent_gets_no_tool_its_author_could_not_use`, `test_switched_off_tools_cannot_be_given` |
| Prompt injection | Observations wrapped as data and the rules say so; the agent is offered only its tools; side effects need approval | `test_text_in_documents_cannot_steer_the_agent` |
| Tenant and user isolation | Organization- and owner-scoped reads (404 otherwise), including the event stream before it opens; composite tenant keys | `test_runs_are_private_to_their_owner`, `test_it_cannot_reach_another_organizations_documents`, `test_another_organizations_agent_is_not_found` |
| Approval integrity | Hash-bound, requester-only, single-use, expiring; expiry committed even though the request fails | `test_a_decision_is_bound_to_the_requester_and_the_exact_action`, `test_an_expired_approval_ends_the_run`, `test_the_sweep_ends_runs_whose_approval_expired` |
| No duplicate effects | Idempotent submissions (also under a race), idempotent calls, interrupted changes never repeated | `test_concurrent_submissions_with_one_key_start_one_run` (fails without the fix), `test_a_change_interrupted_mid_way_is_never_repeated` |
| Bounded cost | Budgets, caps, limiter, quotas, loop guard | `TestExecution`, `test_too_many_active_runs_are_refused`, `test_submissions_per_hour_are_limited` |
| No hidden reasoning stored; no sensitive logs | The decision schema has no reasoning field; logs and audit carry ids, counts and statuses; arguments redacted per tool; the admin hides goals, inputs, results, progress and failure text, and step details | `test_it_answers_from_the_documents_and_cites_them` (audit without the goal) |
| Honest citations | Only references from this run's results become sources; sources re-authorized on read | `test_a_reference_the_agent_never_saw_is_not_a_source` |

Threat model: [agents.md#security-and-threat-model](../architecture/agents.md#security-and-threat-model).

**Defects found and fixed during the phase's own review** (each now has a test):

1. An expired approval's updates were rolled back because the error was raised inside the
   transaction; the run stayed waiting. Now committed, audited, then reported.
2. Time spent paused or waiting for approval counted against the deadline, so a slow approval
   timed the task out on resume. The clock now stops (`time_left_seconds`).
3. Preparation ran outside the engine's error handling: an exception there left a run claimed
   with no deadline, to be reclaimed and fail forever. Now it fails the run.
4. Invalid model answers (and failed calls) were in the usage ledger but not in the run's
   counters; now every metered call is added.
5. Concurrent submissions with one idempotency key could raise a 500; the loser now returns the
   winner's run.
6. The executor's per-reply call cap (8) would have cut agents off below their own budget.
7. Runtime's audit writer did not know the agent actions (500 on submit, caught by tests).
8. A pause or cancel repeated on an already paused or cancelling run wrote a second audit row.
9. The event stream checked ownership after the response had started (200 + error event
   instead of 404).
10. A tool call stopped by a cancellation or shutdown left no observation for the model when
    the run continued elsewhere.

**Defects found by the multi-agent review and fixed** (each with a regression test in
`TestHardening`, `test_building_blocks.py` or `test_agents_api.py`):

11. *Double execution*: a queued run claimed by one worker loop stayed claimable (status still
    `queued`) and all loops of a process shared one claim id, so two loops could run the same
    task. Now a held queued run is not claimable, each loop claims under its own id, and
    preparation is conditional on the claim.
12. *Freezing Runtime with a schema*: a user-written JSON Schema whose `$ref`s fan out (each
    level referencing the next twice) passed `check_schema` and took exponential time to
    validate. `check_schema` (also used by structured output) now bounds the expanded
    references, and Runtime re-checks stored agent schemas before validating against them.
13. *A cancellation overwritten*: a cancel arriving while a step ran was replaced by
    `waiting_for_approval` or `paused`. Non-final releases now apply only from `running`; the
    engine then finishes the run as cancelled.
14. *Idempotency across organizations*: a key reused in another organization returned the first
    organization's task (with its result). Keys are now per organization (lookup and unique
    constraint, migration `agents.0004`).
15. *Conversations that could not be deleted*: a run linked to a conversation made its deletion
    fail (the database-level foreign key had no `ON DELETE`). Migration `agents.0004` adds a
    composite key with `ON DELETE SET NULL (conversation_id)`.
16. *Invalid outputs never failing the run*: the invalid-decision streak was reset before the
    output schema was checked, so repeated invalid outputs ran to the step limit.
17. *Caps bypassed by concurrency*: the active-task caps were counted outside the inserting
    transaction; they are now checked and the run inserted under an organization advisory lock.
18. *Smaller ones*: a decline comment over 267 characters overflowed a column (500); NUL in
    `input` or in agent schemas gave a 500; runs of a deactivated account kept executing
    (principals now require an active account); AI switched off ended runs as `internal_error`
    instead of `permission_denied`; the admin showed progress and failure text from private
    tasks; a repeated pause of a running task was audited twice; `queue_delay_ms` was always
    about zero; run logs lacked organization and request context; and seven documentation
    inaccuracies (SSE payload shape, usage-ledger equality, lease wording, limits wording,
    test counts, a wrong cross-reference).

## G. Verification results

All commands were run from the repository root on 2026-10-24, after the review fixes, with the
deterministic mock model and no API keys.

| Command | Result |
| --- | --- |
| `uv run pytest` (local PostgreSQL 18 on 5433, no pgvector) | **976 passed, 36 skipped**, 0 failed (the skips need pgvector) |
| `DATABASE_URL=…:5434/nexa uv run pytest` (PostgreSQL 16 + pgvector) | **1011 passed, 1 skipped**, 0 failed (the skip is the live OpenAI evaluation) |
| New Phase 11 tests within those | `test_agents_api.py` 36, `test_agents_unit.py` 38, `test_agents_runs.py` 62 (136), plus 2 schema tests in `test_building_blocks.py` |
| `uv run ruff check .` and `uv run ruff format --check .` | clean |
| `uv run mypy apps/core apps/runtime packages/nexa-common packages/nexa-ai` | Success: no issues found in 283 source files |
| `uv run python apps/core/manage.py makemigrations --check --dry-run` | No changes detected |
| `uv run python apps/core/manage.py check` | no issues |
| `manage.py migrate` on both databases | `agents.0001`–`0004`, `tools.0003` applied |
| `pnpm --filter @nexa/web test` | 356 passed (356) in 3 of 4 full runs; one run had 1 failure, intermittent and not reproduced (the web agent saw the existing `ChatPage.test.tsx` focus check flake the same way under load) |
| `pnpm --filter @nexa/web lint` / `typecheck` | clean |
| `pnpm --filter @nexa/web build` | built; main bundle 448 kB, no size warning |
| `uv run python scripts/e2e_agents.py` (running stack) | **72 passed, 0 failed** |
| `scripts/e2e_tools.py`, `e2e_chat.py`, `e2e_rag.py` (regression) | 45/45, 41/41, 31/31 |

Two fixes were shown to be tested by their tests: with the idempotency race fix disabled,
`test_concurrent_submissions_with_one_key_start_one_run` fails; restored, it passes.

## H. Manual setup

1. `./scripts/dev-db.ps1 start`, then `pnpm migrate` (applies `agents.0001–0004` and
   `tools.0003`, seeds the Document Assistant).
2. `pnpm dev`. Runtime starts the agent workers itself (`agents.runner.started` in its log); no
   new process is needed. A running Runtime from before this phase must be restarted.
3. Open http://localhost:5173/agents (for example as `demo@nexa.dev`), upload a document at
   `/documents`, then run the Document Assistant with a question about it.
4. Optional: real models with `AI_ANTHROPIC_API_KEY` or `AI_OPENAI_API_KEY` in `.env`; without
   keys the mock model plays the agents (scripted phrases are listed in [agents-testing.md](../development/agents-testing.md)).
5. Optional, for production-like setups: `AGENTS_WORKER_ENABLED=false` on the API process and
   `uv run python -m nexa_runtime.agents.worker` as separate processes.
6. `uv run python scripts/e2e_agents.py` checks the running stack.

## I. Remaining gaps

- **Real models were not exercised.** No API key is configured here; every check used the mock
  model. How well a real model follows the decision contract, and the cost of a typical task,
  are unmeasured.
- **No taint tracking** (stricter approvals once untrusted content entered a run), **no
  clarification state** (`awaiting_input`), **no plan step**, **no sub-agents or scheduling**.
- **The built-in tools are read-only except `save_memory`**: agents cannot yet send, write
  files or call external services (Phase 12+ tools).
- **Agents are per organization**: no sharing or public catalogue; no agent import or export.
- **Retention**: runs and steps are kept until their organization is deleted.
- **Live progress polls the database** once a second per open stream.
- **Redis is not installed locally**, so the submission limit ran on the limiter's in-process
  fallback (as in development); it was not exercised against a real Redis.
- **Interrupting a data-changing tool mid-call** relies on its handler tolerating cancellation,
  as in Phase 10.
- **The web pages were not exercised in a browser**: Vitest component tests (jsdom) only; no
  Playwright or manual browser pass. Not built: a decision comment box, a dashboard link and
  automatic refresh of the history.
- **The web review was not run**: the multi-agent review of the web UI failed on the account's
  session limit before any reviewer finished; only a manual reading of `api/runs.ts` and
  `hooks/useRun.ts` was done. Of the backend review, the five reviewers finished but most of
  their verifiers did not; every finding was therefore checked by hand against the code, and
  each one fixed has a regression test (section F).

## J. Phase 12 readiness

Phase 12 (specialized agents: research, data analysis, coding) can build directly on this
phase:

- A new agent is a seed entry plus new tools: registry entries with schemas, permissions, risk,
  approval policy and `scopes`; the engine, budgets, approvals, recovery and UI apply unchanged.
- Web search (the provider question from Phase 01 §30.6 is still open and must be answered
  then), fetching and sandboxed execution become tools; long jobs should start work and let the
  agent poll a second tool rather than hold a step open.
- A visible plan can be a new decision action recorded like progress; the context builder
  already rebuilds from steps.
- Taint tracking fits at the observation boundary (mark the run when a tool returns untrusted
  content) with the executor's existing "approve every call" switch.
- Runs already carry an owner, a snapshot and budgets, so Phase 15's orchestration can create
  child runs through the same service.

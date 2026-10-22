# ADR-039: Agent runs in Runtime: definitions in Core, a database-leased run queue, one structured decision per step, tools only through the Phase 10 executor

Status: accepted

## Context

Phase 11 turns NEXA's tool calling into agents: configured assistants that take a goal, work
through several steps (model decisions and tool calls) on their own, survive restarts, can
be paused, resumed, cancelled and approved, and never act beyond the authority of the user
who started them. Choices to make: where definitions and runs live, what executes a run and
how it survives a crash, how the model tells the engine what to do, how tools and approvals
are reused, how a configuration change affects running tasks, and how time and cost are
bounded.

The architecture document (section 14) proposes Celery AI workers, an `agent_versions`
table, model-native tool calling for each step and a WebSocket for live steps. Phase 11
departs from four of those points, deliberately; the reasons are below.

## Decision

1. **Definitions in Core, runs in Runtime.** Agents are configuration (`agents`: instructions,
   logical model, allowed tool ids, input and output JSON Schemas, limits, approval policy),
   created through Core's `/api/v1/agents/` with Core's permissions and audit. Runs and their
   steps (`agent_runs`, `agent_steps`; tool calls in `tool_invocations.run_id`) are written
   by Runtime through table grants; Core owns every table's schema, as for chat.
2. **A configuration is data, never code.** An agent names tool ids from the code-defined
   registry; it cannot carry a handler, a query, a script or a URL, and unknown fields are
   refused. Core validates it against `nexa_common.tool_catalog` (a Runtime test keeps that
   catalog equal to the registry): tools must exist, be switched on, and be usable by the
   author; schemas must be valid, local JSON Schemas; limits stay under platform maxima.
3. **A run pins its configuration.** Every behaviour change bumps the agent's `version`; a run
   stores a snapshot of the configuration it started with (`agent_snapshot`, limits already
   capped by `AGENTS_MAX_*`). Editing or deleting an agent never changes a running task, and
   history shows exactly what ran. No separate `agent_versions` table: the snapshot is the
   immutable version, without a join or a second write path.
4. **The queue is PostgreSQL; workers live in Runtime.** Runtime has the AI Gateway and the
   tool executor; Core's Celery has neither, and adding a broker round trip would not make a
   run more durable than its rows already are. Worker loops claim queued runs with
   `FOR UPDATE SKIP LOCKED`, hold a lease (`claimed_by`, `lease_expires_at`) renewed by a
   heartbeat, and give it up when the run pauses, waits or ends. A run whose lease expired
   belongs to a dead worker and is claimed again. The loops run inside each Runtime process
   by default, or in dedicated processes (`python -m nexa_runtime.agents.worker`).
5. **One structured decision per step.** Each step asks the model, through the gateway's
   structured output, for exactly one JSON decision (`call_tool`, `request_approval`,
   `report_progress`, `complete`, `fail`), validated twice (schema, then meaning). It works
   the same on every provider and on the mock, makes "finish" and "give up" explicit actions,
   and lets the engine enforce an output schema. The context is rebuilt from the recorded
   steps for every decision, so it cannot diverge from the record and another worker can
   continue the run.
6. **Tools only through the Phase 10 executor.** An agent's tool call is a `Turn` owned by the
   run instead of a message: the same resolution, authorization (permissions re-read from the
   database at every step, as the user who started the run), argument validation, idempotency
   key, conditional claim, timeout, output bounds, audit and approvals. What an agent may use
   is the intersection of the registry, the agent's allowed tools, the switches and the
   user's permissions; tools can exclude themselves from agents (`scopes`).
7. **Approvals pause the run, not a connection.** A call that needs approval (by the tool's
   policy, by the agent's `all_tool_calls` policy, or because the model asked with
   `request_approval`) releases the run in `waiting_for_approval`; the user's decision, bound
   to the exact arguments' hash, puts it back in the queue. The run's clock stops while it
   waits or is paused (`time_left_seconds`), so a slow human does not time a task out.
8. **Budgets are code.** Steps, tool calls, wall-clock time, tokens, identical repeated calls,
   invalid decisions, active runs per user and organization, and submissions per hour, each
   with a named failure reason. Every model call is in the usage ledger (`feature = agent`,
   `source_type = agent_run`), and the run's counters are the sum of what was measured.
9. **Progress over Server-Sent Events**, read from the database (any worker of any process
   may be executing the run), consistent with ADR-030; the steps and run endpoints serve
   clients that cannot stream.

## Alternatives

- **Celery workers in Core for runs.** Core has no AI Gateway or tool executor; running them
  there would duplicate both or call Runtime per step. Rejected; the run queue in PostgreSQL
  gives the durability we need with one fewer moving part.
- **A workflow engine (Temporal and the like).** Strong guarantees, but a new service to run
  and learn before NEXA has its first workflow; Phase 13's workflows can revisit it.
- **Native tool calling as the step protocol.** Fine for chat, but it leaves "done" and
  "cannot do this" implicit, cannot carry a validated final output, and differs per
  provider. Structured decisions keep the contract in NEXA's hands.
- **An `agent_versions` table.** Equivalent information, one more write and join; the run
  snapshot is enough until agents are shared across organizations or published.
- **A WebSocket per run.** Bidirectional channels are not needed: controls are plain POSTs.

## Consequences

- A run costs one model call per step (bounded by the budgets) plus its tool calls; the usage
  is visible on the run.
- A run survives a worker crash: it resumes from its steps. A read-only tool interrupted
  mid-call is reported to the model as interrupted; a call that changes data whose outcome
  is unknown ends the run (`interrupted`) instead of risking a second effect.
- In-process workers share the API's event loop and database pool; a deployment with heavy
  agent traffic should run dedicated worker processes (one setting).
- The "taint" rules of the architecture document (stricter approvals once untrusted content
  entered a run) are not implemented in Phase 11; the agent's `approval_policy` is the manual
  equivalent. Documented as a limitation.

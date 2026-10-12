# ADR-034: Structured memory in PostgreSQL, team scope as project memory, in-process extraction

Status: accepted

## Context

Phase 06 asks for long-term memory with conversation, user, organization and project scopes,
extraction from conversations, retrieval, summaries of long conversations, and full user
control, without vector search yet and without new services. Three decisions shape it:
where memories live and how they are found, what "project" means when NEXA has no project
entity, and how the work after a reply runs.

## Decision

1. **One `memories` table in PostgreSQL**, owned by Core's migrations and written by Runtime
   (as conversations are, ADR-033), with explicit scope, kind, source, confidence, status and
   provenance. Retrieval is lexical full-text search (a generated `tsvector`, GIN index) plus a
   transparent score (importance, confidence, recency, scope). The visibility rule is a SQL
   predicate applied before ranking.
2. **Project memory is team memory.** The Phase 03 team already has server-side membership
   and administration; `scope = team` memories are visible to its members and to
   `memory.manage_all` holders. No project entity is added in this phase.
3. **Extraction and summaries run in-process in Runtime** after a completed reply, in tasks
   the chat engine tracks and awaits at shutdown. Idempotency comes from the database: one
   extraction claim per message, unique summary versions. Retention runs as a Core Celery task.

## Alternatives

- **pgvector now.** Better recall for paraphrases, but it needs an embedding model, a
  backfill and an evaluation set, which Phase 08 brings. Rejected for this phase; the search
  function keeps its signature so a vector term can be added.
- **A projects feature** (entity, members, UI). Correct long-term, but a feature in itself
  and outside this phase's scope. A later `projects` table can take over by adding
  `project_id` beside `team_id` and a `project` scope; existing team memories stay valid.
- **Celery for extraction.** Runtime has no worker, the work must start the moment a reply
  completes, and Redis is optional in development. A queue would add a moving part for no
  benefit at this scale. The cost: an extraction interrupted by a crash is not retried.
- **Storing every message as memory.** Rejected by the brief and by privacy: memory is
  selected, reviewed and deletable.

## Consequences

- No new service, no new infrastructure; memories share the database, backups and tenancy
  rules of everything else.
- Recall misses paraphrases until semantic retrieval arrives.
- "Project" in the UI and docs reads "team" until projects exist.
- Each completed reply costs one small-model call for extraction (metered as `memory`).

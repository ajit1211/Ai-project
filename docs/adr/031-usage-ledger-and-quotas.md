# ADR-031: An append-only usage ledger, integer cost, and reserve-then-settle quotas

Status: accepted (deviates from Phase 01 section 25 as recorded below)

## Context

Cost must be attributable to an organization, a user, a feature and a model from the first
call, and a tenant must not be able to spend without bound, whether by accident, by
concurrency or by creating organizations. Phase 01 planned money budgets with atomic
reservations, monthly limits and 402 responses for Phase 04, before there was a plan model
or a billing phase to attach them to.

## Decision

- Every gateway call appends one row to `ai_usage_events`: metadata only, never prompt or
  completion text. Its id is the gateway call id returned to the caller. Identifiers are
  plain UUIDs (no foreign keys), nothing references the table, and a trigger refuses UPDATE
  and DELETE for every role, so the ledger survives deletions and stays partitionable.
- Cost is computed at write time in micro-dollars with integer arithmetic from the price
  version in force (a new row per price change), with separate rates for plain input,
  cached input, cache writes and output. Rows are never recomputed. A model without a price
  records a null cost and logs an error on every call.
- Tokens are summed over every attempt the provider may have billed: the serving call, the
  repair call, and abandoned attempts (timed-out attempts count their estimated input).
  Counts the provider did not report (cancelled or broken streams) are estimated and marked
  `usage_source = estimated`.
- Quotas instead of money budgets for Phase 04: requests per minute and per day, tokens per
  day and requests in flight, per organization (overridable by platform staff) and per user
  (platform settings keyed by the user alone, so they follow a person across organizations).
  A Lua script reserves the request and the call's worst-case tokens atomically before the
  provider is called and refuses if any limit would be exceeded; the reservation is settled
  to the real count when the call ends. A breach is `AI_QUOTA_EXCEEDED` (429) with
  `Retry-After`, superseding Phase 01's 402 `QUOTA_EXCEEDED` / `BUDGET_EXCEEDED`.
- When Redis is unreachable, quotas do not fail open (a deliberate deviation from ADR-025):
  each process enforces its share of every limit in memory and logs that it is degraded.

## Alternatives

- Money budgets now: needs plans, monthly windows, alerts and a billing model that do not
  exist yet; token and request ceilings give the same protection against runaway spend
  without inventing a plan structure that Phase 20 will define.
- Check-then-charge token quotas: simpler, but N concurrent requests all pass against the
  same remaining budget; the daily cap would be a lagging indicator rather than a limit.
- Failing open without Redis, as login rate limits do: a Redis outage would make AI free.
- Foreign keys on the ledger: cascades would erase spend history, and a foreign key into the
  ledger would block partitioning.

## Consequences

- Phase 05 dashboards and rollups read the ledger; nothing else needs to change for them.
- Budgets, plans and the 80 % / 100 % actions of Phase 01 section 25 are deferred to the
  phase that introduces plans; `ai_organization_policies` is named so a
  `monthly_budget_micros` column can be added without renames.
- In degraded mode, per-process day counters restart on process restart; the
  `AI_QUOTA_PROCESS_COUNT` divisor must not be set below the real task count.
- A development machine without Redis runs in degraded mode all the time, which is also how
  that path stays tested.

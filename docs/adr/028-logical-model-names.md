# ADR-028: Logical model names resolved by a database catalog

Status: accepted

## Context

Application code must not name provider models: models are retired, repriced and
outperformed every few months, and a provider outage must be survivable without a deploy.
Organizations will eventually get different model access and different prices.

## Decision

- Callers use logical names: `nexa-fast`, `nexa-balanced` (the default), `nexa-reasoning`,
  `nexa-vision`. These replace the `chat.fast` / `chat.default` names of Phase 01 section
  12.2 with the names the Phase 04 brief uses; embeddings will add `nexa-embed`.
- The catalog lives in PostgreSQL, owned by Core: concrete models (`ai_models`), versioned
  prices (`ai_model_prices`), aliases (`ai_model_aliases`) and ordered alias targets
  (`ai_alias_targets`). The target order is the fallback chain. Runtime reads a snapshot every
  30 seconds, so changes in the Django admin take effect within a minute without a restart.
- A seed catalog creates missing rows after every migration and never overwrites edited ones,
  so a fresh database works and operator edits survive deploys.
- Model access is allow-by-default per alias plus explicit per-organization grants for
  restricted aliases. Organization-specific overrides, weights and deny rules (Phase 01
  section 12.2) are deferred until there is a plan model to attach them to.
- Credentials are never in the catalog; a model whose provider has no key in this process
  is simply not a candidate.

## Alternatives

- Configuration files or settings for the catalog: no admin UI, a deploy per change, and no
  way to express per-organization grants.
- One table with a `logical_name` column per model row: cannot hold alias metadata without
  duplication, and the same model cannot back two aliases without copying its price.

## Consequences

- Switching the default provider is a priority edit in the admin.
- The registry snapshot means a change can lag by up to 30 seconds, and up to five minutes
  while the database is unreachable (after which AI requests fail rather than serve an older
  catalog).

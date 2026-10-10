# ADR-026: Every model call goes through one in-house AI Gateway

Status: accepted (refines Phase 01 ADR-004 for Phase 04)

## Context

NEXA will call language models from chat (Phase 05), retrieval (Phases 08 and 09), agents
(Phase 11) and workflows (Phase 16). Each call must be authorized, limited, metered and
normalized the same way, and the platform must be able to change or add providers without
touching application code. The first LLM call is the moment cost tracking has to exist
(Phase 01 principle 10).

## Decision

- One library, `nexa_ai`, is the only way to call a model. It takes a canonical request,
  resolves a logical model, enforces permission, organization policy and quotas, executes
  with retries and fallback under one deadline, validates structured output, meters tokens
  and cost into the ledger, and returns NEXA's own response and stream shapes.
- The library is framework-free: no FastAPI, no database, no Redis. Runtime injects a
  catalog source, a quota guard and a usage sink. The same object will serve the AI
  workers, where there is no HTTP request at all.
- The gateway re-checks the `ai.chat` permission itself, so no caller, including a future
  agent, can route around authorization by calling it directly.
- Caching of answers is refused by design; registry and policy snapshots are the only caches.

## Alternatives

- Call provider SDKs from each feature. Rejected: every feature would re-implement limits,
  metering and error handling, and would drift.
- LangChain or LiteLLM as the core abstraction. Rejected in Phase 01 (ADR-004): the adapter
  surface is small and NEXA needs control over streaming, metering and error semantics.
  LiteLLM remains a possible single adapter for long-tail providers.
- A separate gateway service. Rejected: a network hop on every token stream for no
  isolation benefit at this scale; the library boundary gives the same independence.

## Consequences

- Adding a feature that uses AI is: build a `ChatRequest`, call the gateway with a server-side
  context. There is nothing else to get right.
- The gateway's tests run against the mock provider in well under a second; the real
  adapters are tested against canned HTTP responses.
- Provider changes are catalog rows, not releases.

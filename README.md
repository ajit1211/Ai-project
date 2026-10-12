# NEXA

AI Intelligence & Agent Platform. The architecture and the 20-phase plan are in
[docs/architecture/NEXA-architecture.md](docs/architecture/NEXA-architecture.md); decisions
made since are in [docs/adr/](docs/adr/).

**Current phase: 06, conversation memory.** NEXA remembers selected things about you and
your work across conversations: what you state (and approve) becomes a memory, relevant
memories and your standing preferences are recalled into each reply's context, long
conversations get a rolling summary, and everything is visible, editable and deletable at
`/memories`, with automatic memory on or off. Memories are private, team or organization
scoped, never cross a tenant, and never hold secrets. Details:
[memory-engine.md](docs/architecture/memory-engine.md), [memory-security.md](docs/architecture/memory-security.md),
endpoints in [docs/api/memories.md](docs/api/memories.md), decision in [ADR-034](docs/adr/034-memory-engine.md).

Earlier phases: the chat engine (Phase 05, [chat-engine.md](docs/architecture/chat-engine.md),
[docs/api/conversations.md](docs/api/conversations.md), ADR-033; private conversations at
`/chat`, streamed with the `nexa.chat.v1` protocol), the AI Gateway (Phase 04, [ai-gateway.md](docs/architecture/ai-gateway.md),
[docs/api/ai.md](docs/api/ai.md), ADR-026 to 032; the developer playground is at `/playground`)
and identity and access (Phase 03, [authentication.md](docs/architecture/authentication.md),
[authorization.md](docs/architecture/authorization.md), [multi-tenancy.md](docs/architecture/multi-tenancy.md),
[security.md](docs/architecture/security.md)).

```text
React (/chat, /memories) ──► edge (Vite proxy) ──► FastAPI /api/v1/conversations, /memories, /ai
                                           └──► Django   /api/v1/auth, /users, /organizations, /ai/usage
FastAPI ──► memory engine (recall, extraction, summaries) ──► AI Gateway ──► Anthropic | OpenAI | mock
                                                   (one PostgreSQL; Django owns the schema)
```

| Part | Where | What it is |
| --- | --- | --- |
| React | http://localhost:5173 | `apps/web`: chat workspace, memories, sign-in, settings, organization switcher, the AI playground; `/status` shows the stack |
| Django | http://localhost:8000 | `apps/core`: the control plane (identity, organizations, roles, audit, AI catalog, usage ledger, the schema of every table, admin) |
| FastAPI | http://localhost:8001 | `apps/runtime`: the AI plane (`/api/v1/conversations/*`, `/api/v1/memories/*`, `/api/v1/ai/*`) |
| PostgreSQL | localhost:5433 (project-local) | One database, two roles: `nexa_core` and `nexa_runtime` |
| Redis | localhost:6379 | Rate limits, AI quotas, Celery broker and pub/sub (db 0), cache (db 1) |
| Celery | worker process | Emails and background jobs on `core.default` and `core.email` |

In development the browser talks only to port 5173: Vite proxies `/api` to Django or FastAPI
using the same path rules as the production edge.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python 3.13 for the project automatically)
- Node 24 and pnpm
- PostgreSQL 16+ installed (the binaries are enough; see below) and, optionally, a Redis-
  compatible server ([Memurai](https://www.memurai.com/) on Windows, or `docker compose up -d`)

## First-time setup

```powershell
uv sync                       # Python 3.13 + all workspace packages into .venv
pnpm install                  # web dependencies
Copy-Item .env.example .env   # then set DJANGO_SECRET_KEY to a long random string

# Database. Option A (no system password needed): a project-local PostgreSQL on port 5433.
# Create its data folder once (adjust the PostgreSQL version/path if yours differs):
& "C:\Program Files\PostgreSQL\18\bin\initdb.exe" -D "$env:LOCALAPPDATA\nexa\postgres-18\data" -U postgres --pwfile=<(echo postgres) --auth=scram-sha-256 -E UTF8 --no-locale
# then set `port = 5433` in that folder's postgresql.conf and:
./scripts/dev-db.ps1 start
./scripts/dev-db.ps1 init     # creates the nexa database and roles

# Option B: an existing PostgreSQL on 5432 (asks for your postgres password), then set 5432 in .env:
psql -h localhost -U postgres -f scripts/db/init.sql

pnpm migrate                  # applies migrations and syncs the permission catalog
```

Without Redis everything still works: login rate limits fail open with a warning, AI quotas
are enforced per process, emails are sent inline, and the status page shows Redis and Celery
as down. Without AI provider keys the mock provider answers; add `AI_ANTHROPIC_API_KEY` or
`AI_OPENAI_API_KEY` to `.env` for real models (see `.env.example`).

## Daily commands

| Command | What it does |
| --- | --- |
| `./scripts/dev-db.ps1 start` | Starts the project-local PostgreSQL (after every reboot) |
| `pnpm dev` | Starts React, Django, FastAPI and the Celery worker together |
| `pnpm test` | Python tests (pytest) and web tests (Vitest) |
| `pnpm lint` | ruff, mypy (strict), ESLint and the TypeScript compiler |
| `pnpm migrate` | Applies database migrations |
| `uv run python scripts/e2e_auth.py` | Walks the whole sign-up and sign-in flow against the running stack |
| `uv run python scripts/e2e_ai.py` | Walks the AI gateway (models, chat, structured output, streaming, errors, usage) against the running stack |
| `uv run python scripts/e2e_chat.py` | Walks the chat engine (conversation, streamed reply, history, retries, failures, isolation, deletion) against the running stack |
| `uv run python scripts/e2e_memory.py` | Walks the memory engine (extraction, refusals, approval, manual memories, search, isolation, settings, clearing) against the running stack |
| `uv run python apps/core/manage.py createsuperuser` | Creates an admin login for http://localhost:8000/admin/ |

Emails in development are written to `.local/mail/`; open the newest file for the link.
Each service answers `GET /healthz` and `GET /readyz`.

## Notes

- Celery runs with `--pool solo` locally because its default process pool does not support
  Windows. Deployed workers run on Linux with the normal pool.
- The development JWT signing key is generated into `.secrets/` on first use; deployed
  environments provide `JWT_PRIVATE_KEY` from the secret store.
- Install the git hooks once with `uv run pre-commit install`.

# NEXA

AI Intelligence & Agent Platform. The architecture and the 20-phase plan are in
[docs/architecture/NEXA-architecture.md](docs/architecture/NEXA-architecture.md); decisions
made since are in [docs/adr/](docs/adr/).

**Current phase: 04, the AI Gateway.** Every model call goes through one framework-free
gateway (`packages/nexa-ai`): logical model names resolved by a database catalog, Anthropic
and OpenAI adapters plus a deterministic mock, retries and fallback under one deadline,
SSE streaming, validated structured output, an append-only usage ledger with per-call cost,
and reserve-then-settle quotas. The React playground at `/playground` exercises all of it.
Details: [docs/architecture/ai-gateway.md](docs/architecture/ai-gateway.md); endpoints in
[docs/api/ai.md](docs/api/ai.md); decisions in [ADR-026 to 032](docs/adr/README.md). Phase 03
(identity and access) is described in [authentication.md](docs/architecture/authentication.md),
[authorization.md](docs/architecture/authorization.md), [multi-tenancy.md](docs/architecture/multi-tenancy.md)
and [security.md](docs/architecture/security.md).

| Part | Where | What it is |
| --- | --- | --- |
| React | http://localhost:5173 | `apps/web`: sign-in, settings, organization switcher, the AI playground; `/status` shows the stack |
| Django | http://localhost:8000 | `apps/core`: the control plane (identity, organizations, roles, audit, AI catalog and usage ledger, admin) |
| FastAPI | http://localhost:8001 | `apps/runtime`: the AI plane (`/api/v1/ai/*`: models, chat, streaming) |
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
| `uv run python apps/core/manage.py createsuperuser` | Creates an admin login for http://localhost:8000/admin/ |

Emails in development are written to `.local/mail/`; open the newest file for the link.
Each service answers `GET /healthz` and `GET /readyz`.

## Notes

- Celery runs with `--pool solo` locally because its default process pool does not support
  Windows. Deployed workers run on Linux with the normal pool.
- The development JWT signing key is generated into `.secrets/` on first use; deployed
  environments provide `JWT_PRIVATE_KEY` from the secret store.
- Install the git hooks once with `uv run pre-commit install`.

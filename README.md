# NEXA

AI Intelligence & Agent Platform. The architecture and the 20-phase plan are in
[docs/architecture/NEXA-architecture.md](docs/architecture/NEXA-architecture.md); decisions
made since are in [docs/adr/](docs/adr/).

**Current phase: 11, AI agents and tasks.** At `/agents`, give a goal to an agent and let it
work: it decides one step at a time, uses the tools it is allowed (through the same checked
executor as the chat), asks you before any action that needs approval, and answers with
citations. Tasks run in the background, survive restarts, stay within step, tool-call, time and
token budgets, and can be watched live, paused, resumed or cancelled; an agent never has more
authority than the person who runs it. Start with the built-in **Document Assistant** (it
searches your documents and cites them), or create your own agents. Details:
[agents.md](docs/architecture/agents.md), [docs/api/agents.md](docs/api/agents.md), decision in
[ADR-039](docs/adr/039-agent-runs-in-runtime.md). Without an API key the mock model plays the
Document Assistant; scripted phrases such as `[[agent:call_tool calculator {"expression":
"6*7"}]] [[agent:complete Done]]` drive any other agent.

Phase 10, AI tool calling: in the chat, the AI can use tools while it answers (a calculator,
the clock, your documents, your workspace, and saving to your memories after your approval);
every call is checked, run by NEXA's own code under limits, recorded and shown in the reply
([tools.md](docs/architecture/tools.md), [docs/api/tools.md](docs/api/tools.md),
[ADR-038](docs/adr/038-tool-calling-in-runtime.md)).

Phase 09, vector search, embeddings and hybrid retrieval: document passages are
embedded in the background (OpenAI `text-embedding-3-small` by default; a deterministic mock in
development) and stored with pgvector; grounded questions are answered from keyword search,
semantic (vector) search, or both fused (hybrid, the default), always inside your access
rights. Where pgvector or an embedding key is missing, answers fall back to keyword search and
say so. Details: [vector-search.md](docs/architecture/vector-search.md) (including
[installing pgvector](docs/architecture/vector-search.md#installing-pgvector)), decision in
[ADR-037](docs/adr/037-pgvector-hybrid-retrieval.md). The local Windows database has no
pgvector, so `pnpm dev` runs keyword search only unless you install it.

Phase 08, document-grounded answers (RAG): in the chat, choose documents (or all documents you
may read) and ask about them; the reply cites passages (`[S1]`) with source cards that open the
document at the right page, and says so when nothing matches
([rag.md](docs/architecture/rag.md), [docs/api/conversations.md](docs/api/conversations.md#document-grounded-answers),
[ADR-036](docs/adr/036-rag-in-chat-lexical-baseline.md)).

Phase 07, documents: upload PDF, Word, text, Markdown and CSV files at `/documents`; they are
stored privately, their text is extracted in the background, and the library shows status,
text, warnings and sharing ([documents.md](docs/architecture/documents.md),
[docs/api/documents.md](docs/api/documents.md), [ADR-035](docs/adr/035-document-uploads-through-core.md)).
Phase 06, conversation memory: selected memories and summaries, managed at `/memories`
([memory-engine.md](docs/architecture/memory-engine.md), [ADR-034](docs/adr/034-memory-engine.md)).

Earlier phases: the chat engine (Phase 05, [chat-engine.md](docs/architecture/chat-engine.md),
[docs/api/conversations.md](docs/api/conversations.md), ADR-033; private conversations at
`/chat`, streamed with the `nexa.chat.v1` protocol), the AI Gateway (Phase 04, [ai-gateway.md](docs/architecture/ai-gateway.md),
[docs/api/ai.md](docs/api/ai.md), ADR-026 to 032; the developer playground is at `/playground`)
and identity and access (Phase 03, [authentication.md](docs/architecture/authentication.md),
[authorization.md](docs/architecture/authorization.md), [multi-tenancy.md](docs/architecture/multi-tenancy.md),
[security.md](docs/architecture/security.md)).

```text
React (/chat, /documents, /memories, /agents) ──► edge (Vite proxy) ──► FastAPI /api/v1/conversations, /memories,
                                                                         /ai, /tools, /runs
                                                                └──► Django  /api/v1/auth, /users, /organizations,
                                                                             /documents, /agents, /ai/usage
Django ──► Celery (core.documents): extraction, search index, embeddings ──► private file storage
FastAPI ──► memory engine + document retrieval (RAG) + tools ──► AI Gateway ──► Anthropic | OpenAI | mock
FastAPI ──► agent run workers (queue in PostgreSQL, leases) ──► decision loop ──► tools / AI Gateway
                                                   (one PostgreSQL; Django owns the schema)
```

| Part | Where | What it is |
| --- | --- | --- |
| React | http://localhost:5173 | `apps/web`: chat workspace, documents, memories, agents and tasks, sign-in, settings, organization switcher, the AI playground; `/status` shows the stack |
| Django | http://localhost:8000 | `apps/core`: the control plane (identity, organizations, roles, audit, AI catalog, usage ledger, agent definitions, the schema of every table, admin) |
| FastAPI | http://localhost:8001 | `apps/runtime`: the AI plane (`/api/v1/conversations/*`, `/api/v1/memories/*`, `/api/v1/ai/*`, `/api/v1/tools/*`, `/api/v1/runs/*`) and the agent run workers |
| PostgreSQL | localhost:5433 (project-local) | One database, two roles: `nexa_core` and `nexa_runtime` |
| Redis | localhost:6379 | Rate limits, AI quotas, Celery broker and pub/sub (db 0), cache (db 1) |
| Celery | worker process | Emails and background jobs on `core.default`, `core.email` and `core.documents` (extraction, indexing, retention) |
| File storage | `.local/documents` | Uploaded files, private, git-ignored (an object store when deployed) |

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
| `uv run python scripts/e2e_documents.py` | Walks the document pipeline (uploads in every format, processing, indexing, text, preview, download, refusals, retry, isolation, deletion) against the running stack |
| `uv run python scripts/e2e_rag.py` | Walks document-grounded chat (sources, citations, no-evidence, fabricated citations, isolation, deletion) against the running stack |
| `uv run python scripts/e2e_tools.py` | Walks tool calling in the chat (calculator, clock, document tools, approvals, switches, isolation) against the running stack |
| `uv run python scripts/e2e_agents.py` | Walks agents and tasks (library, agent versions, the Document Assistant with citations, steps and progress stream, approvals, pause/resume, cancel, isolation) against the running stack |
| `uv run python -m nexa_runtime.agents.worker` | A process that only executes agent tasks (for `AGENTS_WORKER_ENABLED=false` on the API processes); `pnpm dev` does not need it |
| `uv run pytest -s apps/runtime/tests/test_rag_eval.py` | Prints the retrieval and citation evaluation on the synthetic set |
| `uv run pytest -s apps/runtime/tests/test_rag_eval_modes.py` | Compares lexical, semantic and hybrid retrieval (needs pgvector) |
| `uv run python apps/core/manage.py setup_vector_search` | Says whether semantic search runs here and creates the vector table when pgvector is installed |
| `uv run python apps/core/manage.py embed_documents` | Embeds documents that have no current vectors (`--force` re-embeds all) |
| `uv run python apps/core/manage.py createsuperuser` | Creates an admin login for http://localhost:8000/admin/ |

Emails in development are written to `.local/mail/`; open the newest file for the link.
Each service answers `GET /healthz` and `GET /readyz`.

## Notes

- Celery runs with `--pool solo` locally because its default process pool does not support
  Windows. Deployed workers run on Linux with the normal pool.
- The development JWT signing key is generated into `.secrets/` on first use; deployed
  environments provide `JWT_PRIVATE_KEY` from the secret store.
- Install the git hooks once with `uv run pre-commit install`.
- Without Redis, document processing and indexing run inside Core right after the upload
  (the broker fallback), so the worker is optional locally. With Redis, the worker must
  listen on `core.documents` (`pnpm dev` does).
- Windows Defender's Controlled Folder Access protects `Documents`: if the repository lives
  there, allow Python, uv, node, git and your editor through it, or writes (tests, formatting,
  uploads, commits) fail with "file not found" errors.

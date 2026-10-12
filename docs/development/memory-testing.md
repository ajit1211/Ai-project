# Testing the memory engine

## Run it

```powershell
./scripts/dev-db.ps1 start   # after every reboot
pnpm migrate                 # applies the memory migrations
pnpm dev
```

Open http://localhost:5173/chat. Without provider keys the mock provider answers, including
the memory engine's two structured requests: it recognizes a few phrase shapes in your
message so the whole pipeline can be exercised offline.

| Type this in a chat | Then on /memories |
| --- | --- |
| `Remember that I deploy on Fridays.` | An active private fact, "I deploy on Fridays", linked to the conversation |
| `I prefer short answers.` | An active preference; it is recalled in every later conversation |
| `Maybe I should learn Rust.` | A candidate under *Waiting for approval*: approve or reject it |
| `Remember that my password is hunter2.` | Nothing: secrets are refused |
| `Remember that my diagnosis is flu.` / `... my email is me@example.com` | Nothing: sensitive data is never stored automatically |
| `We decided to use PostgreSQL.` | An active decision |

Then ask something related in a new conversation (`How should I deploy?`): the reply's
context includes the matching memory. Turn *Remember things from my conversations* off and
nothing new is stored. A conversation longer than 20 messages gets a summary (and an episode
memory under *Past conversations*). `uv run python scripts/e2e_memory.py` walks the same path
against the running stack.

## Automated tests

| Suite | What it covers |
| --- | --- |
| `apps/runtime/tests/test_memory_api.py` | CRUD; secrets refused; one memory per statement; supersede by key; isolation from colleagues, administrators and other organizations (detail, list, search, approve, update, delete); team memories for team members and managers only; organization memories by administrators only; no owner or organization from the client; a conversation of another member refused; filters, search ranking, cursor pagination; approve and reject; clear only the caller's private memories; settings; expired, superseded and rejected memories never recalled. Chat integration: explicit statements stored with their source; inferences wait; secrets, health data and emails never extracted; recalled memories enter the context as data after the protected instructions (prompt-injection text stays inside the data block); recall and extraction failures never fail the chat; extraction runs once per message; the extraction setting; summaries every N messages without losing messages, used in the next context instead of the messages they cover; deleting a conversation |
| `apps/runtime/tests/test_memory_unit.py` | The policy (secrets, personal identifiers, Luhn, grounding, keys, hashing), extraction judging in order, malformed model output, the context budget with memories and summaries (priorities, truncation, protected instructions, conversation boundary), summary parsing |
| `apps/core/tests/test_memory_models.py` | Scope targets, one active memory per subject, ranges, tenant consistency of every reference, summary versions, one extraction per message, what deleting a conversation, a team or a successor leaves, full-text search, role permissions |
| `apps/core/tests/test_memory_retention.py` | The purge: expiry, per-member retention, orphans, final delete |
| `apps/web/src/pages/MemoriesPage.test.tsx`, `api/memories.test.ts` | List with scope, source and origin; filters and search; approve, reject, edit, delete with confirmation; add by hand; settings; clear with confirmation; refusals shown |

`uv run pytest` and `pnpm --filter @nexa/web test` run them; none calls a paid provider.

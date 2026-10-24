# Testing agents and tasks locally

## Run it

```powershell
./scripts/dev-db.ps1 start   # after every reboot
pnpm migrate                 # agents.0001–0004, tools.0003; seeds the Document Assistant
pnpm dev                     # React :5173, Django :8000, FastAPI :8001 (agent workers inside), Celery
```

The Runtime log shows `agents.runner.started` once the workers are up. Open
http://localhost:5173/agents. Without provider keys every decision comes from the
deterministic mock model; with `AI_ANTHROPIC_API_KEY` or `AI_OPENAI_API_KEY` in `.env` the
real models decide (see [local-ai-testing.md](local-ai-testing.md)).

## The Document Assistant

Upload a document at `/documents` (for example a Markdown file saying "Customers may request a
refund within thirty days of purchase.") and wait until it is indexed. Then run the
**Document Assistant** with "How many days do customers have to request a refund?". The task
searches, then answers with a citation (`[R1]`) and the document as its source. Ask about
something the documents do not cover and it says so (`answered: false`).

## Scripting any agent with the mock model

Create an agent at `/agents` with the tools you want (for example the calculator and "Save to
memory"), then put control phrases in the task's goal. Each phrase is one decision, in order
(development and test only):

| Goal contains | The agent |
| --- | --- |
| `[[agent:call_tool calculator {"expression": "6*7"}]]` | Calls the calculator; the result appears in the timeline |
| `[[agent:request_approval calculator {"expression": "2+3"}]]` | Proposes the call and waits for your approval, although the calculator needs none |
| `[[agent:call_tool save_memory {"content": "I prefer metric units"}]]` | Waits for approval (the tool requires it); approve or decline in the task view |
| `[[agent:progress Half way]]` | Reports progress (the status line under the title) |
| `[[agent:complete The answer is 42.]]` | Finishes with that answer |
| `[[agent:complete {"n": 3}]]` | Finishes with structured output (for agents with an output schema) |
| `[[agent:fail The documents are missing.]]` | Gives up with that reason |
| `[[agent:loop calculator {"expression": "1+1"}]]` | Repeats the same call: refused once, then the task fails with `loop_detected` (or `step_limit` with a low step limit) |
| `[[agent:invalid]]` / `[[agent:garbage]]` | Answers that are not valid decisions: twice in a row fails with `validation_failed` |
| `[[slow:3000]] …` | The first decision takes three seconds: time to press **Pause** or **Cancel** |
| `[[fail:auth]] …` | The model call fails: `provider_error` |

For example: `[[agent:progress Starting]] [[agent:call_tool calculator {"expression": "6*7"}]]
[[agent:complete The answer is 42.]]`.

## Things to try

- **Pause** a task during `[[slow:3000]]`: it pauses after the current step; **Resume** continues
  it, and the time it spent paused does not count against its time limit.
- **Cancel** during `[[slow:8000]]`: the model call is interrupted within about two seconds.
- Let an approval expire (`AGENTS_APPROVAL_TTL_SECONDS=60` in `.env`, restart Runtime, wait a
  minute, then approve): the task fails with `approval_expired`.
- Sign in as another user: the tasks are not visible, and their URLs answer 404.
- Stop Runtime while a task runs `[[slow:8000]]` and start it again: the task goes back to the
  queue and continues (or, if it was killed hard, is taken over once its lease expires, after
  `AGENTS_LEASE_SECONDS`).
- Edit an agent while a task of it runs: the task keeps the version it started with.

## Scripted walk-through

`uv run python scripts/e2e_agents.py` runs against the running stack through the Vite proxy:
the library → creating and changing an agent → the Document Assistant over an uploaded
handbook (answer, citation, steps, progress stream, history) → an action approved after a
stale decision is refused → pause and resume → cancel → another user is refused. The exit code
is non-zero if any check failed.

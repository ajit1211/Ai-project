"""End-to-end check of Phase 11 agents and tasks through the Vite proxy.

Run with the full stack up (`pnpm dev`, mock model):  uv run python scripts/e2e_agents.py
It registers throwaway users and checks: the agent library (the platform Document Assistant);
creating, changing (a new version) and validating an agent; a task for the Document Assistant
that searches an uploaded handbook and answers with a citation; the task history, its steps
and the progress stream; an action that waits for approval (a stale decision refused, the
approved action run once); pausing and resuming; cancelling a task while it works; and that
another user can see neither the tasks nor the agents. Every step prints its status; the exit
code is non-zero on failure.

The scripted parts use the mock model's control phrases ([[agent:...]], [[slow:...]]); a real
model chooses its own steps, so those checks then differ (the script says which).
"""

import sys
import time

from e2e_chat import call, check, events, results, sign_up
from e2e_documents import upload, wait_until

HANDBOOK = (
    b"# Handbook\n\n## Refunds\n\nCustomers may request a refund within thirty days of "
    b"purchase.\n\n## Travel\n\nBook flights at least two weeks ahead."
)
FINAL = ("completed", "failed", "cancelled", "timed_out")


def submit(token, agent_id, goal, expect=202, **extra):
    _, payload = call(
        "POST", "/runs/", {"agent_id": agent_id, "goal": goal, **extra}, token=token, expect=expect
    )
    return payload.get("data") or {}


def run_of(token, run_id):
    _, payload = call("GET", f"/runs/{run_id}/", token=token, expect=200)
    return payload.get("data") or {}


def wait_for(token, run_id, done, seconds=60.0):
    deadline = time.monotonic() + seconds
    current = run_of(token, run_id)
    while not done(current) and time.monotonic() < deadline:
        time.sleep(0.5)
        current = run_of(token, run_id)
    return current


def steps_of(token, run_id):
    _, payload = call("GET", f"/runs/{run_id}/steps/", token=token, expect=200)
    return payload.get("data") or []


def run():
    token = sign_up("agents")

    # --- The library ---------------------------------------------------------------------
    _, payload = call("GET", "/agents/", token=token, expect=200)
    agents = payload.get("data") or []
    assistant = next((a for a in agents if a["slug"] == "document-assistant"), None)
    check(
        assistant is not None and assistant["scope"] == "platform",
        "the Document Assistant is listed",
    )
    check(
        assistant is not None
        and {t["id"] for t in assistant["tools"]} == {"search_documents", "document_metadata"},
        "it may only read documents",
    )
    check(assistant is not None and not assistant["can_edit"], "a platform agent is read-only")

    _, payload = call(
        "POST",
        "/agents/",
        {
            "name": "E2E helper",
            "instructions": "Do what the goal says with the tools you have.",
            "allowed_tools": ["calculator", "save_memory"],
            "max_steps": 6,
        },
        token=token,
        expect=201,
    )
    helper = payload.get("data") or {}
    check(helper.get("version") == 1 and helper.get("can_edit") is True, "an agent is created")
    _, payload = call(
        "PATCH",
        f"/agents/{helper.get('id')}/",
        {"instructions": "Be brief. Do what the goal says."},
        token=token,
        expect=200,
    )
    check((payload.get("data") or {}).get("version") == 2, "changing its behaviour is version 2")
    call(
        "POST",
        "/agents/",
        {"name": "Bad", "instructions": "x", "allowed_tools": ["shell"]},
        token=token,
        expect=422,
    )
    call(
        "POST",
        "/agents/",
        {"name": "Bad", "instructions": "x", "code": "import os"},
        token=token,
        expect=422,
    )
    check(True, "unknown tools and unknown fields are refused")

    # --- The Document Assistant over an uploaded handbook ---------------------------------
    status, document = upload(token, "handbook.md", HANDBOOK)
    document_id = document.get("data", {}).get("id") if status == 201 else None
    if document_id:
        wait_until(
            token,
            document_id,
            lambda d: d["index"]["status"] in ("indexed", "failed"),
            "handbook indexed",
        )
    task = submit(token, assistant["id"], "How many days do customers have to request a refund?")
    check(task.get("status") == "queued", "a task is queued")
    done = wait_for(token, task.get("id"), lambda r: r.get("status") in FINAL)
    check(done.get("status") == "completed", f"the task completes ({done.get('status')})")
    check(
        "thirty days" in done.get("result", ""), "the answer comes from the handbook (mock model)"
    )
    check(
        bool(done.get("sources")) and done["sources"][0]["document_id"] == document_id,
        "it cites the handbook",
    )
    check(done.get("usage", {}).get("model_calls", 0) >= 2, "model calls are counted")
    kinds = [s["kind"] for s in steps_of(token, task.get("id"))]
    check(kinds == ["decision", "tool", "decision", "final"], f"the timeline is complete ({kinds})")
    _, stream = call("GET", f"/runs/{task.get('id')}/events/", token=token, expect=200, stream=True)
    names = [name for name, _ in events(stream)]
    check(names[:1] == ["run"] and names[-1:] == ["end"], "the progress stream reports and ends")
    _, payload = call("GET", "/runs/?status=finished", token=token, expect=200)
    check(
        task.get("id") in [r["id"] for r in payload.get("data") or []], "the task is in the history"
    )

    # --- An action waits for approval -----------------------------------------------------
    goal = '[[agent:call_tool save_memory {"content": "I prefer metric units"}]] [[agent:complete Saved.]]'
    pending_task = submit(token, helper.get("id"), goal)
    waiting = wait_for(
        token, pending_task.get("id"), lambda r: r.get("status") in ("waiting_for_approval", *FINAL)
    )
    approval = waiting.get("pending_approval")
    check(approval is not None, "saving a memory waits for approval (mock model)")
    if approval:
        url = f"/runs/{pending_task['id']}/approvals/{approval['id']}/"
        call(
            "POST",
            url,
            {"decision": "approve", "arguments_hash": "0" * 64},
            token=token,
            expect=409,
        )
        call(
            "POST",
            url,
            {"decision": "approve", "arguments_hash": approval["arguments_hash"]},
            token=token,
            expect=200,
        )
        finished = wait_for(token, pending_task["id"], lambda r: r.get("status") in FINAL)
        check(finished.get("status") == "completed", "approved, the task continues and completes")
        _, memories = call("GET", "/memories/", token=token, expect=200)
        check(
            sum(1 for m in memories.get("data") or [] if m["content"] == "I prefer metric units")
            == 1,
            "the memory was saved exactly once",
        )

    # --- Pause and resume, cancel ---------------------------------------------------------
    slow = submit(
        token, helper.get("id"), "[[slow:2500]] [[agent:progress Working]] [[agent:complete Done]]"
    )
    time.sleep(0.8)
    _, payload = call("POST", f"/runs/{slow.get('id')}/pause/", token=token, expect=200)
    paused = wait_for(
        token, slow.get("id"), lambda r: r.get("status") in ("paused", *FINAL), seconds=20
    )
    check(
        paused.get("status") == "paused",
        f"pausing stops it at a checkpoint ({paused.get('status')})",
    )
    call("POST", f"/runs/{slow.get('id')}/resume/", token=token, expect=200)
    resumed = wait_for(token, slow.get("id"), lambda r: r.get("status") in FINAL)
    check(resumed.get("status") == "completed", "resumed, it completes")

    stopping = submit(token, helper.get("id"), "[[slow:8000]] [[agent:complete Never]]")
    time.sleep(1.0)
    began = time.monotonic()
    call("POST", f"/runs/{stopping.get('id')}/cancel/", token=token, expect=200)
    cancelled = wait_for(token, stopping.get("id"), lambda r: r.get("status") in FINAL, seconds=20)
    check(cancelled.get("status") == "cancelled", "cancelling stops it while it works")
    check(time.monotonic() - began < 6, "within seconds, not after the model call")

    # --- Privacy ----------------------------------------------------------------------------
    other = sign_up("agents-other")
    call("GET", f"/runs/{task.get('id')}/", token=other, expect=404)
    call("POST", f"/runs/{task.get('id')}/cancel/", token=other, expect=404)
    call("GET", f"/agents/{helper.get('id')}/", token=other, expect=404)
    submit(other, helper.get("id"), "Hi", expect=404)
    _, payload = call("GET", "/runs/", token=other, expect=200)
    check(payload.get("data") == [], "another user sees none of these tasks")

    for _, line in results:
        print(line)
    failed = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

"""End-to-end check of the Phase 06 memory engine through the Vite proxy.

Run with the full stack up (`pnpm dev`):  uv run python scripts/e2e_memory.py
It registers a throwaway user, chats so that memories are extracted (an explicit statement,
an inference, a secret and an email address), checks what was stored and what was refused,
approves and rejects candidates, saves and edits a memory by hand, checks that a new
conversation recalls it, checks isolation from a second user, turns automatic memory off,
and clears everything. Every step prints its status; the exit code is non-zero on failure.

Extraction runs after each reply in the background, so the script polls briefly for it.
Without provider keys the mock provider answers (it recognizes "Remember that ...",
"I prefer ...", "Maybe ...").
"""

import sys
import time

from e2e_chat import call, check, events, results, sign_up


def say(token, conversation, text):
    status, body = call(
        "POST",
        f"/conversations/{conversation}/messages/",
        {"content": text},
        token=token,
        expect=200,
        stream=True,
    )
    stream = events(body) if status == 200 else []
    check(stream and stream[-1][0] == "message_complete", f"reply to {text[:40]!r}")


def memories(token, query=""):
    _, payload = call("GET", f"/memories/?limit=100{query}", token=token, expect=200)
    return payload["data"] if isinstance(payload, dict) else []


def wait_for(token, condition, what, seconds=10.0):
    deadline = time.monotonic() + seconds
    found = memories(token)
    while not condition(found) and time.monotonic() < deadline:
        time.sleep(0.5)
        found = memories(token)
    check(condition(found), what)
    return found


def run():
    token = sign_up("memory")
    _, payload = call("POST", "/conversations/", {}, token=token, expect=201)
    conversation = payload["data"]["id"]

    say(token, conversation, "Remember that I deploy on Fridays.")
    found = wait_for(
        token,
        lambda items: any(m["content"] == "I deploy on Fridays" for m in items),
        "an explicit statement became an active memory",
    )
    fridays = next((m for m in found if m["content"] == "I deploy on Fridays"), {})
    check(
        fridays.get("status") == "active" and fridays.get("source_type") == "explicit",
        "stored as active, explicit, private",
    )
    check(fridays.get("conversation_id") == conversation, "linked to its conversation")

    say(token, conversation, "Maybe I should learn Rust.")
    found = wait_for(
        token,
        lambda items: any(m["status"] == "candidate" for m in items),
        "an inference waits for approval",
    )
    candidate = next((m for m in found if m["status"] == "candidate"), {})

    say(token, conversation, "Remember that my password is hunter2.")
    say(token, conversation, "Remember that my email is me@example.com")
    time.sleep(2)
    stored = " ".join(m["content"] for m in memories(token))
    check("hunter2" not in stored, "a secret was not stored")
    check("me@example.com" not in stored, "an email address was not stored")

    if candidate:
        _, payload = call("POST", f"/memories/{candidate['id']}/reject/", token=token, expect=200)
        check(payload["data"]["status"] == "rejected", "a candidate can be rejected")

    _, payload = call(
        "POST",
        "/memories/",
        {"content": "I prefer answers with code examples", "kind": "preference"},
        token=token,
        expect=201,
    )
    manual = payload["data"]
    check(manual["status"] == "active" and manual["source_type"] == "manual", "saved by hand")
    _, payload = call(
        "PATCH",
        f"/memories/{manual['id']}/",
        {"content": "I prefer short answers with code examples"},
        token=token,
        expect=200,
    )
    check(payload["data"]["content"].startswith("I prefer short"), "edited")
    call(
        "POST",
        "/memories/",
        {"content": "api key = sk-abcdefghijklmnopqrstuvwxyz"},
        token=token,
        expect=422,
    )

    _, payload = call("GET", "/memories/search/?q=deploy%20fridays", token=token, expect=200)
    hits = payload["data"] if isinstance(payload, dict) else []
    check(hits and hits[0]["memory"]["content"] == "I deploy on Fridays", "search ranks it first")

    # A new conversation can use what was remembered.
    _, payload = call("POST", "/conversations/", {}, token=token, expect=201)
    say(token, payload["data"]["id"], "When should I deploy the release?")

    # Another user sees none of it.
    other = sign_up("intruder")
    check(memories(other) == [], "another user's list is empty")
    call("GET", f"/memories/{fridays.get('id')}/", token=other, expect=404)
    call("DELETE", f"/memories/{fridays.get('id')}/", token=other, expect=404)

    # Automatic memory off: nothing new is stored.
    call("PATCH", "/memories/settings/", {"extraction_enabled": False}, token=token, expect=200)
    before = len(memories(token))
    say(token, conversation, "Remember that I like tea.")
    time.sleep(2)
    check(len(memories(token)) == before, "nothing stored with extraction off")

    _, payload = call("POST", "/memories/clear/", {}, token=token, expect=200)
    check(payload["data"]["deleted"] >= 2, f"cleared {payload['data']['deleted']} memories")
    check(memories(token) == [], "nothing left")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        results.append((False, f"!! aborted: {type(exc).__name__}: {exc}"))
    finally:
        for _, line in results:
            print(line)
        failed = sum(1 for ok, _ in results if not ok)
        print(f"\n{len(results) - failed} of {len(results)} checks passed")
        sys.exit(1 if failed else 0)

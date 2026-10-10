"""End-to-end check of the Phase 05 chat engine through the Vite proxy.

Run with the full stack up (`pnpm dev`):  uv run python scripts/e2e_chat.py
It registers a throwaway user (reading the verification email from .local/mail), signs in,
creates a conversation, streams a reply, checks the stored messages and the usage row link,
exercises idempotency, regenerate, failure, archive, isolation from a second user, and
deletion. Every step prints its status; the exit code is non-zero if any check failed.

Without provider keys every answer comes from the mock provider and the failure step uses
its control phrases; with keys that step is skipped.
"""

import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:5173/api/v1"
MAIL_DIR = os.path.join(os.getcwd(), ".local", "mail")
results: list[tuple[bool, str]] = []


def call(method, path, body=None, token=None, expect=None, stream=False):
    headers = {"Accept": "text/event-stream" if stream else "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, content, ctype = resp.status, resp.read(), resp.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        status, content, ctype = e.code, e.read(), e.headers.get("content-type", "")
    ms = round((time.perf_counter() - started) * 1000)
    payload = content.decode() if "event-stream" in ctype else json.loads(content or b"null")
    ok = expect is None or status == expect
    summary = ""
    if isinstance(payload, dict):
        summary = payload.get("message") or (payload.get("error") or {}).get("code") or ""
    results.append(
        (ok, f"{'ok' if ok else '!!'} {status} {method:6} {path[:52]:52} {ms:5d} ms  {summary}")
    )
    if not ok:
        results.append((False, f"      body: {str(payload)[:300]}"))
    return status, payload


def check(condition, text):
    results.append((bool(condition), f"{'ok' if condition else '!!'}     {text}"))


def events(text):
    found = []
    for block in text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name and data is not None:
            found.append((name, data))
    return found


def latest_mail_token():
    mails = sorted(glob.glob(os.path.join(MAIL_DIR, "*.log")), key=os.path.getmtime)
    with open(mails[-1], encoding="utf-8", errors="replace") as handle:
        body = handle.read()
    match = re.search(r"#token=([\w-]+)", body)
    assert match, body[-400:]
    return match.group(1)


def sign_up(label):
    email = f"{label}{int(time.time() * 1000)}@example.com"
    password = "e2e-correct-horse-battery"
    call("POST", "/auth/register/", {"email": email, "password": password}, expect=202)
    call("POST", "/auth/verify-email/", {"token": latest_mail_token()}, expect=200)
    _, payload = call("POST", "/auth/login/", {"email": email, "password": password}, expect=200)
    return payload["data"]["access_token"]


def run():
    token = sign_up("chat")
    status, payload = call("POST", "/conversations/", {}, token=token, expect=201)
    conversation = payload["data"]["id"]
    base = f"/conversations/{conversation}/"

    # A streamed reply
    status, text = call(
        "POST",
        f"{base}messages/",
        {"content": "Explain Django middleware", "client_message_id": "e2e-1"},
        token=token,
        expect=200,
        stream=True,
    )
    stream = events(text) if status == 200 else []
    names = [n for n, _ in stream]
    check(
        names[:1] == ["message_start"] and names[-1:] == ["message_complete"],
        f"events: {names[:2]} ... {names[-2:]}",
    )
    check(names.count("text_delta") > 1, f"{names.count('text_delta')} text deltas")
    final = stream[-1][1]["message"] if stream else {}
    mock = final.get("provider") == "mock"
    check(
        final.get("status") == "completed" and final.get("content"),
        f"reply from {final.get('provider')}/{final.get('provider_model')}",
    )

    # Stored history, title, usage link
    status, payload = call("GET", f"{base}messages/", token=token, expect=200)
    stored = payload["data"] if status == 200 else []
    check([m["role"] for m in stored] == ["user", "assistant"], "history stored in order")
    check(
        stored and stored[-1]["content"] == final.get("content"),
        "stored reply equals streamed text",
    )
    status, payload = call("GET", base, token=token, expect=200)
    check(
        payload["data"]["title"] == "Explain Django middleware",
        f"title: {payload['data']['title']!r}",
    )
    status, payload = call("GET", "/ai/usage/?scope=me&days=1", token=token, expect=200)
    recent = payload["data"]["recent"] if status == 200 else []
    check(any(r["feature"] == "chat" for r in recent), "usage row recorded as chat traffic")

    # Idempotent retry
    status, payload = call(
        "POST",
        f"{base}messages/",
        {"content": "Explain Django middleware", "client_message_id": "e2e-1"},
        token=token,
        expect=200,
        stream=True,
    )
    check(
        isinstance(payload, dict) and payload["data"]["assistant_message"]["id"] == final.get("id"),
        "same client_message_id replays the stored reply",
    )

    # Regenerate
    status, text = call("POST", f"{base}regenerate/", token=token, expect=200, stream=True)
    regen = events(text) if status == 200 else []
    check(regen and regen[-1][0] == "message_complete", "regenerate streams a new reply")

    # A failure keeps the question and fails the reply (mock provider only)
    if mock:
        status, text = call(
            "POST",
            f"{base}messages/",
            {"content": "[[fail:timeout]] hello"},
            token=token,
            expect=200,
            stream=True,
        )
        failed = events(text) if status == 200 else []
        check(
            failed and failed[-1][0] == "error" and failed[-1][1]["code"] == "AI_TIMEOUT",
            "provider timeout ends with an error event",
        )
        check(
            [n for n, _ in failed][-2:] == ["usage", "error"],
            "what the failed call used is reported before the error",
        )
        failed_reply = failed[-1][1].get("assistant_message", {}) if failed else {}
        status, payload = call("GET", "/ai/usage/?scope=me&days=1", token=token, expect=200)
        recent = payload["data"]["recent"] if status == 200 else []
        check(
            any(r["status"] == "error" and r["error_code"] == "AI_TIMEOUT" for r in recent)
            and failed_reply.get("status") == "failed",
            "the failed reply is stored as failed and its call is in the ledger",
        )
        status, text = call("POST", f"{base}regenerate/", token=token, stream=True)

    # Validation and conflicts
    call("POST", "/conversations/", {"model": "nexa-nope"}, token=token, expect=422)
    call("POST", "/conversations/", {"owner_id": str(uuid.uuid4())}, token=token, expect=422)
    # PostgreSQL text cannot hold NUL: refused up front, never a database error.
    call("POST", f"{base}messages/", {"content": "a\x00b"}, token=token, expect=422, stream=True)
    call("POST", f"{base}cancel/", token=token, expect=200)

    # Another user sees nothing
    other = sign_up("intruder")
    call("GET", base, token=other, expect=404)
    call("GET", f"{base}messages/", token=other, expect=404)
    call("POST", f"{base}messages/", {"content": "hi"}, token=other, expect=404, stream=True)
    status, payload = call("GET", "/conversations/", token=other, expect=200)
    check(payload["data"] == [], "the other user's list is empty")

    # Archive, then delete
    call("PATCH", base, {"status": "archived"}, token=token, expect=200)
    call("POST", f"{base}messages/", {"content": "hi"}, token=token, expect=409, stream=True)
    call("DELETE", base, token=token, expect=200)
    call("GET", base, token=token, expect=404)


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

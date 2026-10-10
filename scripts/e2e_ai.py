"""End-to-end check of the Phase 04 AI Gateway through the Vite proxy.

Run with the full stack up (`pnpm dev`):  uv run python scripts/e2e_ai.py
It registers a throwaway user (reading the verification email from .local/mail), signs in,
lists models, asks for a completion, a structured answer, a streamed answer, exercises the
normalized error paths with the mock provider's control phrases, and reads the usage
summary. Every step prints its status; the exit code is non-zero if any check failed.

Without provider keys every answer comes from the mock provider; with keys, the first
target of each model answers and the control-phrase steps are skipped.
"""

import glob
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:5173/api/v1"
MAIL_DIR = os.path.join(os.getcwd(), ".local", "mail")

jar = http.cookiejar.CookieJar(
    http.cookiejar.DefaultCookiePolicy(secure_protocols=("https", "wss", "http"))
)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
token: str | None = None
results: list[tuple[bool, str]] = []


def call(method, path, body=None, auth=True, expect=None, raw=False):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if auth and token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    started = time.perf_counter()
    try:
        with opener.open(req, timeout=120) as resp:
            status, content, ctype = resp.status, resp.read(), resp.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        status, content, ctype = e.code, e.read(), e.headers.get("content-type", "")
    ms = round((time.perf_counter() - started) * 1000)
    payload = content.decode() if raw and "json" not in ctype else json.loads(content or b"null")
    ok = expect is None or status == expect
    summary = ""
    if isinstance(payload, dict):
        summary = payload.get("message") or (payload.get("error") or {}).get("code") or ""
    results.append(
        (ok, f"{'ok' if ok else '!!'} {status} {method:6} {path:30} {ms:5d} ms  {summary}")
    )
    if not ok:
        results.append((False, f"      body: {str(payload)[:300]}"))
    return status, payload


def check(condition, text):
    results.append((bool(condition), f"{'ok' if condition else '!!'}     {text}"))


def latest_mail_token():
    mails = sorted(glob.glob(os.path.join(MAIL_DIR, "*.log")), key=os.path.getmtime)
    assert mails, "no email files were written"
    with open(mails[-1], encoding="utf-8", errors="replace") as handle:
        body = handle.read()
    match = re.search(r"#token=([\w-]+)", body)
    assert match, body[-400:]
    return match.group(1)


def sse_events(text):
    events = []
    for block in text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name and data is not None:
            events.append((name, data))
    return events


def prompt(text, **extra):
    return {"messages": [{"role": "user", "content": text}], "feature": "playground", **extra}


def run():
    global token
    stamp = int(time.time())
    email = f"ai{stamp}@example.com"
    password = "e2e-correct-horse-battery"
    call("POST", "/auth/register/", {"email": email, "password": password}, auth=False, expect=202)
    call("POST", "/auth/verify-email/", {"token": latest_mail_token()}, auth=False, expect=200)
    status, payload = call(
        "POST", "/auth/login/", {"email": email, "password": password}, auth=False, expect=200
    )
    if status != 200:
        return
    token = payload["data"]["access_token"]

    # Models
    status, payload = call("GET", "/ai/models/", expect=200)
    models = {m["name"]: m for m in payload["data"]} if status == 200 else {}
    check(
        "nexa-balanced" in models and models["nexa-balanced"]["is_default"],
        f"models: {sorted(models)}",
    )
    check("nexa-reasoning" not in models, "nexa-reasoning needs a grant and is not listed")

    # Chat
    status, payload = call("POST", "/ai/chat/", prompt("Explain Django middleware"), expect=200)
    mock = False
    if status == 200:
        data = payload["data"]
        mock = data["provider"] == "mock"
        check(data["content"], f"answer from {data['provider']}/{data['provider_model']}")
        usage = data["usage"]
        check(
            usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"],
            f"usage {usage}",
        )
        check(data["latency_ms"] >= 0 and data["attempts"] >= 1, f"latency {data['latency_ms']} ms")

    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "skills"],
        "additionalProperties": False,
    }
    status, payload = call(
        "POST",
        "/ai/chat/",
        prompt(
            "Describe a developer named Ajit with the skills Python, Django and FastAPI.",
            response_format={"type": "json_schema", "name": "person", "schema": schema},
        ),
        expect=200,
    )
    if status == 200:
        check(
            isinstance(payload["data"]["parsed"], dict), f"structured: {payload['data']['parsed']}"
        )

    # Policy and validation errors
    call("POST", "/ai/chat/", prompt("hi", model="nexa-reasoning"), expect=422)
    call("POST", "/ai/chat/", prompt("hi", model="nexa-nope"), expect=422)
    call("POST", "/ai/chat/", {"messages": []}, expect=422)
    call("POST", "/ai/chat/", prompt("hi") | {"organization_id": "x"}, expect=422)
    call("POST", "/ai/chat/", prompt("hi"), auth=False, expect=401)
    unsafe = {"type": "object", "properties": {"a": {"$ref": "http://169.254.169.254/"}}}
    call(
        "POST",
        "/ai/chat/",
        prompt("hi", response_format={"type": "json_schema", "schema": unsafe}),
        expect=422,
    )

    # Provider error paths (mock only)
    if mock:
        call("POST", "/ai/chat/", prompt("[[fail:content]] hi"), expect=422)
        call("POST", "/ai/chat/", prompt("[[fail:timeout]] hi"), expect=504)
        call("POST", "/ai/chat/", prompt("[[fail:auth]] hi"), expect=502)

    # Streaming
    status, text = call("POST", "/ai/chat/stream/", prompt("Explain SSE"), expect=200, raw=True)
    if status == 200:
        events = sse_events(text)
        names = [n for n, _ in events]
        check(
            names[:1] == ["message.start"] and names[-2:] == ["usage", "message.end"],
            f"stream events: {names[:3]} ... {names[-2:]}",
        )
        streamed = "".join(d["text"] for n, d in events if n == "message.delta")
        check(streamed, f"streamed {len(streamed)} characters")
    if mock:
        status, text = call(
            "POST", "/ai/chat/stream/", prompt("[[fail:stream_after:1]] hi"), expect=200, raw=True
        )
        if status == 200:
            names = [n for n, _ in sse_events(text)]
            check(names[-1] == "error", f"mid-stream failure ends with an error event: {names}")
    call("POST", "/ai/chat/stream/", prompt("hi", model="nexa-nope"), expect=422)

    # Usage, served by Core
    status, payload = call("GET", "/ai/usage/?scope=me&days=1", expect=200)
    if status == 200:
        totals = payload["data"]["totals"]
        check(totals["requests"] >= 3, f"usage totals: {totals}")
    call("GET", "/ai/usage/?scope=organization", expect=200)  # owner of the personal workspace

    call("POST", "/auth/logout/", expect=200)


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

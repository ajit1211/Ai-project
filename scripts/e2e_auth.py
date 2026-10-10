"""End-to-end check of the Phase 03 authentication flow through the Vite proxy.

Run with the full stack up (`pnpm dev`):  uv run python scripts/e2e_auth.py
It registers a throwaway user, reads the verification email from .local/mail, signs in,
refreshes, creates an organization, invites someone, asks Runtime who the caller is, and
signs out. Every step prints its status; the exit code is non-zero if any check failed.
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
import uuid

BASE = "http://localhost:5173/api/v1"
MAIL_DIR = os.path.join(os.getcwd(), ".local", "mail")


# Browsers treat http://localhost as a secure context and send Secure cookies to it;
# Python's cookie jar does not unless told so.
jar = http.cookiejar.CookieJar(
    http.cookiejar.DefaultCookiePolicy(secure_protocols=("https", "wss", "http"))
)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
token: str | None = None
results: list[tuple[bool, str]] = []


def call(method, path, body=None, auth=True, csrf=False, expect=None):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if auth and token:
        headers["Authorization"] = f"Bearer {token}"
    if csrf:
        headers["X-NEXA-CSRF"] = "1"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    started = time.perf_counter()
    try:
        with opener.open(req, timeout=30) as resp:
            status, payload = resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"null")
    ms = round((time.perf_counter() - started) * 1000)
    ok = expect is None or status == expect
    summary = payload.get("message") or (payload.get("error") or {}).get("code") or ""
    results.append(
        (ok, f"{'ok' if ok else '!!'} {status} {method:6} {path:46} {ms:5d} ms  {summary}")
    )
    if not ok:
        results.append((False, f"      body: {json.dumps(payload)[:300]}"))
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


def run():
    global token
    stamp = int(time.time())
    email = f"e2e{stamp}@example.com"
    password = "e2e-correct-horse-battery"

    call(
        "POST",
        "/auth/register/",
        {"email": email, "password": password, "full_name": "E2E Tester"},
        auth=False,
        expect=202,
    )
    call(
        "POST", "/auth/login/", {"email": email, "password": password}, auth=False, expect=403
    )  # unverified
    verification = latest_mail_token()
    call("POST", "/auth/verify-email/", {"token": verification}, auth=False, expect=200)
    call(
        "POST", "/auth/verify-email/", {"token": verification}, auth=False, expect=400
    )  # single use

    status, payload = call(
        "POST", "/auth/login/", {"email": email, "password": password}, auth=False, expect=200
    )
    if status != 200:
        return
    token = payload["data"]["access_token"]
    check([c.name for c in jar] == ["nexa_refresh"], f"refresh cookie set: {[c.name for c in jar]}")
    check(
        "refresh_token" not in payload["data"], "refresh token kept out of the body for web clients"
    )

    call("GET", "/users/me/", expect=200)
    call("GET", "/organizations/", expect=200)
    call("POST", "/auth/refresh/", auth=False, expect=403)  # cookie without the CSRF header
    status, payload = call("POST", "/auth/refresh/", auth=False, csrf=True, expect=200)
    if status == 200:
        check(payload["data"]["access_token"] != token, "refresh rotated the access token")
        token = payload["data"]["access_token"]

    status, payload = call("POST", "/organizations/", {"name": "E2E Labs"}, expect=201)
    org = payload["data"]["id"]
    status, payload = call(
        "POST", "/auth/switch-organization/", {"organization_id": org}, expect=200
    )
    token = payload["data"]["access_token"]
    status, payload = call("GET", f"/organizations/{org}/roles/", expect=200)
    member_role = next(r["id"] for r in payload["data"] if r["key"] == "member")
    call(
        "POST",
        f"/organizations/{org}/invitations/",
        {"email": f"friend{stamp}@example.com", "role_id": member_role},
        expect=201,
    )
    call("POST", f"/organizations/{org}/teams/", {"name": "Research"}, expect=201)
    call("GET", f"/organizations/{org}/audit-logs/?limit=5", expect=200)
    call("GET", "/runtime/whoami", expect=200)  # FastAPI, through the same proxy
    call(
        "GET", f"/organizations/{uuid.uuid4()}/", expect=404
    )  # unknown org looks like a foreign one
    call("GET", "/sessions/", expect=200)
    call("POST", "/auth/logout/", expect=200)
    call("GET", "/users/me/", expect=401)  # revoked at once


try:
    run()
finally:
    print("\n".join(line for _, line in results))
    failed = [line for ok, line in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    sys.exit(1 if failed else 0)

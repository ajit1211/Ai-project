"""End-to-end check of the Phase 07 document pipeline through the Vite proxy.

Run with the full stack up (`pnpm dev`):  uv run python scripts/e2e_documents.py
It registers a throwaway user, uploads a text file, a Markdown file, a CSV, a Word document
and a PDF (multipart), waits for each to be processed and indexed, reads the extracted text,
the plain-text preview and the original bytes back, checks the refusals (a PDF that is not a
PDF, an unsupported extension, an empty file), checks that a second user in another
organization sees nothing, retries an image-only PDF, and deletes a document. Every step
prints its status; the exit code is non-zero on failure.

Without Redis the Celery broker is unreachable and Core processes the upload itself right
after the request commits (the documented fallback), so this works on a bare machine too.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nexa_core.settings")
import django

django.setup()

from e2e_chat import BASE, call, check, results, sign_up  # noqa: E402

from nexa_core.testing import make_docx, make_pdf  # noqa: E402


def upload(token, name, data, expect=201, **fields):
    """A multipart upload; the content type sent for the file is deliberately misleading."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        BASE + "/documents/",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, payload = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"null")
    ok = status == expect
    code = (payload or {}).get("error", {}).get("code", "") if isinstance(payload, dict) else ""
    results.append((ok, f"{'ok' if ok else '!!'} {status} UPLOAD {name:44} {code}"))
    return status, payload


def raw(token, path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        # Header names are case-insensitive (the Vite proxy lowercases them).
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()


def wait_until(token, document_id, done, what, seconds=60.0):
    deadline = time.monotonic() + seconds
    document = {}
    while time.monotonic() < deadline:
        _, payload = call("GET", f"/documents/{document_id}/", token=token, expect=200)
        document = payload["data"]
        if done(document):
            break
        time.sleep(1)
    check(done(document), what)
    return document


def processed_and_indexed(document):
    return document["status"] not in ("uploaded", "queued", "processing") and document["index"][
        "status"
    ] not in ("pending", "indexing")


def run():
    token = sign_up("documents")
    files = {
        "notes.txt": b"Plain notes.\nSecond line mentions refunds.",
        "guide.md": b"# Guide\n\n## Refunds\n\nRefunds take five days.\n\n<script>alert(1)</script>",
        "prices.csv": b"Plan,Price\nStarter,19\nTeam,=1+1\n",
        "notes.docx": make_docx(),
        "two-pages.pdf": make_pdf(["First page about refunds.", "Second page about travel."]),
    }
    ids = {}
    for name, data in files.items():
        status, payload = upload(token, name, data, title=f"E2E {name}")
        if status == 201:
            ids[name] = payload["data"]["id"]
            check(
                payload["data"]["status"] in ("queued", "processing", "completed"),
                f"{name} accepted",
            )
            check(
                "storage_key" not in json.dumps(payload), f"{name}: no storage path in the response"
            )

    for name, document_id in ids.items():
        document = wait_until(
            token, document_id, processed_and_indexed, f"{name} processed and indexed"
        )
        check(document["status"] == "completed", f"{name} completed ({document['status']})")
        check(document["index"]["status"] == "indexed", f"{name} search index built")
        check(document["index"]["chunk_count"] >= 1, f"{name} has passages")

    pdf = ids.get("two-pages.pdf")
    if pdf:
        _, payload = call("GET", f"/documents/{pdf}/content/?page=2", token=token, expect=200)
        check(
            payload["data"]["content"] == "Second page about travel.",
            "PDF page 2 read back by page",
        )
        status, headers, body = raw(token, f"/documents/{pdf}/download/")
        check(body == files["two-pages.pdf"], "download returns the original bytes")
        check(
            headers.get("content-disposition", "").startswith("attachment"),
            "download is an attachment",
        )
    guide = ids.get("guide.md")
    if guide:
        status, headers, body = raw(token, f"/documents/{guide}/preview/")
        check(headers.get("content-type", "").startswith("text/plain"), "preview is plain text")
        check(headers.get("x-content-type-options") == "nosniff", "preview is not sniffed")
        check(b"<script>alert(1)</script>" in body, "markup stays literal text")
    csv_id = ids.get("prices.csv")
    if csv_id:
        _, payload = call("GET", f"/documents/{csv_id}/", token=token, expect=200)
        check("formula_cells_neutralized" in payload["data"]["warnings"], "CSV formula neutralized")

    upload(token, "fake.pdf", b"not really a pdf", expect=415)
    upload(token, "tool.exe", b"MZ....", expect=415)
    upload(token, "empty.txt", b"", expect=422)

    _, payload = call("GET", "/documents/?searchable=true&limit=100", token=token, expect=200)
    check(len(payload["data"]) == len(ids), "searchable filter lists the indexed documents")
    check(payload["meta"]["limits"]["max_upload_bytes"] > 0, "upload limits are advertised")

    status, payload = upload(token, "scan.pdf", make_pdf([None, None]))
    if status == 201:
        scan = payload["data"]["id"]
        document = wait_until(token, scan, processed_and_indexed, "image-only PDF processed")
        check(document["status"] == "needs_ocr", "image-only PDF needs OCR (no fake text)")
        call("POST", f"/documents/{scan}/retry/", token=token, expect=202)
        document = wait_until(token, scan, processed_and_indexed, "retried")
        check(document["attempt_count"] == 2, "a second attempt is recorded")

    other = sign_up("documents-other")
    if pdf:
        call("GET", f"/documents/{pdf}/", token=other, expect=404)
        call("GET", f"/documents/{pdf}/download/", token=other, expect=404)
        _, payload = call("GET", "/documents/", token=other, expect=200)
        check(payload["data"] == [], "another organization sees no documents")

    notes = ids.get("notes.txt")
    if notes:
        call("DELETE", f"/documents/{notes}/", token=token, expect=200)
        call("GET", f"/documents/{notes}/", token=token, expect=404)

    for _, line in results:
        print(line)
    failed = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

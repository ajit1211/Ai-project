"""End-to-end check of Phase 08 document-grounded answers through the Vite proxy.

Run with the full stack up (`pnpm dev`):  uv run python scripts/e2e_rag.py
It registers a throwaway user, uploads a handbook, waits for it to be indexed, asks a
grounded question (streamed), and checks the `sources` event, the citation, the stored
grounding record, the sources endpoint, the no-evidence answer, a fabricated citation, that
ordinary chat is unchanged, that a second user in another organization cannot ground on the
document, and that deleting the document leaves the old citation without its text. Every
step prints its status; the exit code is non-zero on failure.

Without provider keys the mock provider answers: it quotes the best-matching sentence and
cites it, and its control phrases ([[cite:S99]]) misbehave on purpose.
"""

import sys
import time

from e2e_chat import call, check, events, results, sign_up
from e2e_documents import upload, wait_until

HANDBOOK = (
    b"# Handbook\n\n## Refunds\n\nCustomers may request a refund within thirty days of "
    b"purchase. Enterprise refunds need written approval from finance.\n\n## Travel\n\n"
    b"Book flights at least two weeks ahead."
)


def ask(token, conversation, content, grounding=None, expect=200):
    body = {"content": content}
    if grounding is not None:
        body["grounding"] = grounding
    status, payload = call(
        "POST",
        f"/conversations/{conversation}/messages/",
        body,
        token=token,
        expect=expect,
        stream=True,
    )
    return status, events(payload) if status == 200 else payload


def run():
    token = sign_up("rag")
    status, payload = upload(token, "handbook.md", HANDBOOK)
    if status != 201:
        return 1
    document_id = payload["data"]["id"]
    wait_until(
        token,
        document_id,
        lambda d: d["index"]["status"] in ("indexed", "failed"),
        "handbook indexed",
    )
    scope = {"document_ids": [document_id]}
    _, payload = call("POST", "/conversations/", {}, token=token, expect=201)
    conversation = payload["data"]["id"]

    started = time.perf_counter()
    status, stream = ask(
        token, conversation, "How long do customers have to request a refund?", scope
    )
    names = [name for name, _ in stream] if status == 200 else []
    check(names[:2] == ["message_start", "sources"], "the stream announces its sources first")
    check(names and names[-1] == "message_complete", "the grounded reply completes")
    if names and names[-1] == "message_complete":
        message = stream[-1][1]["message"]
        check("[S1]" in message["content"], "the answer cites S1")
        check(message["grounding"]["outcome"] == "answered", "outcome: answered")
        cited = [s for s in message["sources"] if s["cited"]]
        check(cited and cited[0]["document_id"] == document_id, "the cited source is the handbook")
        check(cited and cited[0]["section"] == "Handbook > Refunds", "with its section")
        print(f"   grounded reply in {round((time.perf_counter() - started) * 1000)} ms")
        _, detail = call(
            "GET",
            f"/conversations/{conversation}/messages/{message['id']}/sources/",
            token=token,
            expect=200,
        )
        check(detail["data"] and "thirty days" in detail["data"][0]["excerpt"], "sources endpoint")

    status, stream = ask(token, conversation, "What colour is the office cat?", scope)
    if status == 200:
        message = stream[-1][1]["message"]
        check(
            message["grounding"]["outcome"] == "no_evidence", "no evidence: the model is not asked"
        )

    status, stream = ask(
        token, conversation, "How long do customers have to request a refund? [[cite:S99]]", scope
    )
    if status == 200:
        grounding = stream[-1][1]["message"]["grounding"]
        check(grounding["invalid_citations"] == ["S99"], "a made-up citation is caught")

    status, stream = ask(token, conversation, "Hello, ordinary chat")
    if status == 200:
        message = stream[-1][1]["message"]
        check(message["grounding"] is None and message["sources"] == [], "ordinary chat unchanged")

    other = sign_up("rag-other")
    _, payload = call("POST", "/conversations/", {}, token=other, expect=201)
    status, payload = ask(other, payload["data"]["id"], "refund?", scope, expect=404)
    check(
        isinstance(payload, dict) and payload["error"]["code"] == "DOCUMENT_UNAVAILABLE",
        "another organization cannot ground on the document",
    )

    call("DELETE", f"/documents/{document_id}/", token=token, expect=200)
    _, payload = call("GET", f"/conversations/{conversation}/messages/", token=token, expect=200)
    grounded = [m for m in payload["data"] if m["grounding"] and m["grounding"]["supplied"]]
    check(
        grounded
        and all(not s["available"] and s["excerpt"] is None for s in grounded[0]["sources"]),
        "a deleted document's passages are no longer shown",
    )

    for _, line in results:
        print(line)
    failed = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

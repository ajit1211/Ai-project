"""End-to-end check of Phase 09 retrieval modes through the Vite proxy.

Run with the full stack up (`pnpm dev`):  uv run python scripts/e2e_vector.py
It works on either kind of stack and says which one it found:

- **semantic search available** (pgvector and an embedding provider): a document becomes
  semantically ready, a misspelt question is answered in semantic and hybrid mode without a
  fallback, re-embedding works, and the admin search status counts the vectors;
- **not available** (e.g. the local Windows database without pgvector): every document says
  why, `auto` answers by keywords without complaint, an explicit hybrid request answers by
  keywords and records the fallback, and re-embedding is refused.

Both: the retrieval options endpoint, input validation, and that no response carries vectors.
Every step prints its status; the exit code is non-zero on failure.
"""

import json
import sys

from e2e_chat import call, check, results, sign_up
from e2e_documents import upload, wait_until
from e2e_rag import ask

POLICY = (
    b"# Security policy\n\n## Passwords\n\nPasswords must be at least fourteen characters long "
    b"and are never shared.\n\n## Devices\n\nReport a lost laptop to the service desk within "
    b"one hour."
)
SETTLED = ("ready", "failed", "unavailable", "none")


def grounding_of(stream):
    names = [name for name, _ in stream] if isinstance(stream, list) else []
    if not names or names[-1] != "message_complete":
        return None
    return stream[-1][1]["message"]["grounding"]


def run():
    token = sign_up("vector")
    _, payload = call("GET", "/ai/retrieval/", token=token, expect=200)
    options = payload["data"]
    available = options["semantic_available"]
    check("lexical" in options["modes"], "keyword retrieval is always offered")
    check(
        available == ("semantic" in options["modes"] and "hybrid" in options["modes"]),
        "semantic modes are offered exactly when semantic search runs",
    )
    print(
        "   semantic search:",
        "available, model " + str(options["embedding_model"])
        if available
        else f"unavailable ({options['semantic_unavailable_reason']})",
    )

    status, payload = upload(token, "policy.md", POLICY)
    if status != 201:
        return 1
    document_id = payload["data"]["id"]
    document = wait_until(
        token,
        document_id,
        lambda d: (
            d["index"]["status"] in ("indexed", "failed")
            and d["index"]["semantic"]["status"] in SETTLED
        ),
        "policy indexed and its semantic state settled",
        seconds=90.0,
    )
    semantic = document["index"]["semantic"]
    shown = json.dumps(document)
    check(
        not any(key in shown for key in ('"vector"', '"embedding"', '"dimensions"', "similarity")),
        "the document shows no vectors, dimensions or similarities",
    )
    _, payload = call("POST", "/conversations/", {}, token=token, expect=201)
    conversation = payload["data"]["id"]
    scope = {"document_ids": [document_id]}

    call("POST", f"/documents/{document_id}/index/", {"reembed": "yes"}, token=token, expect=422)

    if available:
        check(
            semantic["status"] == "ready" and semantic["ready"],
            "the document is semantically ready",
        )
        for mode in ("semantic", "hybrid"):
            _, stream = ask(
                token, conversation, "pasword lenght rulez", {**scope, "retrieval_mode": mode}
            )
            grounding = grounding_of(stream)
            check(
                grounding and grounding["retrieval_mode"] == mode and grounding["fallback"] is None,
                f"{mode} retrieval ran without a fallback",
            )
            check(grounding and grounding["supplied"] > 0, f"{mode}: a misspelt question finds it")
        call("POST", f"/documents/{document_id}/index/", {"reembed": True}, token=token, expect=202)
        wait_until(
            token,
            document_id,
            lambda d: d["index"]["semantic"]["status"] in SETTLED,
            "re-embedded",
        )
        status, payload = call("GET", "/documents/search-status/", token=token, expect=200)
        if status == 200:
            counts = payload["data"]
            check(counts["passages"]["embedded"] >= 1, "search status counts the vectors")
    else:
        reason = options["semantic_unavailable_reason"]
        check(
            semantic["status"] == "unavailable" and semantic["reason"] == reason,
            f"the document says why: {semantic['reason']}",
        )
        _, stream = ask(token, conversation, "How long must passwords be?", scope)
        grounding = grounding_of(stream)
        check(
            grounding and (grounding["retrieval_mode"], grounding["fallback"]) == ("lexical", None),
            "auto answers by keywords without a fallback notice",
        )
        _, stream = ask(
            token,
            conversation,
            "How long must passwords be?",
            {**scope, "retrieval_mode": "hybrid"},
        )
        grounding = grounding_of(stream)
        check(
            grounding
            and grounding["retrieval_mode"] == "lexical"
            and grounding["fallback"] == f"semantic_{reason}"
            and grounding["outcome"] == "answered",
            "an explicit hybrid request answers by keywords and records why",
        )
        call("POST", f"/documents/{document_id}/index/", {"reembed": True}, token=token, expect=409)
        status, payload = call("GET", "/documents/search-status/", token=token, expect=200)
        if status == 200:
            check(
                payload["data"]["semantic"]["available"] is False,
                "search status reports semantic search off",
            )

    for _, line in results:
        print(line)
    failed = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

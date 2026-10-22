"""End-to-end check of Phase 10 tool calling through the Vite proxy.

Run with the full stack up (`pnpm dev`, mock model or real keys):  uv run python scripts/e2e_tools.py
It registers throwaway users and checks: the tools list; the calculator and the clock in chat
(streamed `tool_call` / `tool_result` events and an answer from the result); document details
and search inside the user's access; saving a memory, which pauses the reply until approved,
blocks new messages meanwhile, refuses a stale decision, runs once on approval and is declined
on rejection; switching a tool off for the organization; `tools: "none"`; and that another
user cannot read an invocation. Every step prints its status; the exit code is non-zero on
failure.

With the mock model the plain requests below trigger tools; a real model chooses for itself,
so a few checks may then differ (the script says which).
"""

import sys

from e2e_chat import call, check, events, results, sign_up
from e2e_documents import upload, wait_until

HANDBOOK = (
    b"# Handbook\n\n## Refunds\n\nCustomers may request a refund within thirty days of "
    b"purchase.\n\n## Travel\n\nBook flights at least two weeks ahead."
)


def ask(token, conversation, content, expect=200, **extra):
    status, payload = call(
        "POST",
        f"/conversations/{conversation}/messages/",
        {"content": content, **extra},
        token=token,
        expect=expect,
        stream=True,
    )
    return status, events(payload) if status == 200 else payload


def kinds(stream):
    return [name for name, _ in stream if name not in ("text_delta", "usage")]


def of(stream, kind):
    return [data for name, data in stream if name == kind]


def run():
    token = sign_up("tools")
    _, payload = call("GET", "/tools/", token=token, expect=200)
    listed = {tool["id"]: tool for tool in payload["data"]}
    check(
        {
            "calculator",
            "current_time",
            "document_metadata",
            "search_documents",
            "workspace_info",
            "save_memory",
        }
        <= set(listed),
        "the six built-in tools are listed",
    )
    check(listed.get("save_memory", {}).get("requires_approval") is True, "saving needs approval")

    _, payload = call("POST", "/conversations/", {}, token=token, expect=201)
    conversation = payload["data"]["id"]
    invocation_id = None
    status, stream = ask(token, conversation, "What is 12 * (3 + 4)?")
    if status == 200:
        check(kinds(stream)[-1] == "message_complete", "the calculator reply completes")
        ran = of(stream, "tool_result")
        check(
            ran and ran[0]["tool_id"] == "calculator" and ran[0]["result"]["result_text"] == "84",
            "the calculator ran and returned 84 (mock model)",
        )
        answer = of(stream, "message_complete")[0]["message"]
        check("84" in answer["content"], "the answer uses the result")
        invocation_id = answer["tools"][0]["invocation_id"] if answer["tools"] else None

    status, stream = ask(token, conversation, "What time is it?")
    if status == 200:
        check(
            [r["tool_id"] for r in of(stream, "tool_result")] == ["current_time"],
            "the clock ran (mock model)",
        )

    status, document = upload(token, "handbook.md", HANDBOOK)
    if status == 201:
        wait_until(
            token,
            document["data"]["id"],
            lambda d: d["index"]["status"] in ("indexed", "failed"),
            "handbook indexed",
        )
        status, stream = ask(token, conversation, "List my documents")
        if status == 200:
            found = of(stream, "tool_result")
            check(
                found
                and any(d["filename"] == "handbook.md" for d in found[0]["result"]["documents"]),
                "document details list the handbook",
            )
        status, stream = ask(token, conversation, "Search my documents for refund within days")
        if status == 200:
            found = of(stream, "tool_result")
            check(
                found
                and found[0]["tool_id"] == "search_documents"
                and found[0]["result"]["results"],
                "document search finds the refund passage",
            )

    # An action that changes data waits for the user.
    _, payload = call("POST", "/conversations/", {}, token=token, expect=201)
    paused_in = payload["data"]["id"]
    status, stream = ask(token, paused_in, "save to memory: I prefer metric units")
    approval = None
    if status == 200:
        check(kinds(stream)[-1] == "approval_required", "saving a memory pauses for approval")
        approval = (
            of(stream, "approval_required")[0]["approval"]
            if of(stream, "approval_required")
            else None
        )
    if approval:
        check(approval["details"]["content"] == "I prefer metric units", "the exact text is shown")
        _, blocked = ask(token, paused_in, "anything else?", expect=409)
        check(
            isinstance(blocked, dict) and blocked["error"]["code"] == "TOOL_APPROVAL_PENDING",
            "a new message waits for the decision",
        )
        url = f"/conversations/{paused_in}/approvals/{approval['id']}/"
        call(
            "POST",
            url,
            {"decision": "approve", "arguments_hash": "0" * 64},
            token=token,
            expect=409,
        )
        status, payload = call(
            "POST",
            url,
            {"decision": "approve", "arguments_hash": approval["arguments_hash"]},
            token=token,
            expect=200,
            stream=True,
        )
        if status == 200:
            stream = events(payload)
            check(
                [r["status"] for r in of(stream, "tool_result")] == ["succeeded"],
                "approving saves it",
            )
            check(kinds(stream)[-1] == "message_complete", "and the reply continues")
        _, memories = call("GET", "/memories/", token=token, expect=200)
        check(
            sum(1 for m in memories["data"] if m["content"] == "I prefer metric units") == 1,
            "exactly one memory was saved",
        )
        call(
            "POST",
            url,
            {"decision": "approve", "arguments_hash": approval["arguments_hash"]},
            token=token,
            expect=409,
        )

    status, stream = ask(token, paused_in, "save to memory: my desk is number 42")
    pending = of(stream, "approval_required") if status == 200 else []
    if pending:
        second = pending[0]["approval"]
        status, payload = call(
            "POST",
            f"/conversations/{paused_in}/approvals/{second['id']}/",
            {"decision": "reject", "arguments_hash": second["arguments_hash"]},
            token=token,
            expect=200,
            stream=True,
        )
        if status == 200:
            check(
                [r["error_code"] for r in of(events(payload), "tool_result")]
                == ["declined_by_user"],
                "rejecting declines it",
            )

    # Switched off for the organization (the registering user owns their workspace).
    call("PATCH", "/tools/calculator/", {"enabled": False}, token=token, expect=200)
    status, stream = ask(token, conversation, "What is 2 + 2?")
    if status == 200:
        check(
            "calculator" not in [c["tool_id"] for c in of(stream, "tool_call")],
            "a switched-off tool is not used",
        )
    call("PATCH", "/tools/calculator/", {"enabled": True}, token=token, expect=200)
    status, stream = ask(token, conversation, "What is 2 + 2?", tools="none")
    if status == 200:
        check(not of(stream, "tool_call"), "tools: none answers without tools")

    if invocation_id:
        call("GET", f"/tools/invocations/{invocation_id}/", token=token, expect=200)
        other = sign_up("tools-other")
        call("GET", f"/tools/invocations/{invocation_id}/", token=other, expect=404)

    for _, line in results:
        print(line)
    failed = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

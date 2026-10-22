"""Tool activity as people see it: on messages, in invocation records and in approvals.

Results that came from documents are re-authorized whenever they are read: if any document a
result was drawn from is no longer readable by the viewer (deleted, unshared), the result is
hidden and only the references that are still readable remain.
"""

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from nexa_common.access import Principal
from nexa_runtime.rag import tables as rt
from nexa_runtime.rag.retrieval import DocumentFilter, readable_documents
from nexa_runtime.tools import repository as repo
from nexa_runtime.tools.registry import ToolRegistry
from nexa_runtime.tools.schemas import ApprovalOut, InvocationOut, ToolActivityOut


def approval_out(row: Any, registry: ToolRegistry) -> ApprovalOut:
    definition = registry.get(row.tool_id)
    status = row.status
    if status == "pending" and row.expires_at <= repo.now():
        status = "expired"
    return ApprovalOut(
        id=row.id,
        invocation_id=row.invocation_id,
        tool_id=row.tool_id,
        tool_name=definition.name if definition else row.tool_id,
        summary=row.summary,
        details=row.details or {},
        risk=row.risk,
        status=status,
        arguments_hash=row.arguments_hash,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
    )


def activity_out(
    row: Any, approval: Any, registry: ToolRegistry, readable: set[UUID] | None = None
) -> ToolActivityOut:
    definition = registry.get(row.tool_id)
    sources = list(row.sources or [])
    result = row.result
    hidden = False
    if sources and readable is not None:
        kept = [s for s in sources if _uuid(s.get("document_id")) in readable]
        if len(kept) != len(sources):
            result, hidden = None, True
        sources = kept
    return ToolActivityOut(
        invocation_id=row.id,
        call_id=row.call_id,
        tool_id=row.tool_id,
        tool_name=definition.name if definition else row.tool_id,
        tool_version=row.tool_version,
        step=row.step,
        position=row.position,
        status=row.status,
        summary=definition.summary(row.arguments or {}) if definition else row.tool_id,
        risk=row.risk,
        duration_ms=row.duration_ms,
        error_code=row.error_code or None,
        error_message=row.error_message or None,
        result=result,
        result_truncated=row.result_truncated,
        result_hidden=hidden,
        sources=sources,
        approval=approval_out(approval, registry) if approval is not None else None,
    )


async def activity_for(
    conn: AsyncConnection, principal: Principal, registry: ToolRegistry, message_ids: list[UUID]
) -> dict[UUID, list[ToolActivityOut]]:
    rows = await repo.invocations_of(conn, message_ids)
    if not rows:
        return {}
    approvals = {a.invocation_id: a for a in await repo.approvals_of(conn, [r.id for r in rows])}
    readable = await readable_ids(conn, principal, _document_ids(rows))
    found: dict[UUID, list[ToolActivityOut]] = defaultdict(list)
    for row in rows:
        found[row.message_id].append(activity_out(row, approvals.get(row.id), registry, readable))
    return dict(found)


async def activity_for_run(
    conn: AsyncConnection, principal: Principal, registry: ToolRegistry, run_id: UUID
) -> list[ToolActivityOut]:
    """An agent run's tool calls, in order, with document results re-authorized now."""
    rows = await repo.invocations_of_run(conn, run_id)
    if not rows:
        return []
    approvals = {a.invocation_id: a for a in await repo.approvals_of(conn, [r.id for r in rows])}
    readable = await readable_ids(conn, principal, _document_ids(rows))
    return [activity_out(row, approvals.get(row.id), registry, readable) for row in rows]


async def invocation_detail(
    conn: AsyncConnection, principal: Principal, registry: ToolRegistry, row: Any
) -> InvocationOut:
    approvals = await repo.approvals_of(conn, [row.id])
    readable = await readable_ids(conn, principal, _document_ids([row]))
    activity = activity_out(row, approvals[0] if approvals else None, registry, readable)
    return InvocationOut(
        **activity.model_dump(),
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        run_id=row.run_id,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


async def readable_ids(conn: AsyncConnection, principal: Principal, ids: set[UUID]) -> set[UUID]:
    if not ids or principal.organization_id is None:
        return set()
    scope = DocumentFilter(
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        team_ids=principal.team_ids,
        manage_all=principal.has("documents.manage_all"),
        document_ids=tuple(sorted(ids)),
    )
    rows = await conn.execute(select(rt.documents.c.id).where(readable_documents(scope)))
    return {row.id for row in rows}


def _document_ids(rows: list[Any]) -> set[UUID]:
    found: set[UUID] = set()
    for row in rows:
        for source in row.sources or []:
            value = _uuid(source.get("document_id"))
            if value is not None:
                found.add(value)
    return found


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None

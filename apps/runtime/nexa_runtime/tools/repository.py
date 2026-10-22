"""Queries over the tool tables. Every write that decides who runs a call is a single
conditional statement (insert-if-absent, update-if-still-in-state), so two requests can never
both record or both run the same call."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Row, and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from nexa_common.ids import new_id
from nexa_runtime.tools import tables as t

InvocationRow = Row[Any]
ApprovalRow = Row[Any]

RUNNABLE = ("validated", "approved")
FINAL = ("succeeded", "rejected", "failed", "timed_out", "cancelled")


def now() -> datetime:
    return datetime.now(UTC)


def _first(result: Any) -> InvocationRow | None:
    return cast("Row[Any] | None", result.first())


async def record_invocation(conn: AsyncConnection, **values: Any) -> tuple[InvocationRow, bool]:
    """Insert the invocation unless one with its idempotency key exists. Returns the row and
    whether it was created now (False: the same call of the same turn was recorded before)."""
    values = {"id": new_id(), "created_at": now(), "attempts": 0, **values}
    created = (
        await conn.execute(
            pg_insert(t.tool_invocations)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(t.tool_invocations)
        )
    ).first()
    if created is not None:
        return created, True
    existing = (
        await conn.execute(
            select(t.tool_invocations).where(
                t.tool_invocations.c.idempotency_key == values["idempotency_key"]
            )
        )
    ).one()
    return existing, False


async def claim(conn: AsyncConnection, invocation_id: UUID) -> InvocationRow | None:
    """Move a runnable call to `executing`. None when it is not runnable any more: another
    request claimed it, it finished, or it was never allowed to run."""
    return _first(
        await conn.execute(
            update(t.tool_invocations)
            .where(
                t.tool_invocations.c.id == invocation_id,
                t.tool_invocations.c.status.in_(RUNNABLE),
            )
            .values(
                status="executing",
                started_at=now(),
                attempts=t.tool_invocations.c.attempts + 1,
            )
            .returning(t.tool_invocations)
        )
    )


async def finish(
    conn: AsyncConnection, invocation_id: UUID, *, from_states: tuple[str, ...], **values: Any
) -> InvocationRow | None:
    """Record how a call ended, only from one of `from_states` (no transition out of a final
    state, ever)."""
    return _first(
        await conn.execute(
            update(t.tool_invocations)
            .where(
                t.tool_invocations.c.id == invocation_id,
                t.tool_invocations.c.status.in_(from_states),
            )
            .values(completed_at=now(), **values)
            .returning(t.tool_invocations)
        )
    )


async def set_status(
    conn: AsyncConnection, invocation_id: UUID, status: str, *, from_states: tuple[str, ...]
) -> InvocationRow | None:
    return _first(
        await conn.execute(
            update(t.tool_invocations)
            .where(
                t.tool_invocations.c.id == invocation_id,
                t.tool_invocations.c.status.in_(from_states),
            )
            .values(status=status)
            .returning(t.tool_invocations)
        )
    )


async def cancel_open(conn: AsyncConnection, message_id: UUID) -> int:
    """Close the calls of a reply that ended (stopped, failed, left) before they ran: they
    are recorded as cancelled, never left looking as if they might still run. A call waiting
    for approval is closed with its approval instead (`close_pending_approvals`)."""
    result = await conn.execute(
        update(t.tool_invocations)
        .where(
            t.tool_invocations.c.message_id == message_id,
            t.tool_invocations.c.status.in_(("requested", "validated", "approved", "executing")),
        )
        .values(
            status="cancelled",
            error_code="cancelled",
            error_message="Stopped before the tool finished.",
            completed_at=now(),
        )
    )
    return result.rowcount


async def invocations_of(conn: AsyncConnection, message_ids: list[UUID]) -> list[InvocationRow]:
    if not message_ids:
        return []
    return list(
        (
            await conn.execute(
                select(t.tool_invocations)
                .where(t.tool_invocations.c.message_id.in_(message_ids))
                .order_by(
                    t.tool_invocations.c.message_id,
                    t.tool_invocations.c.step,
                    t.tool_invocations.c.position,
                )
            )
        ).all()
    )


async def invocation(
    conn: AsyncConnection, organization_id: UUID, invocation_id: UUID
) -> InvocationRow | None:
    return _first(
        await conn.execute(
            select(t.tool_invocations).where(
                t.tool_invocations.c.id == invocation_id,
                t.tool_invocations.c.organization_id == organization_id,
            )
        )
    )


async def count_for_message(conn: AsyncConnection, message_id: UUID) -> int:
    rows = await conn.execute(
        select(t.tool_invocations.c.id).where(t.tool_invocations.c.message_id == message_id)
    )
    return len(rows.all())


async def cancel_open_run(conn: AsyncConnection, run_id: UUID) -> int:
    """As `cancel_open`, for an agent run that ended."""
    result = await conn.execute(
        update(t.tool_invocations)
        .where(
            t.tool_invocations.c.run_id == run_id,
            t.tool_invocations.c.status.in_(("requested", "validated", "approved", "executing")),
        )
        .values(
            status="cancelled",
            error_code="cancelled",
            error_message="Stopped before the tool finished.",
            completed_at=now(),
        )
    )
    return result.rowcount


async def close_pending_approvals_of_run(
    conn: AsyncConnection, run_id: UUID, *, status: str
) -> int:
    """As `close_pending_approvals`, for an agent run."""
    ids = select(t.tool_invocations.c.id).where(t.tool_invocations.c.run_id == run_id)
    closed = await conn.execute(
        update(t.tool_approvals)
        .where(t.tool_approvals.c.invocation_id.in_(ids), t.tool_approvals.c.status == "pending")
        .values(status=status, decided_at=now())
        .returning(t.tool_approvals.c.invocation_id)
    )
    invocation_ids = [row.invocation_id for row in closed.all()]
    if invocation_ids:
        await conn.execute(
            update(t.tool_invocations)
            .where(
                t.tool_invocations.c.id.in_(invocation_ids),
                t.tool_invocations.c.status == "awaiting_approval",
            )
            .values(
                status="cancelled",
                error_code="approval_" + status,
                error_message="The action was not approved in time."
                if status == "expired"
                else "The action is no longer pending.",
                completed_at=now(),
            )
        )
    return len(invocation_ids)


async def invocations_of_run(conn: AsyncConnection, run_id: UUID) -> list[InvocationRow]:
    return list(
        (
            await conn.execute(
                select(t.tool_invocations)
                .where(t.tool_invocations.c.run_id == run_id)
                .order_by(t.tool_invocations.c.step, t.tool_invocations.c.position)
            )
        ).all()
    )


async def count_for_run(conn: AsyncConnection, run_id: UUID) -> int:
    rows = await conn.execute(
        select(t.tool_invocations.c.id).where(t.tool_invocations.c.run_id == run_id)
    )
    return len(rows.all())


# --- Approvals -----------------------------------------------------------------------------


async def create_approval(conn: AsyncConnection, **values: Any) -> ApprovalRow:
    return cast(
        Row[Any],
        (
            await conn.execute(
                insert(t.tool_approvals)
                .values(id=new_id(), created_at=now(), status="pending", **values)
                .returning(t.tool_approvals)
            )
        ).one(),
    )


async def approvals_of(conn: AsyncConnection, invocation_ids: list[UUID]) -> list[ApprovalRow]:
    if not invocation_ids:
        return []
    return list(
        (
            await conn.execute(
                select(t.tool_approvals).where(t.tool_approvals.c.invocation_id.in_(invocation_ids))
            )
        ).all()
    )


async def approval(
    conn: AsyncConnection, organization_id: UUID, approval_id: UUID, *, for_update: bool = False
) -> ApprovalRow | None:
    stmt = select(t.tool_approvals).where(
        t.tool_approvals.c.id == approval_id,
        t.tool_approvals.c.organization_id == organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return _first(await conn.execute(stmt))


async def decide(
    conn: AsyncConnection, approval_id: UUID, *, status: str, decided_by: UUID
) -> ApprovalRow | None:
    """Record a decision on a pending approval; None if it was decided (or expired) already."""
    return _first(
        await conn.execute(
            update(t.tool_approvals)
            .where(t.tool_approvals.c.id == approval_id, t.tool_approvals.c.status == "pending")
            .values(status=status, decided_by_id=decided_by, decided_at=now())
            .returning(t.tool_approvals)
        )
    )


async def close_pending_approvals(conn: AsyncConnection, message_id: UUID, *, status: str) -> int:
    """Expire or invalidate every pending approval of a reply, and cancel their calls."""
    ids = select(t.tool_invocations.c.id).where(t.tool_invocations.c.message_id == message_id)
    closed = await conn.execute(
        update(t.tool_approvals)
        .where(t.tool_approvals.c.invocation_id.in_(ids), t.tool_approvals.c.status == "pending")
        .values(status=status, decided_at=now())
        .returning(t.tool_approvals.c.invocation_id)
    )
    invocation_ids = [row.invocation_id for row in closed.all()]
    if invocation_ids:
        await conn.execute(
            update(t.tool_invocations)
            .where(
                t.tool_invocations.c.id.in_(invocation_ids),
                t.tool_invocations.c.status == "awaiting_approval",
            )
            .values(
                status="cancelled",
                error_code="approval_" + status,
                error_message="The action was not approved in time."
                if status == "expired"
                else "The action is no longer pending.",
                completed_at=now(),
            )
        )
    return len(invocation_ids)


# --- Settings -------------------------------------------------------------------------------


async def switches(conn: AsyncConnection, organization_id: UUID) -> dict[str, tuple[bool, bool]]:
    """tool id -> (enabled platform-wide, enabled in this organization); True when no row."""
    rows = (
        await conn.execute(
            select(
                t.tool_settings.c.tool_id,
                t.tool_settings.c.organization_id,
                t.tool_settings.c.enabled,
            ).where(
                or_(
                    t.tool_settings.c.organization_id.is_(None),
                    t.tool_settings.c.organization_id == organization_id,
                )
            )
        )
    ).all()
    found: dict[str, tuple[bool, bool]] = {}
    for row in rows:
        platform, organization = found.get(row.tool_id, (True, True))
        if row.organization_id is None:
            platform = row.enabled
        else:
            organization = row.enabled
        found[row.tool_id] = (platform, organization)
    return found


async def set_organization_switch(
    conn: AsyncConnection, organization_id: UUID, tool_id: str, *, enabled: bool, user_id: UUID
) -> None:
    stamp = now()
    await conn.execute(
        pg_insert(t.tool_settings)
        .values(
            id=new_id(),
            organization_id=organization_id,
            tool_id=tool_id,
            enabled=enabled,
            updated_by_id=user_id,
            created_at=stamp,
            updated_at=stamp,
        )
        .on_conflict_do_update(
            index_elements=["organization_id", "tool_id"],
            index_where=and_(t.tool_settings.c.organization_id.is_not(None)),
            set_={"enabled": enabled, "updated_by_id": user_id, "updated_at": stamp},
        )
    )

"""Queries over the agent tables. Every state change of a run is one conditional statement
(claim-if-queued, finish-if-running, …), so two workers or two requests can never both own a
run or both end it; every read is scoped by organization."""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Row, and_, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from nexa_common.errors import ValidationFailed
from nexa_common.ids import new_id
from nexa_runtime.agents import tables as t
from nexa_runtime.tools import repository as tools_repo
from nexa_runtime.tools import tables as tool_tables

AgentRow = Row[Any]
RunRow = Row[Any]
StepRow = Row[Any]

ACTIVE = ("queued", "running", "waiting_for_approval", "paused", "cancel_requested")
FINAL = ("completed", "failed", "cancelled", "timed_out")


def now() -> datetime:
    return datetime.now(UTC)


def _first(result: Any) -> Row[Any] | None:
    return cast("Row[Any] | None", result.first())


# --- Agents ---------------------------------------------------------------------------------


async def get_agent(
    conn: AsyncConnection, organization_id: UUID, agent_id: UUID
) -> AgentRow | None:
    """An agent of this organization, or a platform agent; never another organization's."""
    return _first(
        await conn.execute(
            select(t.agents).where(
                t.agents.c.id == agent_id,
                t.agents.c.deleted_at.is_(None),
                or_(
                    t.agents.c.organization_id == organization_id,
                    t.agents.c.organization_id.is_(None),
                ),
            )
        )
    )


# --- Runs -----------------------------------------------------------------------------------


async def create_run(conn: AsyncConnection, **values: Any) -> RunRow:
    stamp = now()
    defaults: dict[str, Any] = {
        "id": new_id(),
        "status": "queued",
        "failure_reason": "",
        "error_message": "",
        "progress_message": "",
        "step_count": 0,
        "model_calls": 0,
        "tool_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_micros": None,
        "result": "",
        "output": None,
        "sources": [],
        "cancel_requested": False,
        "pause_requested": False,
        "claimed_by": "",
        "lease_expires_at": None,
        "attempts": 0,
        "idempotency_key": "",
        "request_id": "",
        "created_at": stamp,
        "updated_at": stamp,
    }
    defaults.update(values)
    return cast(
        Row[Any],
        (await conn.execute(insert(t.agent_runs).values(**defaults).returning(t.agent_runs))).one(),
    )


async def get_run(
    conn: AsyncConnection, organization_id: UUID, run_id: UUID, *, for_update: bool = False
) -> RunRow | None:
    stmt = select(t.agent_runs).where(
        t.agent_runs.c.id == run_id, t.agent_runs.c.organization_id == organization_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    return _first(await conn.execute(stmt))


async def run_by_idempotency_key(conn: AsyncConnection, user_id: UUID, key: str) -> RunRow | None:
    return _first(
        await conn.execute(
            select(t.agent_runs).where(
                t.agent_runs.c.user_id == user_id, t.agent_runs.c.idempotency_key == key
            )
        )
    )


async def list_runs(
    conn: AsyncConnection,
    organization_id: UUID,
    user_id: UUID,
    *,
    status: str | None,
    agent_id: UUID | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[RunRow], str | None]:
    """One page of the user's runs, newest first."""
    stmt = select(t.agent_runs).where(
        t.agent_runs.c.organization_id == organization_id, t.agent_runs.c.user_id == user_id
    )
    if status == "active":
        stmt = stmt.where(t.agent_runs.c.status.in_(ACTIVE))
    elif status == "finished":
        stmt = stmt.where(t.agent_runs.c.status.in_(FINAL))
    elif status:
        stmt = stmt.where(t.agent_runs.c.status == status)
    if agent_id is not None:
        stmt = stmt.where(t.agent_runs.c.agent_id == agent_id)
    if cursor:
        created_at, run_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                t.agent_runs.c.created_at < created_at,
                and_(t.agent_runs.c.created_at == created_at, t.agent_runs.c.id < run_id),
            )
        )
    rows = list(
        (
            await conn.execute(
                stmt.order_by(t.agent_runs.c.created_at.desc(), t.agent_runs.c.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id)
    return rows, next_cursor


async def count_active(
    conn: AsyncConnection, *, organization_id: UUID, user_id: UUID | None
) -> int:
    stmt = select(func.count()).where(
        t.agent_runs.c.organization_id == organization_id, t.agent_runs.c.status.in_(ACTIVE)
    )
    if user_id is not None:
        stmt = stmt.where(t.agent_runs.c.user_id == user_id)
    return int((await conn.execute(stmt)).scalar_one())


async def update_run(conn: AsyncConnection, run_id: UUID, **values: Any) -> RunRow | None:
    return _first(
        await conn.execute(
            update(t.agent_runs)
            .where(t.agent_runs.c.id == run_id)
            .values(updated_at=now(), **values)
            .returning(t.agent_runs)
        )
    )


async def transition(
    conn: AsyncConnection, run_id: UUID, *, from_states: tuple[str, ...], **values: Any
) -> RunRow | None:
    """Change a run only from one of `from_states`; None when it is in another state now."""
    return _first(
        await conn.execute(
            update(t.agent_runs)
            .where(t.agent_runs.c.id == run_id, t.agent_runs.c.status.in_(from_states))
            .values(updated_at=now(), **values)
            .returning(t.agent_runs)
        )
    )


async def claim_next(conn: AsyncConnection, *, worker: str, lease_seconds: int) -> RunRow | None:
    """Take the oldest queued run (or a running one whose lease expired: its worker died),
    with `FOR UPDATE SKIP LOCKED`, so workers never take the same run."""
    stamp = now()
    candidate = (
        select(t.agent_runs.c.id)
        .where(
            or_(
                t.agent_runs.c.status == "queued",
                and_(
                    t.agent_runs.c.status.in_(("running", "cancel_requested")),
                    t.agent_runs.c.lease_expires_at < stamp,
                ),
            )
        )
        .order_by(t.agent_runs.c.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return _first(
        await conn.execute(
            update(t.agent_runs)
            .where(t.agent_runs.c.id == candidate)
            .values(
                claimed_by=worker,
                lease_expires_at=stamp + timedelta(seconds=lease_seconds),
                attempts=t.agent_runs.c.attempts + 1,
                updated_at=stamp,
            )
            .returning(t.agent_runs)
        )
    )


async def renew_lease(
    conn: AsyncConnection, run_id: UUID, *, worker: str, lease_seconds: int
) -> RunRow | None:
    """Extend this worker's lease and read the control flags; None when the run is no longer
    this worker's (its lease expired and another worker took it)."""
    return _first(
        await conn.execute(
            update(t.agent_runs)
            .where(t.agent_runs.c.id == run_id, t.agent_runs.c.claimed_by == worker)
            .values(lease_expires_at=now() + timedelta(seconds=lease_seconds))
            .returning(t.agent_runs)
        )
    )


async def release(
    conn: AsyncConnection, run_id: UUID, *, worker: str, **values: Any
) -> RunRow | None:
    """Give the run up (paused, waiting, finished): the lease ends with the claim."""
    return _first(
        await conn.execute(
            update(t.agent_runs)
            .where(t.agent_runs.c.id == run_id, t.agent_runs.c.claimed_by == worker)
            .values(claimed_by="", lease_expires_at=None, updated_at=now(), **values)
            .returning(t.agent_runs)
        )
    )


def stopped_clock(row: Any) -> dict[str, Any]:
    """Values that stop a run's clock (it pauses or waits for a person): keep what is left."""
    if row.deadline_at is None:
        return {}
    left = max(0, int((row.deadline_at - now()).total_seconds()))
    return {"time_left_seconds": left}


def restarted_clock(row: Any) -> dict[str, Any]:
    """Values that restart a stopped clock: the deadline is now plus what was left."""
    if row.time_left_seconds is None:
        return {}
    return {
        "deadline_at": now() + timedelta(seconds=row.time_left_seconds),
        "time_left_seconds": None,
    }


async def expired_waiting(
    conn: AsyncConnection, *, at: datetime, stale_before: datetime, limit: int = 100
) -> list[RunRow]:
    """Runs waiting for an approval that has expired, and (defensively) runs waiting since
    before `stale_before` with no pending approval at all. The runner's sweep ends them."""
    approvals = tool_tables.tool_approvals
    invocations = tool_tables.tool_invocations
    pending = (
        select(approvals.c.id)
        .select_from(approvals.join(invocations, invocations.c.id == approvals.c.invocation_id))
        .where(invocations.c.run_id == t.agent_runs.c.id, approvals.c.status == "pending")
    )
    expired = pending.where(approvals.c.expires_at <= at)
    return list(
        (
            await conn.execute(
                select(t.agent_runs)
                .where(
                    t.agent_runs.c.status == "waiting_for_approval",
                    or_(
                        expired.exists(),
                        and_(~pending.exists(), t.agent_runs.c.updated_at < stale_before),
                    ),
                )
                .order_by(t.agent_runs.c.updated_at)
                .limit(limit)
            )
        ).all()
    )


async def fail_waiting(conn: AsyncConnection, run_id: UUID) -> RunRow | None:
    """End a run whose pending action was not approved in time; None if it is no longer
    waiting (decided or cancelled meanwhile)."""
    row = await transition(
        conn,
        run_id,
        from_states=("waiting_for_approval",),
        status="failed",
        failure_reason="approval_expired",
        error_message="The action was not approved in time.",
        completed_at=now(),
        claimed_by="",
        lease_expires_at=None,
        time_left_seconds=None,
    )
    if row is not None:
        await tools_repo.close_pending_approvals_of_run(conn, run_id, status="expired")
        await tools_repo.cancel_open_run(conn, run_id)
        await close_open_steps(conn, run_id, status="failed")
    return row


async def add_usage(
    conn: AsyncConnection,
    run_id: UUID,
    *,
    steps: int,
    model_calls: int,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int | None,
    model: str | None,
) -> RunRow | None:
    """Add one model call's measured usage (and the step it produced) to the run's counters."""
    values: dict[str, Any] = {
        "step_count": t.agent_runs.c.step_count + steps,
        "model_calls": t.agent_runs.c.model_calls + model_calls,
        "input_tokens": t.agent_runs.c.input_tokens + input_tokens,
        "output_tokens": t.agent_runs.c.output_tokens + output_tokens,
        "updated_at": now(),
    }
    if cost_micros is not None:
        values["cost_micros"] = func.coalesce(t.agent_runs.c.cost_micros, 0) + cost_micros
    if model:
        values["model"] = model
    return _first(
        await conn.execute(
            update(t.agent_runs)
            .where(t.agent_runs.c.id == run_id)
            .values(**values)
            .returning(t.agent_runs)
        )
    )


# --- Steps ----------------------------------------------------------------------------------


async def add_step(conn: AsyncConnection, **values: Any) -> StepRow:
    """Append a step with the next sequence number; the unique (run, sequence) key makes a
    concurrent duplicate fail instead of appearing twice."""
    stamp = now()
    run_id = values["run_id"]
    next_sequence = (
        select(func.coalesce(func.max(t.agent_steps.c.sequence), 0) + 1)
        .where(t.agent_steps.c.run_id == run_id)
        .scalar_subquery()
    )
    defaults: dict[str, Any] = {
        "id": new_id(),
        "status": "succeeded",
        "summary": "",
        "detail": {},
        "invocation_id": None,
        "error_code": "",
        "retry_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "usage_event_id": None,
        "started_at": stamp,
        "finished_at": stamp,
        "duration_ms": 0,
        "created_at": stamp,
    }
    defaults.update(values)
    defaults["sequence"] = next_sequence
    return cast(
        Row[Any],
        (
            await conn.execute(insert(t.agent_steps).values(**defaults).returning(t.agent_steps))
        ).one(),
    )


async def update_step(conn: AsyncConnection, step_id: UUID, **values: Any) -> StepRow | None:
    return _first(
        await conn.execute(
            update(t.agent_steps)
            .where(t.agent_steps.c.id == step_id)
            .values(**values)
            .returning(t.agent_steps)
        )
    )


async def steps_of(
    conn: AsyncConnection, run_id: UUID, *, after: int = 0, limit: int | None = None
) -> list[StepRow]:
    stmt = (
        select(t.agent_steps)
        .where(t.agent_steps.c.run_id == run_id, t.agent_steps.c.sequence > after)
        .order_by(t.agent_steps.c.sequence)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await conn.execute(stmt)).all())


async def close_open_steps(conn: AsyncConnection, run_id: UUID, *, status: str) -> int:
    """Steps still running or waiting when the run ended: closed with the run."""
    stamp = now()
    result = await conn.execute(
        update(t.agent_steps)
        .where(t.agent_steps.c.run_id == run_id, t.agent_steps.c.status.in_(("running", "waiting")))
        .values(status=status, finished_at=stamp)
    )
    return result.rowcount


async def last_step(conn: AsyncConnection, run_id: UUID) -> StepRow | None:
    return _first(
        await conn.execute(
            select(t.agent_steps)
            .where(t.agent_steps.c.run_id == run_id)
            .order_by(t.agent_steps.c.sequence.desc())
            .limit(1)
        )
    )


async def ping(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT 1"))


# --- Cursors --------------------------------------------------------------------------------


def encode_cursor(created_at: datetime, run_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{run_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, run_id = base64.urlsafe_b64decode(padded).decode().split("|", 1)
        return datetime.fromisoformat(created), UUID(run_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationFailed(
            details=[{"field": "cursor", "code": "invalid", "message": "Invalid cursor."}]
        ) from exc

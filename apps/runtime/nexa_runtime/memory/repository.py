"""Queries of the memory tables. Every query takes the tenant and the visibility set
explicitly; the service decides what the caller may see, this module never widens it."""

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    Row,
    and_,
    case,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from nexa_common.ids import new_id
from nexa_runtime.chat import tables as chat
from nexa_runtime.chat.repository import decode_cursor, encode_cursor
from nexa_runtime.memory import tables as t

MemoryRow = Row[Any]
SettingsRow = Row[Any]
SummaryRow = Row[Any]

SEARCH_CONFIG = "english"
_WORD = re.compile(r"[a-z0-9]{3,}")
_MAX_TERMS = 32


def query_terms(query: str) -> list[str]:
    """The words of a query worth matching, in order, without repeats."""
    terms: list[str] = []
    for word in _WORD.findall(query.lower()):
        if word not in terms:
            terms.append(word)
    return terms[:_MAX_TERMS]


def tsquery(query: str) -> Any:
    """Any of the query's words: a memory sharing some of them ranks by how many it shares."""
    return func.to_tsquery(SEARCH_CONFIG, " | ".join(query_terms(query)))


def now() -> datetime:
    return datetime.now(UTC)


# --- Visibility ----------------------------------------------------------------------------


def visible_to(
    organization_id: UUID, user_id: UUID, team_ids: Iterable[UUID], *, manage_all: bool = False
) -> ColumnElement[bool]:
    """Rows this member may see: their own private memories, their teams' memories, and the
    organization's. `memory.manage_all` adds every team's memories, never anyone's private
    ones."""
    m = t.memories.c
    teams = list(team_ids)
    shared = [m.scope == "organization"]
    if manage_all:
        shared.append(m.scope == "team")
    elif teams:
        shared.append(and_(m.scope == "team", m.team_id.in_(teams)))
    return and_(
        m.organization_id == organization_id,
        or_(and_(m.scope.in_(("conversation", "user")), m.owner_id == user_id), *shared),
    )


def _not_expired(at: datetime) -> ColumnElement[bool]:
    m = t.memories.c
    return or_(m.expires_at.is_(None), m.expires_at > at)


# --- Memories ------------------------------------------------------------------------------


async def get_memory(
    conn: AsyncConnection, organization_id: UUID, memory_id: UUID, *, for_update: bool = False
) -> MemoryRow | None:
    query = select(t.memories).where(
        t.memories.c.organization_id == organization_id, t.memories.c.id == memory_id
    )
    if for_update:
        query = query.with_for_update()
    return (await conn.execute(query)).one_or_none()


async def list_memories(
    conn: AsyncConnection,
    visibility: ColumnElement[bool],
    *,
    scope: str | None,
    kind: str | None,
    status: str | None,
    conversation_id: UUID | None,
    query: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[MemoryRow], str | None]:
    m = t.memories.c
    stmt = select(t.memories).where(visibility)
    if scope:
        stmt = stmt.where(m.scope == scope)
    if kind:
        stmt = stmt.where(m.kind == kind)
    if status:
        stmt = stmt.where(m.status == status)
    if conversation_id is not None:
        stmt = stmt.where(m.conversation_id == conversation_id)
    if query_terms(query):
        stmt = stmt.where(m.search.op("@@")(tsquery(query)))
    if cursor:
        created_at, last_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(m.created_at < created_at, and_(m.created_at == created_at, m.id < last_id))
        )
    rows = list(
        (await conn.execute(stmt.order_by(m.created_at.desc(), m.id.desc()).limit(limit + 1))).all()
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id)
    return rows, next_cursor


async def insert_memory(conn: AsyncConnection, **values: Any) -> MemoryRow:
    at = now()
    values.setdefault("id", new_id())
    values.setdefault("created_at", at)
    values.setdefault("updated_at", at)
    values.setdefault("key", "")
    values.setdefault("value", None)
    values.setdefault("confidence", 1.0)
    values.setdefault("importance", 3)
    result = await conn.execute(insert(t.memories).values(**values).returning(t.memories))
    return cast(Row[Any], result.one())


async def update_memory(conn: AsyncConnection, memory_id: UUID, **values: Any) -> MemoryRow:
    values["updated_at"] = now()
    result = await conn.execute(
        update(t.memories)
        .where(t.memories.c.id == memory_id)
        .values(**values)
        .returning(t.memories)
    )
    return cast(Row[Any], result.one())


async def delete_memory(conn: AsyncConnection, memory_id: UUID) -> None:
    await conn.execute(delete(t.memories).where(t.memories.c.id == memory_id))


async def delete_where(conn: AsyncConnection, *conditions: ColumnElement[bool]) -> int:
    result = await conn.execute(delete(t.memories).where(*conditions))
    return int(result.rowcount)


def same_target(
    *,
    organization_id: UUID,
    scope: str,
    owner_id: UUID,
    team_id: UUID | None,
    conversation_id: UUID | None,
) -> ColumnElement[bool]:
    """The rows that share one scope target (the subject uniqueness domain)."""
    m = t.memories.c
    base = and_(m.organization_id == organization_id, m.scope == scope)
    if scope == "user":
        return and_(base, m.owner_id == owner_id)
    if scope == "conversation":
        return and_(base, m.owner_id == owner_id, m.conversation_id == conversation_id)
    if scope == "team":
        return and_(base, m.team_id == team_id)
    return base


async def active_by_key(
    conn: AsyncConnection, target: ColumnElement[bool], key: str, *, for_update: bool = False
) -> MemoryRow | None:
    m = t.memories.c
    stmt = select(t.memories).where(target, m.key == key, m.status == "active")
    if for_update:
        stmt = stmt.with_for_update()
    return (await conn.execute(stmt)).one_or_none()


async def by_hash(
    conn: AsyncConnection, target: ColumnElement[bool], content_hash: str
) -> MemoryRow | None:
    """A memory with the same text in the same place, whatever its status: a repeat of an
    active one is a duplicate, a repeat of a rejected one must not be proposed again."""
    m = t.memories.c
    stmt = (
        select(t.memories)
        .where(target, m.content_hash == content_hash, m.status != "superseded")
        .order_by(m.created_at.desc())
        .limit(1)
    )
    return (await conn.execute(stmt)).one_or_none()


async def supersede(conn: AsyncConnection, old_id: UUID, new_id_: UUID) -> None:
    await conn.execute(
        update(t.memories)
        .where(t.memories.c.id == old_id)
        .values(status="superseded", superseded_by_id=new_id_, updated_at=now())
    )


async def retire(conn: AsyncConnection, memory_id: UUID) -> None:
    """Take a memory out of the active set, before its successor is stored (one active
    memory per subject is a database rule)."""
    await conn.execute(
        update(t.memories)
        .where(t.memories.c.id == memory_id)
        .values(status="superseded", updated_at=now())
    )


async def touch_accessed(conn: AsyncConnection, ids: Sequence[UUID], at: datetime) -> None:
    if ids:
        await conn.execute(
            update(t.memories).where(t.memories.c.id.in_(list(ids))).values(last_accessed_at=at)
        )


# --- Retrieval -----------------------------------------------------------------------------

# Weights of the transparent ranking (docs/architecture/memory-engine.md#retrieval).
RELEVANCE_WEIGHT = 4.0
IMPORTANCE_WEIGHT = 1.0
CONFIDENCE_WEIGHT = 1.0
RECENCY_WEIGHT = 0.5
SCOPE_WEIGHTS = {"conversation": 1.0, "user": 0.9, "team": 0.7, "organization": 0.5}


def score_expression(query: str, at: datetime) -> ColumnElement[float]:
    m = t.memories.c
    relevance = func.ts_rank_cd(m.search, tsquery(query))
    age_days = (
        func.extract("epoch", literal(at) - func.coalesce(m.last_accessed_at, m.created_at))
        / 86400.0
    )
    recency = 1.0 / (1.0 + age_days / 30.0)
    scope_weight = case(dict(SCOPE_WEIGHTS), value=m.scope, else_=0.5)
    return (
        relevance * RELEVANCE_WEIGHT
        + (m.importance / 5.0) * IMPORTANCE_WEIGHT
        + m.confidence * CONFIDENCE_WEIGHT
        + recency * RECENCY_WEIGHT
        + scope_weight
    )


async def search(
    conn: AsyncConnection,
    visibility: ColumnElement[bool],
    *,
    query: str,
    limit: int,
    at: datetime,
    statuses: Sequence[str] = ("active",),
) -> list[tuple[MemoryRow, float]]:
    """Unexpired memories matching `query`, best first. Nothing matches an empty or
    all-stopword query: a memory is recalled for a reason, never because it exists."""
    m = t.memories.c
    if not query_terms(query):
        return []
    score = score_expression(query, at).label("score")
    stmt = select(t.memories, score).where(
        visibility,
        _not_expired(at),
        m.status.in_(list(statuses)),
        m.search.op("@@")(tsquery(query)),
    )
    rows = (await conn.execute(stmt.order_by(score.desc(), m.created_at.desc()).limit(limit))).all()
    return [(row, float(row.score)) for row in rows]


async def standing_preferences(
    conn: AsyncConnection,
    *,
    organization_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    limit: int,
    at: datetime,
) -> list[MemoryRow]:
    """The user's own preferences, recalled on every turn whatever the question."""
    m = t.memories.c
    stmt = (
        select(t.memories)
        .where(
            m.organization_id == organization_id,
            m.owner_id == user_id,
            m.kind == "preference",
            m.status == "active",
            _not_expired(at),
            or_(
                m.scope == "user",
                and_(m.scope == "conversation", m.conversation_id == conversation_id),
            ),
        )
        .order_by(m.importance.desc(), m.created_at.desc())
        .limit(limit)
    )
    return list((await conn.execute(stmt)).all())


# --- Settings ------------------------------------------------------------------------------


async def get_settings(
    conn: AsyncConnection, organization_id: UUID, user_id: UUID
) -> SettingsRow | None:
    s = t.memory_settings.c
    return (
        await conn.execute(
            select(t.memory_settings).where(
                s.organization_id == organization_id, s.user_id == user_id
            )
        )
    ).one_or_none()


async def upsert_settings(
    conn: AsyncConnection, organization_id: UUID, user_id: UUID, **values: Any
) -> SettingsRow:
    defaults = {
        "id": new_id(),
        "organization_id": organization_id,
        "user_id": user_id,
        "extraction_enabled": True,
        "auto_save": True,
        "retention_days": None,
        "updated_at": now(),
    }
    stmt = (
        pg_insert(t.memory_settings)
        .values(**{**defaults, **values})
        .on_conflict_do_update(
            constraint="memory_settings_member_uniq", set_={**values, "updated_at": now()}
        )
        .returning(t.memory_settings)
    )
    row: Row[Any] = (await conn.execute(stmt)).one()
    return row


# --- Summaries -----------------------------------------------------------------------------


async def latest_summary(conn: AsyncConnection, conversation_id: UUID) -> SummaryRow | None:
    s = t.conversation_summaries.c
    stmt = (
        select(t.conversation_summaries)
        .where(s.conversation_id == conversation_id)
        .order_by(s.version.desc())
        .limit(1)
    )
    return (await conn.execute(stmt)).one_or_none()


async def insert_summary(conn: AsyncConnection, **values: Any) -> SummaryRow | None:
    """None when this version already exists: another process summarized the same span."""
    values.setdefault("id", new_id())
    values.setdefault("created_at", now())
    stmt = (
        pg_insert(t.conversation_summaries)
        .values(**values)
        .on_conflict_do_nothing(constraint="conversation_summaries_version_uniq")
        .returning(t.conversation_summaries)
    )
    row: Row[Any] | None = (await conn.execute(stmt)).one_or_none()
    return row


async def messages_between(
    conn: AsyncConnection, conversation_id: UUID, *, after: int, through: int
) -> Sequence[Any]:
    c = chat.messages.c
    stmt = (
        select(c.sequence, c.role, c.content, c.status)
        .where(c.conversation_id == conversation_id, c.sequence > after, c.sequence <= through)
        .order_by(c.sequence)
    )
    return list((await conn.execute(stmt)).all())


# --- Extraction claims ---------------------------------------------------------------------


async def claim_extraction(
    conn: AsyncConnection, *, organization_id: UUID, conversation_id: UUID, message_id: UUID
) -> UUID | None:
    """One extraction per message: None when it was already claimed (a retry, a duplicate)."""
    stmt = (
        pg_insert(t.memory_extractions)
        .values(
            id=new_id(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            message_id=message_id,
            status="pending",
            candidates_found=0,
            stored=0,
            rejected=0,
            duplicates=0,
            error_code="",
            duration_ms=0,
            created_at=now(),
        )
        .on_conflict_do_nothing(index_elements=[t.memory_extractions.c.message_id])
        .returning(t.memory_extractions.c.id)
    )
    row = (await conn.execute(stmt)).one_or_none()
    return cast(UUID, row.id) if row is not None else None


async def finish_extraction(conn: AsyncConnection, extraction_id: UUID, **values: Any) -> None:
    values.setdefault("completed_at", now())
    await conn.execute(
        update(t.memory_extractions)
        .where(t.memory_extractions.c.id == extraction_id)
        .values(**values)
    )

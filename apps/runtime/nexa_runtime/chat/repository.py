"""Queries over the chat tables.

Every conversation read is scoped to one organization; the service applies the ownership
rule on top with the shared policy (`nexa_common.access.authorize`). Writes that must be
consistent run inside one transaction opened by the service.
"""

import base64
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, delete, func, insert, literal_column, or_, select, tuple_, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from nexa_common.errors import ValidationFailed
from nexa_common.ids import new_id
from nexa_runtime.chat import tables as t

ConversationRow = Row[Any]
MessageRow = Row[Any]

# Conversations are listed by their last activity: the last message, or creation if none.
_ACTIVITY = func.coalesce(t.conversations.c.last_message_at, t.conversations.c.created_at)


def now() -> datetime:
    return datetime.now(UTC)


# --- Conversations -------------------------------------------------------------------------


async def get_conversation(
    conn: AsyncConnection, organization_id: UUID, conversation_id: UUID, *, for_update: bool = False
) -> ConversationRow | None:
    stmt = select(t.conversations).where(
        t.conversations.c.id == conversation_id,
        t.conversations.c.organization_id == organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await conn.execute(stmt)).first()


async def list_conversations(
    conn: AsyncConnection,
    organization_id: UUID,
    owner_id: UUID,
    *,
    status: str,
    query: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[ConversationRow], str | None]:
    """One page of the owner's conversations, newest activity first. Returns the rows and
    the cursor for the next page (None on the last page)."""
    stmt = (
        select(t.conversations, _ACTIVITY.label("activity_at"))
        .where(
            t.conversations.c.organization_id == organization_id,
            t.conversations.c.owner_id == owner_id,
            t.conversations.c.status == status,
        )
        .order_by(_ACTIVITY.desc(), t.conversations.c.id.desc())
        .limit(limit + 1)
    )
    if query:
        stmt = stmt.where(t.conversations.c.title.ilike(f"%{_escape_like(query)}%", escape="\\"))
    if cursor:
        activity_at, last_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(_ACTIVITY, t.conversations.c.id) < tuple_(activity_at, last_id))
    rows = list((await conn.execute(stmt)).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].activity_at, rows[-1].id)
    return rows, next_cursor


async def create_conversation(
    conn: AsyncConnection,
    *,
    organization_id: UUID,
    owner_id: UUID,
    title: str,
    model: str,
    prompt_version: str,
) -> ConversationRow:
    moment = now()
    result = await conn.execute(
        insert(t.conversations)
        .values(
            id=new_id(),
            organization_id=organization_id,
            owner_id=owner_id,
            title=title,
            model=model,
            prompt_version=prompt_version,
            status="active",
            message_count=0,
            active_generation_id=None,
            generation_started_at=None,
            metadata={},
            created_at=moment,
            updated_at=moment,
            last_message_at=None,
        )
        .returning(t.conversations)
    )
    return cast(Row[Any], result.one())


async def update_conversation(
    conn: AsyncConnection, conversation_id: UUID, **values: Any
) -> ConversationRow:
    values["updated_at"] = now()
    result = await conn.execute(
        update(t.conversations)
        .where(t.conversations.c.id == conversation_id)
        .values(**values)
        .returning(t.conversations)
    )
    return cast(Row[Any], result.one())


async def delete_conversation(conn: AsyncConnection, conversation_id: UUID) -> None:
    # Messages go with it (ON DELETE CASCADE); usage ledger rows stay, they hold no text.
    await conn.execute(delete(t.conversations).where(t.conversations.c.id == conversation_id))


async def acquire_generation(
    conn: AsyncConnection, conversation_id: UUID, message_id: UUID, *, stale_before: datetime
) -> bool:
    """Claim the conversation for one generation. A lock older than `stale_before` belongs
    to a process that died and can be taken over."""
    result = await conn.execute(
        update(t.conversations)
        .where(
            t.conversations.c.id == conversation_id,
            or_(
                t.conversations.c.active_generation_id.is_(None),
                t.conversations.c.generation_started_at < stale_before,
            ),
        )
        .values(active_generation_id=message_id, generation_started_at=now())
    )
    return result.rowcount == 1


async def renew_generation(conn: AsyncConnection, conversation_id: UUID, message_id: UUID) -> None:
    """Keep this generation's claim fresh (a long tool loop is alive, not stale)."""
    await conn.execute(
        update(t.conversations)
        .where(
            t.conversations.c.id == conversation_id,
            t.conversations.c.active_generation_id == message_id,
        )
        .values(generation_started_at=now())
    )


async def release_generation(
    conn: AsyncConnection, conversation_id: UUID, message_id: UUID
) -> None:
    await conn.execute(
        update(t.conversations)
        .where(
            t.conversations.c.id == conversation_id,
            t.conversations.c.active_generation_id == message_id,
        )
        .values(active_generation_id=None, generation_started_at=None, updated_at=now())
    )


# --- Messages ------------------------------------------------------------------------------


async def list_messages(
    conn: AsyncConnection, conversation_id: UUID, *, limit: int, before: int | None
) -> tuple[list[MessageRow], int | None]:
    """The newest `limit` messages older than `before` (a sequence), oldest first. Returns
    the rows and the `before` value for the next older page (None when there is none)."""
    stmt = select(t.messages).where(t.messages.c.conversation_id == conversation_id)
    if before is not None:
        stmt = stmt.where(t.messages.c.sequence < before)
    rows = list(
        (await conn.execute(stmt.order_by(t.messages.c.sequence.desc()).limit(limit + 1))).all()
    )
    next_before = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_before = rows[-1].sequence
    rows.reverse()
    return rows, next_before


async def message_by_client_id(
    conn: AsyncConnection, conversation_id: UUID, client_message_id: str
) -> MessageRow | None:
    result = await conn.execute(
        select(t.messages).where(
            t.messages.c.conversation_id == conversation_id,
            t.messages.c.client_message_id == client_message_id,
        )
    )
    return result.first()


async def message_by_id(
    conn: AsyncConnection, conversation_id: UUID, message_id: UUID
) -> MessageRow | None:
    result = await conn.execute(
        select(t.messages).where(
            t.messages.c.conversation_id == conversation_id, t.messages.c.id == message_id
        )
    )
    return result.first()


async def message_at(
    conn: AsyncConnection, conversation_id: UUID, sequence: int
) -> MessageRow | None:
    result = await conn.execute(
        select(t.messages).where(
            t.messages.c.conversation_id == conversation_id,
            t.messages.c.sequence == sequence,
        )
    )
    return result.first()


async def last_message(conn: AsyncConnection, conversation_id: UUID) -> MessageRow | None:
    result = await conn.execute(
        select(t.messages)
        .where(t.messages.c.conversation_id == conversation_id)
        .order_by(t.messages.c.sequence.desc())
        .limit(1)
    )
    return result.first()


async def next_sequence(conn: AsyncConnection, conversation_id: UUID) -> int:
    result = await conn.execute(
        select(func.coalesce(func.max(t.messages.c.sequence), 0)).where(
            t.messages.c.conversation_id == conversation_id
        )
    )
    return int(result.scalar_one()) + 1


async def insert_message(
    conn: AsyncConnection,
    *,
    organization_id: UUID,
    conversation_id: UUID,
    sequence: int,
    role: str,
    content: str,
    status: str,
    client_message_id: str = "",
    model: str = "",
    message_id: UUID | None = None,
    grounding: dict[str, Any] | None = None,
) -> MessageRow:
    result = await conn.execute(
        insert(t.messages)
        .values(
            grounding=grounding,
            id=message_id or new_id(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            sequence=sequence,
            role=role,
            content=content,
            status=status,
            client_message_id=client_message_id,
            model=model,
            provider="",
            provider_model="",
            input_tokens=0,
            output_tokens=0,
            cost_micros=None,
            latency_ms=0,
            ttft_ms=None,
            usage_event_id=None,
            request_id="",
            finish_reason="",
            error_code="",
            created_at=now(),
            completed_at=None,
        )
        .returning(t.messages)
    )
    return cast(Row[Any], result.one())


async def update_message(conn: AsyncConnection, message_id: UUID, **values: Any) -> MessageRow:
    result = await conn.execute(
        update(t.messages)
        .where(t.messages.c.id == message_id)
        .values(**values)
        .returning(t.messages)
    )
    return cast(Row[Any], result.one())


async def fail_message(
    conn: AsyncConnection, message_id: UUID, *, error_code: str
) -> MessageRow | None:
    """Mark an unfinished reply failed, leaving its text as it is. None when the reply is gone
    or already finished. A grounded reply's record (a JSON object) says "failed" too; an
    ordinary reply's empty grounding is left as it is."""
    result = await conn.execute(
        update(t.messages)
        .where(t.messages.c.id == message_id, t.messages.c.status.in_(("pending", "streaming")))
        .values(
            status="failed",
            error_code=error_code,
            completed_at=now(),
            grounding=case(
                (
                    func.jsonb_typeof(t.messages.c.grounding) == "object",
                    func.jsonb_set(
                        t.messages.c.grounding,
                        literal_column("ARRAY['outcome']"),
                        literal_column("'\"failed\"'::jsonb"),
                    ),
                ),
                else_=t.messages.c.grounding,
            ),
        )
        .returning(t.messages)
    )
    return cast("Row[Any] | None", result.one_or_none())


async def delete_message(conn: AsyncConnection, message_id: UUID) -> None:
    await conn.execute(delete(t.messages).where(t.messages.c.id == message_id))


# --- Cursors -------------------------------------------------------------------------------


def encode_cursor(activity_at: datetime, conversation_id: UUID) -> str:
    raw = f"{activity_at.isoformat()}|{conversation_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        moment, _, identifier = raw.partition("|")
        activity_at = datetime.fromisoformat(moment)
        if activity_at.tzinfo is None:
            raise ValueError("naive")
        return activity_at, UUID(identifier)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationFailed(
            details=[{"field": "cursor", "code": "invalid", "message": "The cursor is invalid."}]
        ) from exc


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

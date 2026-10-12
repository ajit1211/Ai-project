"""The memory engine (docs/architecture/memory-engine.md).

    management API ──► MemoryEngine ──► memories, memory_settings            (PostgreSQL)
    chat turn ───────► recall()     ──► relevant memories + rolling summary ──► context
    completed reply ─► after_turn() ──► extraction, summarization (background, idempotent)

Everything is scoped by the caller's principal: the organization from the token, ownership
and team membership from the database. Private memories (conversation and user scope) are
their owner's alone, even from administrators; `memory.manage_all` reaches shared memories
only. Extraction and summarization are gateway calls metered to the user, run after the
reply in a task of their own; they can fail without the reply noticing.
"""

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from nexa_ai.context import AIRequestContext
from nexa_ai.errors import AIError, AIModelUnavailable
from nexa_ai.gateway import Gateway
from nexa_ai.tokens import estimate_text_tokens
from nexa_ai.types import ChatRequest, ChatResponse, Feature, JSONSchemaFormat, Message
from nexa_common.access import Principal, require
from nexa_common.errors import Conflict, ErrorCode, NexaError, NotFound, PermissionDenied
from nexa_common.logs import get_logger
from nexa_runtime.chat import repository as chat_repo
from nexa_runtime.chat.context import MemoryItem, SummaryItem
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.memory import extraction, policy, summarize
from nexa_runtime.memory import repository as repo
from nexa_runtime.memory import tables as t
from nexa_runtime.memory.schemas import (
    PRIVATE_SCOPES,
    ClearMemories,
    ClearResult,
    CreateMemory,
    MemoryOut,
    MemorySettingsOut,
    SearchHit,
    UpdateMemory,
    UpdateMemorySettings,
)

log = get_logger("nexa.runtime.memory")

# What one extraction or summary call may read, in characters of message text.
_EXTRACTION_TEXT_CHARS = 4_000
_SUMMARY_MESSAGE_CHARS = 2_000
_SUMMARY_TOTAL_CHARS = 36_000
_STRUCTURED_MAX_OUTPUT_TOKENS = 1_500


class MemoryRefused(NexaError):
    """The text cannot be stored as a memory (a secret, for example)."""

    status = 422
    code = ErrorCode.VALIDATION_FAILED


class MemoryEngine:
    def __init__(self, *, engine: AsyncEngine, gateway: Gateway, settings: RuntimeSettings) -> None:
        self._engine = engine
        self._gateway = gateway
        self._settings = settings

    # --- Management ----------------------------------------------------------------------

    async def list_memories(
        self,
        principal: Principal,
        *,
        scope: str | None,
        kind: str | None,
        status: str | None,
        conversation_id: UUID | None,
        query: str,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[MemoryOut], str | None]:
        require(principal, "memory.view")
        async with self._engine.connect() as conn:
            rows, next_cursor = await repo.list_memories(
                conn,
                self._visible(principal),
                scope=scope,
                kind=kind,
                status=status,
                conversation_id=conversation_id,
                query=query.strip(),
                limit=limit,
                cursor=cursor,
            )
        return [self._out(principal, row) for row in rows], next_cursor

    async def search(self, principal: Principal, *, query: str, limit: int) -> list[SearchHit]:
        require(principal, "memory.view")
        async with self._engine.connect() as conn:
            hits = await repo.search(
                conn,
                self._visible(principal),
                query=query.strip(),
                limit=limit,
                at=repo.now(),
                statuses=("active", "candidate"),
            )
        return [SearchHit(memory=self._out(principal, row), score=score) for row, score in hits]

    async def get(self, principal: Principal, memory_id: UUID) -> MemoryOut:
        async with self._engine.connect() as conn:
            row = await self._load(conn, principal, memory_id, "view")
        return self._out(principal, row)

    async def create(self, principal: Principal, body: CreateMemory) -> MemoryOut:
        """A memory the user writes down: active at once, the user being its source. The
        same statement saved twice is one memory."""
        require(principal, "memory.create")
        content = policy.normalize_content(body.content)
        if not content:
            raise MemoryRefused("The memory is empty.")
        if policy.refuse_secret(content) is not None:
            raise MemoryRefused(
                "This looks like a password, key or token. NEXA never stores those."
            )
        async with self._engine.begin() as conn:
            team_id, conversation_id = await self._scope_target(conn, principal, body)
            target = repo.same_target(
                organization_id=_organization(principal),
                scope=body.scope,
                owner_id=principal.user_id,
                team_id=team_id,
                conversation_id=conversation_id,
            )
            key = policy.normalize_key(body.key)
            existing = await repo.by_hash(conn, target, policy.content_hash(content))
            if existing is not None and existing.status == "active":
                return self._out(principal, existing)
            values: dict[str, Any] = {
                "organization_id": _organization(principal),
                "owner_id": principal.user_id,
                "team_id": team_id,
                "conversation_id": conversation_id,
                "scope": body.scope,
                "kind": body.kind,
                "key": key,
                "content": content,
                "value": body.value,
                "source_type": "manual",
                "confidence": 1.0,
                "importance": body.importance,
                "status": "active",
                "content_hash": policy.content_hash(content),
                "expires_at": body.expires_at or self._default_expiry(body.scope),
            }
            old = await repo.active_by_key(conn, target, key, for_update=True) if key else None
            if old is not None and (existing is None or old.id != existing.id):
                await repo.retire(conn, old.id)
            if existing is not None:
                # A candidate or a rejected proposal with these words: the user's own act now.
                values.pop("organization_id")
                row = await repo.update_memory(conn, existing.id, **values)
            else:
                row = await repo.insert_memory(conn, **values)
            if old is not None and old.id != row.id:
                await repo.supersede(conn, old.id, row.id)
        return self._out(principal, row)

    async def update(self, principal: Principal, memory_id: UUID, body: UpdateMemory) -> MemoryOut:
        sent = body.model_fields_set
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, memory_id, "update", for_update=True)
            values: dict[str, Any] = {}
            if body.content is not None:
                content = policy.normalize_content(body.content)
                if not content:
                    raise MemoryRefused("The memory is empty.")
                if policy.refuse_secret(content) is not None:
                    raise MemoryRefused(
                        "This looks like a password, key or token. NEXA never stores those."
                    )
                if content != row.content:
                    # Edited by the user: their statement now, whatever the model proposed.
                    values.update(
                        content=content,
                        content_hash=policy.content_hash(content),
                        source_type="manual",
                        confidence=1.0,
                    )
            if body.kind is not None:
                values["kind"] = body.kind
            if body.importance is not None:
                values["importance"] = body.importance
            if "value" in sent:
                values["value"] = body.value
            if "expires_at" in sent:
                values["expires_at"] = body.expires_at
            if body.key is not None and (key := policy.normalize_key(body.key)) != row.key:
                if row.status == "active" and key:
                    other = await repo.active_by_key(conn, self._target_of(row), key)
                    if other is not None:
                        raise Conflict("Another active memory already has this subject.")
                values["key"] = key
            if values:
                row = await repo.update_memory(conn, row.id, **values)
        return self._out(principal, row)

    async def delete(self, principal: Principal, memory_id: UUID) -> None:
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, memory_id, "delete", for_update=True)
            await repo.delete_memory(conn, row.id)

    async def approve(self, principal: Principal, memory_id: UUID) -> MemoryOut:
        """A candidate becomes active and takes its subject over from the memory that had it."""
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, memory_id, "update", for_update=True)
            if row.status != "candidate":
                raise Conflict("Only a candidate can be approved.")
            old = (
                await repo.active_by_key(conn, self._target_of(row), row.key, for_update=True)
                if row.key
                else None
            )
            if old is not None and old.id != row.id:
                await repo.retire(conn, old.id)
            row = await repo.update_memory(
                conn, row.id, status="active", expires_at=self._default_expiry(row.scope)
            )
            if old is not None and old.id != row.id:
                await repo.supersede(conn, old.id, row.id)
        return self._out(principal, row)

    async def reject(self, principal: Principal, memory_id: UUID) -> MemoryOut:
        """A rejected candidate is kept, so the same proposal is not made again."""
        async with self._engine.begin() as conn:
            row = await self._load(conn, principal, memory_id, "update", for_update=True)
            if row.status != "candidate":
                raise Conflict("Only a candidate can be rejected.")
            row = await repo.update_memory(conn, row.id, status="rejected")
        return self._out(principal, row)

    async def clear(self, principal: Principal, body: ClearMemories) -> ClearResult:
        """Delete the caller's own private memories. Shared memories are never cleared here."""
        require(principal, "memory.delete")
        m = t.memories.c
        conditions = [
            m.organization_id == _organization(principal),
            m.owner_id == principal.user_id,
            m.scope.in_(tuple(PRIVATE_SCOPES)),
        ]
        if body.scope == "user":
            conditions.append(m.scope == "user")
        elif body.scope == "conversation":
            if body.conversation_id is None:
                raise MemoryRefused(
                    "conversation_id is required to clear a conversation's memories."
                )
            conditions += [m.scope == "conversation", m.conversation_id == body.conversation_id]
        elif body.scope == "candidates":
            conditions.append(m.status == "candidate")
        async with self._engine.begin() as conn:
            deleted = await repo.delete_where(conn, *conditions)
        log.info(
            "memory.cleared", user_id=str(principal.user_id), scope=body.scope, deleted=deleted
        )
        return ClearResult(deleted=deleted)

    async def get_settings(self, principal: Principal) -> MemorySettingsOut:
        require(principal, "memory.view")
        async with self._engine.connect() as conn:
            row = await repo.get_settings(conn, _organization(principal), principal.user_id)
        return _settings_out(row)

    async def update_settings(
        self, principal: Principal, body: UpdateMemorySettings
    ) -> MemorySettingsOut:
        require(principal, "memory.update")
        values = {k: v for k, v in body.model_dump().items() if k in body.model_fields_set}
        async with self._engine.begin() as conn:
            row = await repo.upsert_settings(
                conn, _organization(principal), principal.user_id, **values
            )
        return _settings_out(row)

    # --- The generation path -------------------------------------------------------------

    async def recall(
        self, principal: Principal, *, conversation_id: UUID, query: str
    ) -> tuple[list[MemoryItem], SummaryItem | None]:
        """What a turn may use: the user's standing preferences, the memories relevant to
        the message, and the conversation's rolling summary. Recalled memories are marked
        as used. Nothing here grants access the principal does not already have."""
        if not self._settings.memory_enabled:
            return [], None
        started = time.perf_counter()
        at = repo.now()
        organization_id = _organization(principal)
        async with self._engine.begin() as conn:
            preferences = await repo.standing_preferences(
                conn,
                organization_id=organization_id,
                user_id=principal.user_id,
                conversation_id=conversation_id,
                limit=self._settings.memory_preference_limit,
                at=at,
            )
            hits = await repo.search(
                conn,
                self._visible(principal),
                query=query,
                limit=self._settings.memory_retrieval_limit,
                at=at,
            )
            items: list[MemoryItem] = []
            seen: set[UUID] = set()
            for row in preferences:
                seen.add(row.id)
                # Standing preferences outrank everything found by relevance.
                items.append(_item(row, score=100.0 + row.importance))
            for row, score in hits:
                if row.id not in seen:
                    seen.add(row.id)
                    items.append(_item(row, score=score))
            await repo.touch_accessed(conn, list(seen), at)
            latest = await repo.latest_summary(conn, conversation_id)
        summary = (
            SummaryItem(
                content=latest.content,
                through_sequence=latest.through_sequence,
                version=latest.version,
            )
            if latest is not None
            else None
        )
        log.info(
            "memory.recalled",
            conversation_id=str(conversation_id),
            preferences=len(preferences),
            relevant=len(hits),
            summary_version=latest.version if latest is not None else None,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return items, summary

    async def after_turn(
        self,
        principal: Principal,
        *,
        conversation: Any,
        user_message: Any,
        assistant_message: Any,
        request_id: str | None,
    ) -> None:
        """Extraction and summarization after a completed reply. Runs in its own task; every
        failure is logged and swallowed, because the reply has already succeeded."""
        if not self._settings.memory_enabled:
            return
        try:
            async with self._engine.connect() as conn:
                settings = await repo.get_settings(
                    conn, _organization(principal), principal.user_id
                )
            if settings is None or settings.extraction_enabled:
                await self._extract(
                    principal,
                    conversation=conversation,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    auto_save=settings.auto_save if settings is not None else True,
                    request_id=request_id,
                )
            await self._maybe_summarize(
                principal,
                conversation=conversation,
                last_sequence=assistant_message.sequence,
                request_id=request_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "memory.after_turn.failed",
                conversation_id=str(conversation.id),
                message_id=str(assistant_message.id),
                error=type(exc).__name__,
            )

    async def forget_conversation(self, conn: AsyncConnection, conversation_id: UUID) -> None:
        """Conversation-scoped memories go with their conversation; the rest keep their text
        and lose the reference (the database does that)."""
        m = t.memories.c
        await repo.delete_where(
            conn, m.scope == "conversation", m.conversation_id == conversation_id
        )

    # --- Extraction ----------------------------------------------------------------------

    async def _extract(
        self,
        principal: Principal,
        *,
        conversation: Any,
        user_message: Any,
        assistant_message: Any,
        auto_save: bool,
        request_id: str | None,
    ) -> None:
        organization_id = _organization(principal)
        async with self._engine.begin() as conn:
            extraction_id = await repo.claim_extraction(
                conn,
                organization_id=organization_id,
                conversation_id=conversation.id,
                message_id=user_message.id,
            )
        if extraction_id is None:
            log.info("memory.extraction.skipped", message_id=str(user_message.id), reason="claimed")
            return
        started = time.perf_counter()
        user_text = user_message.content[:_EXTRACTION_TEXT_CHARS]
        assistant_text = assistant_message.content[:_EXTRACTION_TEXT_CHARS]
        try:
            response = await self._structured_call(
                principal,
                messages=extraction.extraction_messages(user_text, assistant_text),
                schema_name=extraction.SCHEMA_NAME,
                schema=extraction.EXTRACTION_SCHEMA,
                feature=Feature.MEMORY,
                request_id=request_id,
                source_id=user_message.id,
            )
        except AIError as exc:
            async with self._engine.begin() as conn:
                await repo.finish_extraction(
                    conn,
                    extraction_id,
                    status="failed",
                    error_code=exc.error_code.value,
                    duration_ms=_elapsed_ms(started),
                    usage_event_id=exc.call_id,
                )
            log.warning(
                "memory.extraction.failed",
                message_id=str(user_message.id),
                error_code=exc.error_code.value,
            )
            return
        proposals, malformed = extraction.parse_proposals(response.parsed)
        stored = rejected = duplicates = 0
        reasons: dict[str, int] = {}
        async with self._engine.begin() as conn:
            for proposal in proposals:
                verdict = extraction.judge(proposal, user_text=user_text, auto_save=auto_save)
                reasons[verdict.reason] = reasons.get(verdict.reason, 0) + 1
                if verdict.outcome == "refused":
                    rejected += 1
                    continue
                outcome = await self._store_proposal(
                    conn,
                    principal,
                    verdict,
                    conversation_id=conversation.id,
                    message_id=user_message.id,
                )
                if outcome == "duplicate":
                    duplicates += 1
                else:
                    stored += 1
            await repo.finish_extraction(
                conn,
                extraction_id,
                status="completed",
                candidates_found=len(proposals),
                stored=stored,
                rejected=rejected + malformed,
                duplicates=duplicates,
                duration_ms=_elapsed_ms(started),
                usage_event_id=UUID(response.id),
            )
        log.info(
            "memory.extraction.completed",
            conversation_id=str(conversation.id),
            message_id=str(user_message.id),
            candidates=len(proposals),
            malformed=malformed,
            stored=stored,
            rejected=rejected,
            duplicates=duplicates,
            reasons=reasons,
            latency_ms=_elapsed_ms(started),
        )

    async def _store_proposal(
        self,
        conn: AsyncConnection,
        principal: Principal,
        verdict: extraction.Verdict,
        *,
        conversation_id: UUID,
        message_id: UUID,
    ) -> str:
        """Store one proposal as the user's private memory: active, or a candidate waiting
        for approval. Returns "duplicate" when the same statement is already there."""
        proposal = verdict.proposal
        organization_id = _organization(principal)
        content = policy.normalize_content(proposal.content)
        target = repo.same_target(
            organization_id=organization_id,
            scope="user",
            owner_id=principal.user_id,
            team_id=None,
            conversation_id=None,
        )
        content_hash = policy.content_hash(content)
        if await repo.by_hash(conn, target, content_hash) is not None:
            return "duplicate"  # already there, or rejected once: not proposed again
        key = policy.normalize_key(proposal.key)
        status, reason = verdict.outcome, verdict.reason
        old = await repo.active_by_key(conn, target, key, for_update=True) if key else None
        if (
            status == "active"
            and old is not None
            and (old.source_type == "manual" or old.kind != proposal.kind)
        ):
            # The user's own words, or a different kind of statement about the same subject
            # (a preference versus a decision): the user decides, not the model.
            status, reason = "candidate", "conflict"
        if status == "active" and old is not None:
            await repo.retire(conn, old.id)
        row = await repo.insert_memory(
            conn,
            organization_id=organization_id,
            owner_id=principal.user_id,
            team_id=None,
            conversation_id=conversation_id,
            source_message_id=message_id,
            scope="user",
            kind=proposal.kind,
            key=key,
            content=content,
            value=None,
            source_type=proposal.source_type,
            confidence=proposal.confidence,
            importance=proposal.importance,
            status=status,
            content_hash=content_hash,
            expires_at=self._candidate_expiry() if status == "candidate" else None,
        )
        if status == "active" and old is not None:
            await repo.supersede(conn, old.id, row.id)
        log.debug("memory.proposal.stored", status=status, reason=reason, kind=proposal.kind)
        return status

    # --- Summarization -------------------------------------------------------------------

    async def _maybe_summarize(
        self,
        principal: Principal,
        *,
        conversation: Any,
        last_sequence: int,
        request_id: str | None,
    ) -> None:
        """Summarize the messages that have fallen behind the recent window, once enough of
        them have accumulated, from the previous summary plus the new messages."""
        async with self._engine.connect() as conn:
            latest = await repo.latest_summary(conn, conversation.id)
            covered = latest.through_sequence if latest is not None else 0
            through = last_sequence - self._settings.memory_summary_keep_recent
            if last_sequence - covered < self._settings.memory_summary_after_messages:
                return
            if through <= covered:
                return
            rows = await repo.messages_between(
                conn, conversation.id, after=covered, through=through
            )
        transcript = _transcript(rows)
        if not transcript:
            return
        started = time.perf_counter()
        response = await self._structured_call(
            principal,
            messages=summarize.summary_messages(
                latest.content if latest is not None else None, transcript
            ),
            schema_name=summarize.SCHEMA_NAME,
            schema=summarize.SUMMARY_SCHEMA,
            feature=Feature.SUMMARY,
            request_id=request_id,
            source_id=conversation.id,
        )
        summary = summarize.parse_summary(response.parsed)
        if summary is None:
            log.warning("memory.summary.invalid", conversation_id=str(conversation.id))
            return
        version = (latest.version if latest is not None else 0) + 1
        async with self._engine.begin() as conn:
            inserted = await repo.insert_summary(
                conn,
                organization_id=_organization(principal),
                conversation_id=conversation.id,
                version=version,
                through_sequence=through,
                content=summary.text,
                token_estimate=estimate_text_tokens(summary.text),
                model=response.model,
                usage_event_id=UUID(response.id),
            )
            if inserted is None:
                return  # another process summarized the same span first
            await self._upsert_episode(conn, principal, conversation.id, summary.episode)
        log.info(
            "memory.summary.completed",
            conversation_id=str(conversation.id),
            version=version,
            through_sequence=through,
            messages=len(transcript),
            tokens=estimate_text_tokens(summary.text),
            latency_ms=_elapsed_ms(started),
        )

    async def _upsert_episode(
        self, conn: AsyncConnection, principal: Principal, conversation_id: UUID, episode: str
    ) -> None:
        """The episodic memory of this conversation: one per conversation, replaced on each
        new summary, so other conversations can recall what this one was about."""
        content = policy.normalize_content(episode)
        if not content:
            return
        organization_id = _organization(principal)
        target = repo.same_target(
            organization_id=organization_id,
            scope="user",
            owner_id=principal.user_id,
            team_id=None,
            conversation_id=None,
        )
        key = f"conversation:{conversation_id}"
        old = await repo.active_by_key(conn, target, key, for_update=True)
        if old is not None and old.content == content:
            return
        if old is not None:
            await repo.retire(conn, old.id)
        row = await repo.insert_memory(
            conn,
            organization_id=organization_id,
            owner_id=principal.user_id,
            team_id=None,
            conversation_id=conversation_id,
            source_message_id=None,
            scope="user",
            kind="episode",
            key=key,
            content=content,
            value=None,
            source_type="summary",
            confidence=0.8,
            importance=2,
            status="active",
            content_hash=policy.content_hash(content),
        )
        if old is not None:
            await repo.supersede(conn, old.id, row.id)

    # --- Internals -----------------------------------------------------------------------

    async def _structured_call(
        self,
        principal: Principal,
        *,
        messages: list[Message],
        schema_name: str,
        schema: dict[str, Any],
        feature: Feature,
        request_id: str | None,
        source_id: UUID,
    ) -> ChatResponse:
        """A JSON-schema call on the memory model, or on the default model where the
        organization is not allowed the memory model."""
        ctx = replace(
            AIRequestContext.from_principal(principal, request_id=request_id),
            source_type=feature.value,
            source_id=source_id,
        )
        for model in (self._settings.memory_model or None, None):
            request = ChatRequest(
                model=model,
                messages=messages,
                max_output_tokens=_STRUCTURED_MAX_OUTPUT_TOKENS,
                response_format=JSONSchemaFormat(name=schema_name, json_schema=schema),
                feature=feature,
            )
            try:
                return await self._gateway.chat(request, ctx)
            except AIModelUnavailable:
                if model is None:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _visible(self, principal: Principal) -> Any:
        return repo.visible_to(
            _organization(principal),
            principal.user_id,
            principal.team_ids,
            manage_all=principal.has("memory.manage_all"),
        )

    async def _load(
        self,
        conn: AsyncConnection,
        principal: Principal,
        memory_id: UUID,
        action: str,
        *,
        for_update: bool = False,
    ) -> repo.MemoryRow:
        """The memory, if the caller may see it (404 otherwise, including for administrators
        looking at someone's private memory), with the permission the action needs."""
        row = await repo.get_memory(
            conn, _organization(principal), memory_id, for_update=for_update
        )
        if row is None or not self._can_see(principal, row):
            raise NotFound
        require(principal, f"memory.{action}")
        if action != "view" and not self._can_edit(principal, row):
            raise PermissionDenied
        return row

    @staticmethod
    def _can_see(principal: Principal, row: repo.MemoryRow) -> bool:
        if row.scope in PRIVATE_SCOPES:
            return bool(row.owner_id == principal.user_id)
        if row.scope == "team":
            return row.team_id in principal.team_ids or principal.has("memory.manage_all")
        return True

    @staticmethod
    def _can_edit(principal: Principal, row: repo.MemoryRow) -> bool:
        if row.scope in PRIVATE_SCOPES:
            return bool(row.owner_id == principal.user_id)
        return bool(row.owner_id == principal.user_id) or principal.has("memory.manage_all")

    def _out(self, principal: Principal, row: repo.MemoryRow) -> MemoryOut:
        return MemoryOut.from_row(row, editable=self._can_edit(principal, row))

    @staticmethod
    def _target_of(row: repo.MemoryRow) -> Any:
        return repo.same_target(
            organization_id=row.organization_id,
            scope=row.scope,
            owner_id=row.owner_id,
            team_id=row.team_id,
            conversation_id=row.conversation_id,
        )

    async def _scope_target(
        self, conn: AsyncConnection, principal: Principal, body: CreateMemory
    ) -> tuple[UUID | None, UUID | None]:
        """Check that the caller may write to the requested scope; return its team and
        conversation. Another tenant's or another member's target does not exist (404)."""
        if body.scope == "organization":
            if not principal.has("memory.manage_all"):
                raise PermissionDenied("Organization memories are managed by administrators.")
            return None, None
        if body.scope == "team":
            if body.team_id is None:
                raise MemoryRefused("team_id is required for a team memory.")
            if body.team_id not in principal.team_ids and not principal.has("memory.manage_all"):
                raise NotFound
            return body.team_id, None
        if body.scope == "conversation":
            if body.conversation_id is None:
                raise MemoryRefused("conversation_id is required for a conversation memory.")
            conversation = await chat_repo.get_conversation(
                conn, _organization(principal), body.conversation_id
            )
            if conversation is None or conversation.owner_id != principal.user_id:
                raise NotFound
            return None, body.conversation_id
        return None, body.conversation_id

    def _default_expiry(self, scope: str) -> datetime | None:
        if scope == "conversation":
            return repo.now() + timedelta(days=self._settings.memory_conversation_ttl_days)
        return None

    def _candidate_expiry(self) -> datetime:
        return repo.now() + timedelta(days=self._settings.memory_candidate_ttl_days)


def _organization(principal: Principal) -> UUID:
    if principal.organization_id is None:  # pragma: no cover - CurrentPrincipal guarantees it
        raise NotFound
    return principal.organization_id


def _item(row: repo.MemoryRow, *, score: float) -> MemoryItem:
    return MemoryItem(
        scope=row.scope, kind=row.kind, content=row.content, created_at=row.created_at, score=score
    )


def _settings_out(row: repo.SettingsRow | None) -> MemorySettingsOut:
    if row is None:
        return MemorySettingsOut(extraction_enabled=True, auto_save=True, retention_days=None)
    return MemorySettingsOut(
        extraction_enabled=row.extraction_enabled,
        auto_save=row.auto_save,
        retention_days=row.retention_days,
    )


def _transcript(rows: Any) -> list[tuple[str, str]]:
    """The messages to summarize as (role, text), each capped, newest kept when too long."""
    items = [
        (row.role, row.content[:_SUMMARY_MESSAGE_CHARS])
        for row in rows
        if row.role in ("user", "assistant") and row.content and row.status != "failed"
    ]
    total = 0
    kept: list[tuple[str, str]] = []
    for role, text in reversed(items):
        if total + len(text) > _SUMMARY_TOTAL_CHARS:
            break
        kept.append((role, text))
        total += len(text)
    kept.reverse()
    return kept


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


__all__ = ["IntegrityError", "MemoryEngine", "MemoryRefused"]

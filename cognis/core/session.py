"""Conversation and session lifecycle management."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    SessionModel,
    SessionStatus,
)
from cognis.runtime_context import scoped_runtime_context
from cognis.store import queries

logger = get_logger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
_MAX_INTENTION_LENGTH = 500
_MAX_STATUS_REASON_LENGTH = 500

# Cognis → Intaris session status mapping.  Intaris does not have
# ``failed`` or ``cancelled``; both map to ``terminated`` with a
# machine-readable ``status_reason`` prefix.
_INTARIS_STATUS_MAP: dict[str, str] = {
    "active": "active",
    "idle": "idle",
    "completed": "completed",
    "suspended": "suspended",
    "terminated": "terminated",
    "failed": "terminated",
    "cancelled": "terminated",
}


def _map_cognis_to_intaris_status(
    cognis_status: str,
    *,
    completion_reason: str | None = None,
    result_summary: str | None = None,
    reason: str | None = None,
) -> tuple[str, str | None]:
    """Map a Cognis session status to an Intaris status + status_reason.

    Returns ``(intaris_status, status_reason)``.  The ``status_reason``
    is always bounded to ``_MAX_STATUS_REASON_LENGTH`` characters and
    uses machine-readable prefixes — never raw user/model content.
    """

    intaris_status = _INTARIS_STATUS_MAP.get(cognis_status, cognis_status)

    status_reason: str | None = None
    if cognis_status in ("failed", "cancelled"):
        status_reason = f"source_status={cognis_status}"
    elif cognis_status == "completed" and completion_reason:
        status_reason = f"completion_reason={completion_reason}"
    elif reason:
        status_reason = reason

    if status_reason and len(status_reason) > _MAX_STATUS_REASON_LENGTH:
        status_reason = status_reason[:_MAX_STATUS_REASON_LENGTH]

    return intaris_status, status_reason


def _normalize_intention(value: str | None, fallback: str = "Conversation") -> str:
    """Normalize a session intention to Intaris-safe length and whitespace."""

    collapsed = _WHITESPACE_RE.sub(" ", (value or "").strip())
    resolved = collapsed or fallback
    if len(resolved) <= _MAX_INTENTION_LENGTH:
        return resolved
    return resolved[: _MAX_INTENTION_LENGTH - 3].rstrip() + "..."


class SessionManager:
    """Manage conversation/session metadata and external session correlation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        providers: Any,
        session_cache: Any,
        event_bus: EventBus | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.providers = providers
        self.session_cache = session_cache
        self.event_bus = event_bus

    async def create_conversation(
        self,
        *,
        user_email: str,
        agent_id: str,
        context: ConversationContext,
        title: str | None = None,
        conversation_id: str | None = None,
    ) -> ConversationModel:
        """Create a conversation without creating a root session."""

        async with self.session_factory() as db_session:
            try:
                conversation = await queries.create_conversation(
                    db_session,
                    user_email=user_email,
                    agent_id=agent_id,
                    context_type=context.type,
                    title=title,
                    context_ref=context.ref,
                    context_data=context.platform_data,
                    memory_labels=dict(context.memory_labels),
                    conversation_id=conversation_id,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        return _to_conversation_model(conversation)

    async def create_root_session(
        self,
        *,
        conversation_id: str,
        user_email: str,
        agent_id: str,
        intention: str,
        session_id: str | None = None,
    ) -> SessionModel:
        """Create a root session and corresponding Intaris session."""

        normalized_intention = _normalize_intention(intention)

        logger.info(
            "session: creating root session",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                    "user_email": user_email,
                }
            },
        )
        async with self.session_factory() as db_session:
            try:
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=conversation_id,
                    user_email=user_email,
                    agent_id=agent_id,
                    session_id=session_id,
                )
                with scoped_runtime_context(user_email=user_email, agent_id=agent_id):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=normalized_intention,
                        agent_id=agent_id,
                        user_id=user_email,
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                await queries.update_conversation_active_session(
                    db_session, conversation_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        session_row.intaris_session_id = session_row.session_id
        logger.info(
            "session: root session created",
            extra={
                "extra_data": {
                    "session_id": session_row.session_id,
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                }
            },
        )
        return _to_session_model(session_row)

    async def ensure_root_session(
        self,
        *,
        conversation_id: str,
        user_email: str,
        agent_id: str,
        intention: str,
    ) -> SessionModel:
        """Create exactly one active root session for a conversation."""

        normalized_intention = _normalize_intention(intention)
        async with self.session_factory() as db_session:
            session_row = None
            try:
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=conversation_id,
                    user_email=user_email,
                    agent_id=agent_id,
                )
                with scoped_runtime_context(user_email=user_email, agent_id=agent_id):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=normalized_intention,
                        agent_id=agent_id,
                        user_id=user_email,
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                claimed = await queries.update_conversation_active_session_if_unset(
                    db_session, conversation_id, session_row.session_id
                )
                if not claimed:
                    winner = await queries.get_conversation(db_session, conversation_id)
                    active_session_id = winner.active_session_id if winner is not None else None
                    await db_session.delete(session_row)
                    await db_session.commit()
                    if active_session_id is None:
                        raise RuntimeError("Lost root-session bootstrap race without winner")
                    winner_row = await queries.get_session_row(db_session, active_session_id)
                    if winner_row is None:
                        raise RuntimeError("Active root session missing after bootstrap race")
                    return _to_session_model(winner_row)
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        if session_row is None:
            raise RuntimeError("Root session bootstrap failed")
        session_row.intaris_session_id = session_row.session_id
        return _to_session_model(session_row)

    async def create_conversation_with_root_session(
        self,
        *,
        user_email: str,
        agent_id: str,
        context: ConversationContext,
        title: str | None = None,
        intention: str | None = None,
    ) -> tuple[ConversationModel, SessionModel]:
        """Create a conversation and root session atomically."""

        async with self.session_factory() as db_session:
            try:
                agent = await self._require_agent(db_session, agent_id)
                conversation = await queries.create_conversation(
                    db_session,
                    user_email=user_email,
                    agent_id=agent_id,
                    context_type=context.type,
                    title=title,
                    context_ref=context.ref,
                    context_data=context.platform_data,
                    memory_labels=dict(context.memory_labels),
                )
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=conversation.conversation_id,
                    user_email=user_email,
                    agent_id=agent_id,
                )
                resolved_intention = _normalize_intention(
                    intention or self._build_root_intention(agent, title)
                )
                with scoped_runtime_context(user_email=user_email, agent_id=agent_id):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=resolved_intention,
                        agent_id=agent_id,
                        user_id=user_email,
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                await queries.update_conversation_active_session(
                    db_session, conversation.conversation_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        conversation.active_session_id = session_row.session_id
        session_row.intaris_session_id = session_row.session_id
        return _to_conversation_model(conversation), _to_session_model(session_row)

    async def create_child_session(
        self,
        parent_session: SessionModel,
        *,
        mode: str,
        task_description: str,
        agent_id: str,
        effective_agent_id: str,
        expected_output: str | None = None,
        constraints: dict[str, Any] | None = None,
        intention: str | None = None,
    ) -> SessionModel:
        """Create a delegated child session and corresponding Intaris session."""

        async with self.session_factory() as db_session:
            try:
                child_agent = await self._require_agent(db_session, agent_id)
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=parent_session.conversation_id,
                    user_email=parent_session.user_email,
                    agent_id=agent_id,
                    parent_session_id=parent_session.session_id,
                    delegation_mode=mode,
                    delegation_task=task_description,
                )
                resolved_intention = _normalize_intention(
                    intention or self._build_child_intention(child_agent, task_description)
                )
                details = {
                    "delegated_by_agent": parent_session.agent_id,
                    "effective_agent_id": effective_agent_id,
                    "task_description": task_description,
                    "expected_output": expected_output,
                    "constraints": constraints or {},
                }
                with scoped_runtime_context(
                    user_email=parent_session.user_email,
                    agent_id=parent_session.agent_id,
                ):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=resolved_intention,
                        agent_id=agent_id,
                        user_id=parent_session.user_email,
                        parent_session_id=parent_session.intaris_session_id
                        or parent_session.session_id,
                        details=details,
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        session_row.intaris_session_id = session_row.session_id
        return _to_session_model(session_row)

    async def attach_mnemory_session(self, session_id: str, mnemory_session_id: str) -> bool:
        """Persist the first Mnemory session ID for a Cognis session."""

        if not mnemory_session_id.strip():
            return False
        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_mnemory_session_id(
                    db_session, session_id, mnemory_session_id
                )
                await db_session.commit()
                return updated
            except Exception:
                await db_session.rollback()
                raise

    async def _sync_intaris_status(
        self,
        session_id: str,
        cognis_status: str,
        *,
        user_email: str | None = None,
        completion_reason: str | None = None,
        result_summary: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Best-effort sync of session status to Intaris.

        This is the ONLY place that calls the Intaris status-update
        provider method.  Failures are logged at warning level and
        never raised — Intaris unavailability must not block Cognis
        session transitions.
        """

        intaris_status, status_reason = _map_cognis_to_intaris_status(
            cognis_status,
            completion_reason=completion_reason,
            result_summary=result_summary,
            reason=reason,
        )

        resolved_user_email = user_email
        target_session_id = session_id
        if resolved_user_email is None:
            async with self.session_factory() as db_session:
                session_row = await queries.get_session_row(db_session, session_id)
                if session_row is not None:
                    resolved_user_email = session_row.user_email
                    target_session_id = session_row.intaris_session_id or session_row.session_id

        try:
            await self.providers.guardrails.update_session_status(
                target_session_id,
                intaris_status,
                status_reason,
                user_email=resolved_user_email,
            )
        except Exception:
            logger.warning(
                "session: failed to sync status to Intaris",
                extra={
                    "extra_data": {
                        "session_id": session_id,
                        "cognis_status": cognis_status,
                        "intaris_status": intaris_status,
                    }
                },
                exc_info=True,
            )

    async def mark_idle(self, session_id: str) -> bool:
        """Mark a session idle and evict any warm cache entry."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_idle(db_session, session_id)
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self.session_cache.evict(session_id)
        if updated:
            await self._sync_intaris_status(session_id, "idle")
        return updated

    async def mark_completed(
        self,
        session_id: str,
        result_summary: str | None = None,
        completion_reason: str | None = None,
    ) -> bool:
        """Mark a session completed and evict cache state."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.COMPLETED,
                    completed_at=datetime.now(UTC),
                    result_summary=result_summary,
                    completion_reason=completion_reason,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self.session_cache.evict(session_id)
        if updated:
            await self._sync_intaris_status(
                session_id,
                "completed",
                completion_reason=completion_reason,
                result_summary=result_summary,
            )
        return updated

    async def mark_failed(self, session_id: str, result_summary: str | None = None) -> bool:
        """Mark a session failed and evict cache state."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.FAILED,
                    completed_at=datetime.now(UTC),
                    result_summary=result_summary,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self.session_cache.evict(session_id)
        if updated:
            await self._sync_intaris_status(session_id, "failed")
        return updated

    async def mark_cancelled(self, session_id: str, result_summary: str | None = None) -> bool:
        """Mark a session cancelled and evict cache state."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.CANCELLED,
                    completed_at=datetime.now(UTC),
                    result_summary=result_summary,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self.session_cache.evict(session_id)
        if updated:
            await self._sync_intaris_status(session_id, "cancelled")
        return updated

    async def mark_suspended(self, session_id: str, reason: str | None = None) -> bool:
        """Mark a session suspended (e.g., safety concern from Intaris)."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.SUSPENDED,
                    result_summary=reason,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        if updated:
            await self._sync_intaris_status(session_id, "suspended", reason=reason)
        return updated

    async def mark_terminated(self, session_id: str, reason: str | None = None) -> bool:
        """Mark a session terminated (hard-kill by user or system)."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.TERMINATED,
                    completed_at=datetime.now(UTC),
                    result_summary=reason,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self.session_cache.evict(session_id)
        if updated:
            await self._sync_intaris_status(session_id, "terminated", reason=reason)
        return updated

    async def rotate_session(
        self,
        *,
        conversation_id: str,
        current_session: SessionModel,
        intention: str,
        completion_reason: str = "compacted",
        compaction_summary: str | None = None,
    ) -> SessionModel:
        """Create a new root session, completing the current one.

        This is used for compaction (new clean context window) and for
        explicit session reset. The new session carries forward the
        ``mnemory_session_id`` so memory deduplication continues across
        the rotation boundary.
        """

        logger.info(
            "session: rotating root session",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "old_session_id": current_session.session_id,
                    "completion_reason": completion_reason,
                }
            },
        )

        async with self.session_factory() as db_session:
            try:
                # 1. Mark current session completed
                # NOTE: result_summary is metadata only — no LLM-generated content
                # in the Cognis DB. The actual compaction summary lives in Intaris.
                await queries.set_session_status(
                    db_session,
                    current_session.session_id,
                    SessionStatus.COMPLETED,
                    completed_at=datetime.now(UTC),
                    result_summary=f"Rotated ({completion_reason})",
                    completion_reason=completion_reason,
                )

                # 2. Create new root session (carry mnemory_session_id forward)
                new_session_row = await queries.create_session(
                    db_session,
                    conversation_id=conversation_id,
                    user_email=current_session.user_email,
                    agent_id=current_session.agent_id,
                    previous_session_id=current_session.session_id,
                    mnemory_session_id=current_session.mnemory_session_id,
                )

                # 3. Create Intaris session for the new root
                with scoped_runtime_context(
                    user_email=current_session.user_email,
                    agent_id=current_session.agent_id,
                ):
                    await self.providers.guardrails.create_session(
                        session_id=new_session_row.session_id,
                        intention=_normalize_intention(intention),
                        agent_id=current_session.agent_id,
                        user_id=current_session.user_email,
                    )

                await queries.set_session_intaris_session_id(
                    db_session, new_session_row.session_id, new_session_row.session_id
                )

                # 4. Update conversation root
                await queries.update_conversation_active_session(
                    db_session, conversation_id, new_session_row.session_id
                )

                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        # Evict old session cache
        await self.session_cache.evict(current_session.session_id)

        # Sync old session status to Intaris (best-effort)
        await self._sync_intaris_status(
            current_session.intaris_session_id or current_session.session_id,
            "completed",
            completion_reason=completion_reason,
        )

        new_session_row.intaris_session_id = new_session_row.session_id
        new_session = _to_session_model(new_session_row)

        logger.info(
            "session: rotation completed",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "old_session_id": current_session.session_id,
                    "new_session_id": new_session.session_id,
                }
            },
        )
        return new_session

    async def recover_stale_sessions(self, stale_after_seconds: int = 300) -> list[str]:
        """Mark stale active sessions idle on controller startup."""

        updated_before = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        recovered_ids: list[str] = []
        async with self.session_factory() as db_session:
            try:
                stale_sessions = await queries.list_stale_active_sessions(
                    db_session, updated_before
                )
                for stale_session in stale_sessions:
                    if stale_session.parent_session_id is not None:
                        continue
                    if stale_session.session_id in recovered_ids:
                        continue
                    await queries.set_session_idle(
                        db_session,
                        stale_session.session_id,
                        idle_since=datetime.now(UTC),
                    )
                    recovered_ids.append(stale_session.session_id)
                    child_ids = await self._fail_active_descendants(
                        db_session,
                        parent_session_id=stale_session.session_id,
                        completed_at=datetime.now(UTC),
                    )
                    recovered_ids.extend(child_ids)
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        for recovered_id in recovered_ids:
            await self.session_cache.evict(recovered_id)
        # Best-effort sync to Intaris for all recovered sessions (after commit)
        for stale_session in stale_sessions:
            if stale_session.session_id not in recovered_ids:
                continue
            if stale_session.parent_session_id is None:
                await self._sync_intaris_status(
                    stale_session.intaris_session_id or stale_session.session_id,
                    "idle",
                )
            else:
                await self._sync_intaris_status(
                    stale_session.intaris_session_id or stale_session.session_id,
                    "failed",
                )
        if recovered_ids:
            logger.info(
                "Recovered stale sessions",
                extra={"extra_data": {"recovered_count": len(recovered_ids)}},
            )
            if self.event_bus is not None:
                for recovered_id in recovered_ids:
                    await self.event_bus.publish(
                        Event(
                            type=EventType.SESSION_RECOVERED,
                            data={"session_id": recovered_id},
                        )
                    )
        return recovered_ids

    async def archive_conversation(self, conversation_id: str) -> bool:
        """Archive a conversation and complete its sessions."""

        return await self._close_conversation(conversation_id, conversation_status="archived")

    async def soft_delete_conversation(self, conversation_id: str) -> bool:
        """Soft-delete a conversation and complete its sessions."""

        return await self._close_conversation(conversation_id, conversation_status="deleted")

    async def purge_conversation(self, conversation_id: str) -> bool:
        """Hard-delete Cognis metadata for a conversation.

        Note: Intaris event-store purge is intentionally deferred until a verified
        delete-session provider contract exists.
        """

        async with self.session_factory() as db_session:
            try:
                sessions = await queries.list_conversation_sessions(db_session, conversation_id)
                deleted_sessions = await queries.delete_sessions_for_conversation(
                    db_session, conversation_id
                )
                deleted_conversations = await queries.delete_conversation(
                    db_session, conversation_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        if deleted_sessions or deleted_conversations:
            for session_row in sessions:
                await self.session_cache.evict(session_row.session_id)
        return deleted_conversations > 0

    async def _close_conversation(self, conversation_id: str, conversation_status: str) -> bool:
        async with self.session_factory() as db_session:
            try:
                conversation = await queries.get_conversation(db_session, conversation_id)
                if conversation is None:
                    return False
                sessions = await queries.list_conversation_sessions(db_session, conversation_id)
                await queries.set_conversation_status(
                    db_session, conversation_id, conversation_status
                )
                for session_row in sessions:
                    if session_row.status in {
                        SessionStatus.COMPLETED,
                        SessionStatus.FAILED,
                        SessionStatus.CANCELLED,
                        SessionStatus.TERMINATED,
                    }:
                        continue
                    await queries.set_session_status(
                        db_session,
                        session_row.session_id,
                        SessionStatus.COMPLETED,
                        completed_at=datetime.now(UTC),
                        result_summary=f"conversation {conversation_status}",
                    )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        for session_row in sessions:
            await self.session_cache.evict(session_row.session_id)
            if session_row.status not in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
                SessionStatus.TERMINATED,
            }:
                await self._sync_intaris_status(
                    session_row.intaris_session_id or session_row.session_id,
                    "completed",
                    completion_reason=f"conversation_{conversation_status}",
                )
        return True

    async def _require_agent(self, db_session: AsyncSession, agent_id: str) -> AgentDefinition:
        # System agents are Python constants, not in DB
        from cognis.core.agent_registry import SYSTEM_AGENTS

        if agent_id in SYSTEM_AGENTS:
            return SYSTEM_AGENTS[agent_id]

        agent_row = await queries.get_agent(db_session, agent_id)
        if agent_row is None:
            raise ValueError(f"Unknown agent: {agent_id}")
        return AgentDefinition(
            agent_id=agent_row.agent_id,
            owner_email=agent_row.owner_email,
            name=agent_row.name,
            display_name=agent_row.display_name,
            description=agent_row.description,
            system_prompt=agent_row.system_prompt,
            personality=agent_row.personality,
            skills=agent_row.skills,
            tools=agent_row.tools,
            permissions=agent_row.permissions,
            llm_config=agent_row.llm_config,
            execution=agent_row.execution,
            avatar_url=agent_row.avatar_url,
            avatar_image_id=getattr(agent_row, "avatar_image_id", None),
            status=agent_row.status,
            created_at=agent_row.created_at,
            updated_at=agent_row.updated_at,
        )

    def _build_root_intention(self, agent: AgentDefinition, title: str | None) -> str:
        if title:
            return title
        return f"Conversation with {agent.name}"

    def _build_child_intention(self, agent: AgentDefinition, task_description: str) -> str:
        description_prefix = f"{agent.name}: " if agent.name else ""
        return f"{description_prefix}{task_description}".strip()

    async def _fail_active_descendants(
        self,
        db_session: AsyncSession,
        *,
        parent_session_id: str,
        completed_at: datetime,
    ) -> list[str]:
        recovered_ids: list[str] = []
        child_sessions = await queries.list_child_sessions(db_session, parent_session_id)
        for child_session in child_sessions:
            if child_session.status == "active":
                await queries.set_session_status(
                    db_session,
                    child_session.session_id,
                    "failed",
                    completed_at=completed_at,
                    result_summary="controller restart; parent recovered",
                )
                recovered_ids.append(child_session.session_id)
            recovered_ids.extend(
                await self._fail_active_descendants(
                    db_session,
                    parent_session_id=child_session.session_id,
                    completed_at=completed_at,
                )
            )
        return recovered_ids


def _to_conversation_model(row: Any) -> ConversationModel:
    return ConversationModel(
        conversation_id=row.conversation_id,
        user_email=row.user_email,
        agent_id=row.agent_id,
        title=row.title,
        context=ConversationContext(
            type=row.context_type,
            ref=row.context_ref,
            platform_data=row.context_data or {},
            memory_labels=row.memory_labels or {},
        ),
        active_session_id=row.active_session_id,
        status=row.status,
        last_message_at=row.last_message_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_session_model(row: Any) -> SessionModel:
    return SessionModel(
        session_id=row.session_id,
        conversation_id=row.conversation_id,
        parent_session_id=row.parent_session_id,
        previous_session_id=getattr(row, "previous_session_id", None),
        user_email=row.user_email,
        agent_id=row.agent_id,
        delegation_mode=row.delegation_mode,
        delegation_task=row.delegation_task,
        status=row.status,
        completion_reason=getattr(row, "completion_reason", None),
        intaris_session_id=row.intaris_session_id,
        mnemory_session_id=row.mnemory_session_id,
        started_at=row.started_at,
        idle_since=row.idle_since,
        completed_at=row.completed_at,
        result_summary=row.result_summary,
        updated_at=row.updated_at,
    )

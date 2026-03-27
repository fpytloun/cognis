"""Conversation and session lifecycle management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext, ConversationModel, SessionModel
from cognis.runtime_context import scoped_runtime_context
from cognis.store import queries

logger = get_logger(__name__)


class SessionManager:
    """Manage conversation/session metadata and external session correlation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        providers: Any,
        session_cache: Any,
    ) -> None:
        self.session_factory = session_factory
        self.providers = providers
        self.session_cache = session_cache

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
                        intention=intention,
                        agent_id=agent_id,
                        user_id=user_email,
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                await queries.update_conversation_root_session(
                    db_session, conversation_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
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
                resolved_intention = intention or self._build_root_intention(agent, title)
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
                await queries.update_conversation_root_session(
                    db_session, conversation.conversation_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        conversation.root_session_id = session_row.session_id
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
                resolved_intention = intention or self._build_child_intention(
                    child_agent, task_description
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
        return updated

    async def mark_completed(self, session_id: str, result_summary: str | None = None) -> bool:
        """Mark a session completed and evict cache state."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    "completed",
                    completed_at=datetime.now(UTC),
                    result_summary=result_summary,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self.session_cache.evict(session_id)
        return updated

    async def mark_failed(self, session_id: str, result_summary: str | None = None) -> bool:
        """Mark a session failed and evict cache state."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    "failed",
                    completed_at=datetime.now(UTC),
                    result_summary=result_summary,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self.session_cache.evict(session_id)
        return updated

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
        if recovered_ids:
            logger.info(
                "Recovered stale sessions",
                extra={"extra_data": {"recovered_count": len(recovered_ids)}},
            )
            # TODO: emit SESSION_RECOVERED event when the internal event bus exists.
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
                    if session_row.status in {"completed", "failed", "cancelled"}:
                        continue
                    await queries.set_session_status(
                        db_session,
                        session_row.session_id,
                        "completed",
                        completed_at=datetime.now(UTC),
                        result_summary=f"conversation {conversation_status}",
                    )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        for session_row in sessions:
            await self.session_cache.evict(session_row.session_id)
        return True

    async def _require_agent(self, db_session: AsyncSession, agent_id: str) -> AgentDefinition:
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
            status=agent_row.status,
            created_at=agent_row.created_at,
            updated_at=agent_row.updated_at,
        )

    def _build_root_intention(self, agent: AgentDefinition, title: str | None) -> str:
        if title:
            return title
        if agent.description:
            return agent.description
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
        root_session_id=row.root_session_id,
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
        user_email=row.user_email,
        agent_id=row.agent_id,
        delegation_mode=row.delegation_mode,
        delegation_task=row.delegation_task,
        status=row.status,
        intaris_session_id=row.intaris_session_id,
        mnemory_session_id=row.mnemory_session_id,
        started_at=row.started_at,
        idle_since=row.idle_since,
        completed_at=row.completed_at,
        result_summary=row.result_summary,
        updated_at=row.updated_at,
    )

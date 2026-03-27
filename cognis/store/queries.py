"""Reusable async query helpers for Cognis DB."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import (
    Agent,
    ApiKey,
    Conversation,
    LLMProvider,
    Secret,
    Session,
    Setting,
    User,
)

# --- Users ---


async def get_user(session: AsyncSession, email: str) -> User | None:
    """Get a user by email."""
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    email: str,
    name: str | None,
    password_hash: str,
    role: str = "user",
) -> User:
    """Create a new user."""
    user = User(email=email, name=name, password_hash=password_hash, role=role)
    session.add(user)
    await session.flush()
    return user


async def count_users(session: AsyncSession) -> int:
    """Count total users."""
    result = await session.execute(select(User))
    return len(result.scalars().all())


async def update_user_password(session: AsyncSession, email: str, password_hash: str) -> bool:
    """Update a user's password hash. Returns True if user found."""
    user = await get_user(session, email)
    if user is None:
        return False
    user.password_hash = password_hash
    await session.flush()
    return True


# --- API Keys ---


async def get_api_key(session: AsyncSession, key_id: str) -> ApiKey | None:
    """Get an API key by key_id."""
    result = await session.execute(select(ApiKey).where(ApiKey.key_id == key_id))
    return result.scalar_one_or_none()


async def create_api_key(
    session: AsyncSession,
    user_email: str,
    key_hash: str,
    name: str,
    scopes: dict[str, object] | None = None,
    key_id: str | None = None,
) -> ApiKey:
    """Create a new API key record."""
    key_id = key_id or f"ck{uuid.uuid4().hex[:16]}"
    api_key = ApiKey(
        key_id=key_id,
        user_email=user_email,
        key_hash=key_hash,
        name=name,
        scopes=scopes,
    )
    session.add(api_key)
    await session.flush()
    return api_key


async def list_api_keys(session: AsyncSession, user_email: str) -> list[ApiKey]:
    """List API keys for a user (metadata only)."""
    result = await session.execute(select(ApiKey).where(ApiKey.user_email == user_email))
    return list(result.scalars().all())


# --- Settings ---


async def get_setting(session: AsyncSession, key: str) -> Setting | None:
    """Get a setting by key."""
    result = await session.execute(select(Setting).where(Setting.key == key))
    return result.scalar_one_or_none()


async def get_setting_value(session: AsyncSession, key: str, default: object = None) -> object:
    """Get a setting's value, returning default if not found."""
    setting = await get_setting(session, key)
    if setting is None:
        return default
    return setting.value


async def upsert_setting(
    session: AsyncSession,
    key: str,
    value: object,
    category: str,
    updated_by: str | None = None,
) -> Setting:
    """Create or update a setting."""
    existing = await get_setting(session, key)
    if existing is not None:
        existing.value = value
        existing.updated_by = updated_by
        existing.updated_at = datetime.now(UTC)
        await session.flush()
        return existing
    setting = Setting(key=key, value=value, category=category, updated_by=updated_by)
    session.add(setting)
    await session.flush()
    return setting


async def list_settings(session: AsyncSession) -> list[Setting]:
    """List all settings."""
    result = await session.execute(select(Setting))
    return list(result.scalars().all())


# --- LLM Providers ---


async def list_llm_providers(session: AsyncSession) -> list[LLMProvider]:
    """List all LLM provider configurations."""
    result = await session.execute(select(LLMProvider).where(LLMProvider.status == "active"))
    return list(result.scalars().all())


# --- Agents ---


async def list_active_agents_summary(
    session: AsyncSession, owner_email: str
) -> list[dict[str, str | None]]:
    """List safe agent metadata for tool responses."""

    query = (
        select(Agent.agent_id, Agent.name, Agent.description, Agent.status)
        .where(Agent.status == "active")
        .where(Agent.owner_email == owner_email)
        .order_by(Agent.agent_id)
    )
    result = await session.execute(query)
    return [
        {
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "status": status,
        }
        for agent_id, name, description, status in result.all()
    ]


async def get_agent(session: AsyncSession, agent_id: str) -> Agent | None:
    """Get an agent by ID."""

    result = await session.execute(select(Agent).where(Agent.agent_id == agent_id))
    return result.scalar_one_or_none()


async def get_llm_provider(session: AsyncSession, provider_id: str) -> LLMProvider | None:
    """Get an LLM provider by ID."""
    result = await session.execute(
        select(LLMProvider).where(LLMProvider.provider_id == provider_id)
    )
    return result.scalar_one_or_none()


# --- Conversations ---


async def create_conversation(
    session: AsyncSession,
    user_email: str,
    agent_id: str,
    context_type: str,
    *,
    title: str | None = None,
    context_ref: str | None = None,
    context_data: dict[str, object] | None = None,
    memory_labels: dict[str, object] | None = None,
    conversation_id: str | None = None,
) -> Conversation:
    """Create a new conversation row."""

    conversation = Conversation(
        conversation_id=conversation_id or f"conv_{uuid.uuid4().hex}",
        user_email=user_email,
        agent_id=agent_id,
        title=title,
        context_type=context_type,
        context_ref=context_ref,
        context_data=context_data,
        memory_labels=memory_labels,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def get_conversation(session: AsyncSession, conversation_id: str) -> Conversation | None:
    """Get a conversation by ID."""

    result = await session.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    return result.scalar_one_or_none()


async def update_conversation_root_session(
    session: AsyncSession, conversation_id: str, root_session_id: str
) -> bool:
    """Set the root session ID for a conversation."""

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    conversation.root_session_id = root_session_id
    conversation.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def touch_conversation(
    session: AsyncSession, conversation_id: str, when: datetime | None = None
) -> bool:
    """Update conversation timestamps after activity."""

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    timestamp = when or datetime.now(UTC)
    conversation.last_message_at = timestamp
    conversation.updated_at = timestamp
    await session.flush()
    return True


async def set_conversation_status(session: AsyncSession, conversation_id: str, status: str) -> bool:
    """Set conversation lifecycle status."""

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    conversation.status = status
    conversation.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def list_conversation_sessions(session: AsyncSession, conversation_id: str) -> list[Session]:
    """List all sessions for a conversation."""

    result = await session.execute(
        select(Session)
        .where(Session.conversation_id == conversation_id)
        .order_by(Session.started_at, Session.session_id)
    )
    return list(result.scalars().all())


async def delete_conversation(session: AsyncSession, conversation_id: str) -> int:
    """Hard-delete a conversation row."""

    result = await session.execute(
        delete(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


# --- Sessions ---


async def create_session(
    session: AsyncSession,
    conversation_id: str,
    user_email: str,
    agent_id: str,
    *,
    parent_session_id: str | None = None,
    delegation_mode: str | None = None,
    delegation_task: str | None = None,
    status: str = "active",
    intaris_session_id: str | None = None,
    mnemory_session_id: str | None = None,
    session_id: str | None = None,
) -> Session:
    """Create a new session row."""

    session_row = Session(
        session_id=session_id or f"sess_{uuid.uuid4().hex}",
        conversation_id=conversation_id,
        parent_session_id=parent_session_id,
        user_email=user_email,
        agent_id=agent_id,
        delegation_mode=delegation_mode,
        delegation_task=delegation_task,
        status=status,
        intaris_session_id=intaris_session_id,
        mnemory_session_id=mnemory_session_id,
    )
    session.add(session_row)
    await session.flush()
    return session_row


async def get_session_row(session: AsyncSession, session_id: str) -> Session | None:
    """Get a session row by ID."""

    result = await session.execute(select(Session).where(Session.session_id == session_id))
    return result.scalar_one_or_none()


async def set_session_intaris_session_id(
    session: AsyncSession, session_id: str, intaris_session_id: str
) -> bool:
    """Persist Intaris correlation ID for a session."""

    session_row = await get_session_row(session, session_id)
    if session_row is None:
        return False
    session_row.intaris_session_id = intaris_session_id
    session_row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def set_session_mnemory_session_id(
    session: AsyncSession, session_id: str, mnemory_session_id: str
) -> bool:
    """Persist Mnemory correlation ID for a session."""

    session_row = await get_session_row(session, session_id)
    if session_row is None:
        return False
    session_row.mnemory_session_id = mnemory_session_id
    session_row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def set_session_status(
    session: AsyncSession,
    session_id: str,
    status: str,
    *,
    idle_since: datetime | None = None,
    completed_at: datetime | None = None,
    result_summary: str | None = None,
) -> bool:
    """Update session lifecycle state and timestamps."""

    session_row = await get_session_row(session, session_id)
    if session_row is None:
        return False
    session_row.status = status
    session_row.idle_since = idle_since
    session_row.completed_at = completed_at
    if result_summary is not None:
        session_row.result_summary = result_summary
    session_row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def set_session_idle(
    session: AsyncSession, session_id: str, idle_since: datetime | None = None
) -> bool:
    """Mark a session idle."""

    return await set_session_status(
        session,
        session_id,
        "idle",
        idle_since=idle_since or datetime.now(UTC),
    )


async def list_child_sessions(session: AsyncSession, parent_session_id: str) -> list[Session]:
    """List all direct child sessions for a parent session."""

    result = await session.execute(
        select(Session)
        .where(Session.parent_session_id == parent_session_id)
        .order_by(Session.started_at, Session.session_id)
    )
    return list(result.scalars().all())


async def list_stale_active_sessions(
    session: AsyncSession, updated_before: datetime
) -> list[Session]:
    """List active sessions that have not been updated recently."""

    result = await session.execute(
        select(Session)
        .where(Session.status == "active")
        .where(Session.updated_at < updated_before)
        .order_by(Session.updated_at, Session.session_id)
    )
    return list(result.scalars().all())


async def delete_sessions_for_conversation(session: AsyncSession, conversation_id: str) -> int:
    """Hard-delete all sessions for a conversation."""

    result = await session.execute(
        delete(Session).where(Session.conversation_id == conversation_id)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


# --- Secrets ---


async def get_secret(
    session: AsyncSession,
    user_email: str,
    name: str,
    scope: str = "user",
    agent_id: str | None = None,
) -> Secret | None:
    """Get a secret by user, name, scope, and optional agent_id."""
    query = select(Secret).where(
        Secret.user_email == user_email,
        Secret.name == name,
        Secret.scope == scope,
    )
    if agent_id is not None:
        query = query.where(Secret.agent_id == agent_id)
    else:
        query = query.where(Secret.agent_id.is_(None))
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def list_secrets(session: AsyncSession, user_email: str) -> list[Secret]:
    """List secrets for a user (metadata only, no decrypted values)."""
    result = await session.execute(select(Secret).where(Secret.user_email == user_email))
    return list(result.scalars().all())

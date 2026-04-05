"""Reusable async query helpers for Cognis DB."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import (
    Agent,
    AgentSecondaryBinding,
    ApiKey,
    ArtifactRecordRow,
    ChannelAccountRow,
    ChannelContact,
    ChannelPairingRequest,
    Conversation,
    ExecutorRow,
    LLMProvider,
    MCPServerRow,
    ModelRouting,
    Schedule,
    Secret,
    Session,
    Setting,
    SkillRow,
    StepRun,
    Task,
    TaskDependency,
    User,
    WorkflowRow,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
    """Count total users (excluding system users)."""
    result = await session.execute(select(User).where(User.role != "system"))
    return len(result.scalars().all())


async def update_user_password(session: AsyncSession, email: str, password_hash: str) -> bool:
    """Update a user's password hash. Returns True if user found."""
    user = await get_user(session, email)
    if user is None:
        return False
    user.password_hash = password_hash
    await session.flush()
    return True


async def list_users(
    session: AsyncSession,
    *,
    include_disabled: bool = False,
    limit: int = 100,
) -> list[User]:
    """List all users, optionally including disabled ones. Excludes system users."""
    query = (
        select(User)
        .where(User.role != "system")
        .order_by(User.created_at.desc(), User.email.asc())
        .limit(limit)
    )
    if not include_disabled:
        query = query.where(User.is_active.is_(True))
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_user(
    session: AsyncSession,
    email: str,
    *,
    name: str | None = None,
    role: str | None = None,
) -> User | None:
    """Update mutable user fields. Returns updated user or None if not found."""
    user = await get_user(session, email)
    if user is None:
        return None
    if name is not None:
        user.name = name
    if role is not None:
        user.role = role
    user.updated_at = _utcnow()
    await session.flush()
    return user


async def disable_user(
    session: AsyncSession,
    email: str,
    disabled_by: str,
) -> User | None:
    """Disable a user (soft delete). Returns updated user or None."""
    user = await get_user(session, email)
    if user is None:
        return None
    user.is_active = False
    user.disabled_at = _utcnow()
    user.disabled_by = disabled_by
    user.updated_at = _utcnow()
    await session.flush()
    return user


async def enable_user(session: AsyncSession, email: str) -> User | None:
    """Re-enable a disabled user. Returns updated user or None."""
    user = await get_user(session, email)
    if user is None:
        return None
    user.is_active = True
    user.disabled_at = None
    user.disabled_by = None
    user.updated_at = _utcnow()
    await session.flush()
    return user


async def update_user_last_login(session: AsyncSession, email: str) -> None:
    """Update the last_login_at timestamp for a user."""
    user = await get_user(session, email)
    if user is not None:
        user.last_login_at = _utcnow()
        await session.flush()


async def delete_user_cascade(session: AsyncSession, email: str) -> bool:
    """Hard-delete a user and cascade to all owned resources in Cognis DB.

    Deletes: API keys, conversations (+ sessions), agents (+ bindings),
    tasks (+ dependencies + step runs), workflows, schedules, secrets,
    executors, skills, settings updated_by references, audit log entries.
    Returns True if user existed and was deleted.
    """
    user = await get_user(session, email)
    if user is None:
        return False

    # Delete in dependency order (children before parents)
    # Step runs reference tasks
    await session.execute(
        delete(StepRun).where(
            StepRun.task_id.in_(select(Task.task_id).where(Task.created_by == email))
        )
    )
    # Task dependencies reference tasks
    await session.execute(
        delete(TaskDependency).where(
            TaskDependency.task_id.in_(select(Task.task_id).where(Task.created_by == email))
        )
    )
    await session.execute(
        delete(TaskDependency).where(
            TaskDependency.depends_on.in_(select(Task.task_id).where(Task.created_by == email))
        )
    )
    # Sessions reference conversations
    await session.execute(
        delete(Session).where(
            Session.conversation_id.in_(
                select(Conversation.conversation_id).where(Conversation.user_email == email)
            )
        )
    )
    # Agent secondary bindings reference agents
    await session.execute(
        delete(AgentSecondaryBinding).where(
            AgentSecondaryBinding.primary_agent_id.in_(
                select(Agent.agent_id).where(Agent.owner_email == email)
            )
        )
    )
    await session.execute(
        delete(AgentSecondaryBinding).where(
            AgentSecondaryBinding.secondary_agent_id.in_(
                select(Agent.agent_id).where(Agent.owner_email == email)
            )
        )
    )
    # Schedules reference agents owned by user
    await session.execute(delete(Schedule).where(Schedule.created_by == email))
    # Tasks reference agents and users
    await session.execute(delete(Task).where(Task.created_by == email))
    # Now delete the main tables
    await session.execute(delete(Conversation).where(Conversation.user_email == email))
    await session.execute(delete(Agent).where(Agent.owner_email == email))
    await session.execute(delete(ApiKey).where(ApiKey.user_email == email))
    await session.execute(delete(Secret).where(Secret.user_email == email))
    await session.execute(delete(WorkflowRow).where(WorkflowRow.owner_email == email))
    await session.execute(delete(ExecutorRow).where(ExecutorRow.owner_email == email))
    await session.execute(delete(MCPServerRow).where(MCPServerRow.owner_email == email))
    await session.execute(delete(SkillRow).where(SkillRow.owner_email == email))
    # Nullify settings updated_by references
    await session.execute(
        update(Setting).where(Setting.updated_by == email).values(updated_by=None)
    )
    # Delete the user
    await session.execute(delete(User).where(User.email == email))
    await session.flush()
    return True


async def count_admins(session: AsyncSession) -> int:
    """Count active admin users."""
    result = await session.execute(
        select(sa.func.count())
        .select_from(User)
        .where(User.role == "admin", User.is_active.is_(True))
    )
    return result.scalar_one()


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
    result = await session.execute(
        select(ApiKey)
        .where(ApiKey.user_email == user_email)
        .order_by(ApiKey.created_at.desc(), ApiKey.key_id.asc())
    )
    return list(result.scalars().all())


async def delete_api_key(session: AsyncSession, key_id: str, user_email: str) -> bool:
    """Delete one API key belonging to a user."""

    result = await session.execute(
        delete(ApiKey).where(ApiKey.key_id == key_id, ApiKey.user_email == user_email)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def touch_api_key_last_used(
    session: AsyncSession, key_id: str, when: datetime | None = None
) -> bool:
    """Update the last-used timestamp for an API key."""

    record = await get_api_key(session, key_id)
    if record is None:
        return False
    record.last_used_at = when or _utcnow()
    await session.flush()
    return True


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


async def create_llm_provider(
    session: AsyncSession,
    *,
    provider_id: str,
    display_name: str,
    location: str,
    backend: str,
    config: dict[str, Any],
    status: str = "active",
) -> LLMProvider:
    """Create a new LLM provider row."""
    row = LLMProvider(
        provider_id=provider_id,
        display_name=display_name,
        location=location,
        backend=backend,
        config=config,
        status=status,
    )
    session.add(row)
    await session.flush()
    return row


async def update_llm_provider(
    session: AsyncSession,
    provider_id: str,
    *,
    display_name: str | None = None,
    location: str | None = None,
    backend: str | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
) -> bool:
    """Update an LLM provider row."""
    row = await get_llm_provider(session, provider_id)
    if row is None:
        return False
    if display_name is not None:
        row.display_name = display_name
    if location is not None:
        row.location = location
    if backend is not None:
        row.backend = backend
    if config is not None:
        row.config = config
    if status is not None:
        row.status = status
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def delete_llm_provider(session: AsyncSession, provider_id: str) -> bool:
    """Delete an LLM provider row."""
    result = await session.execute(
        delete(LLMProvider).where(LLMProvider.provider_id == provider_id)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


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


async def list_agents(session: AsyncSession, owner_email: str | None = None) -> list[Agent]:
    """List agents, optionally filtered by owner."""
    query = select(Agent).order_by(Agent.updated_at.desc(), Agent.agent_id.asc())
    if owner_email is not None:
        query = query.where(Agent.owner_email == owner_email)
    result = await session.execute(query)
    return list(result.scalars().all())


async def create_agent(
    session: AsyncSession,
    *,
    agent_id: str,
    owner_email: str,
    name: str,
    display_name: str | None = None,
    description: str | None = None,
    system_prompt: str | None = None,
    personality: dict[str, Any] | None = None,
    skills: dict[str, Any] | None = None,
    tools: dict[str, Any] | None = None,
    permissions: dict[str, Any] | None = None,
    llm_config: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    avatar_url: str | None = None,
    avatar_image_id: str | None = None,
    agent_type: str = "primary",
    status: str = "draft",
) -> Agent:
    """Create an agent row."""
    row = Agent(
        agent_id=agent_id,
        owner_email=owner_email,
        name=name,
        display_name=display_name,
        description=description,
        system_prompt=system_prompt,
        personality=personality,
        skills=skills,
        tools=tools,
        permissions=permissions,
        llm_config=llm_config,
        execution=execution,
        avatar_url=avatar_url,
        avatar_image_id=avatar_image_id,
        agent_type=agent_type,
        status=status,
    )
    session.add(row)
    await session.flush()
    return row


async def update_agent(
    session: AsyncSession,
    agent_id: str,
    *,
    updates: dict[str, Any],
) -> bool:
    """Update mutable agent fields."""
    row = await get_agent(session, agent_id)
    if row is None:
        return False
    nullable_fields = {
        "display_name",
        "description",
        "system_prompt",
        "personality",
        "skills",
        "tools",
        "permissions",
        "llm_config",
        "execution",
        "avatar_image_id",
    }
    for field_name, value in updates.items():
        if hasattr(row, field_name) and (value is not None or field_name in nullable_fields):
            setattr(row, field_name, value)
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def set_agent_status(session: AsyncSession, agent_id: str, status: str) -> bool:
    """Update an agent's lifecycle status."""
    row = await get_agent(session, agent_id)
    if row is None:
        return False
    row.status = status
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def list_secondary_bindings(session: AsyncSession, primary_agent_id: str) -> list[str]:
    """List secondary agent IDs bound to a primary agent."""
    result = await session.execute(
        select(AgentSecondaryBinding.secondary_agent_id).where(
            AgentSecondaryBinding.primary_agent_id == primary_agent_id
        )
    )
    return [row[0] for row in result.all()]


async def set_secondary_bindings(
    session: AsyncSession, primary_agent_id: str, secondary_agent_ids: list[str]
) -> None:
    """Replace all secondary agent bindings for a primary agent."""
    from sqlalchemy import delete

    await session.execute(
        delete(AgentSecondaryBinding).where(
            AgentSecondaryBinding.primary_agent_id == primary_agent_id
        )
    )
    for secondary_id in secondary_agent_ids:
        session.add(
            AgentSecondaryBinding(
                primary_agent_id=primary_agent_id,
                secondary_agent_id=secondary_id,
            )
        )
    await session.flush()


async def add_secondary_binding(
    session: AsyncSession, primary_agent_id: str, secondary_agent_id: str
) -> bool:
    """Add a single secondary agent binding. Returns False if already exists."""
    existing = await session.execute(
        select(AgentSecondaryBinding).where(
            AgentSecondaryBinding.primary_agent_id == primary_agent_id,
            AgentSecondaryBinding.secondary_agent_id == secondary_agent_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return False
    session.add(
        AgentSecondaryBinding(
            primary_agent_id=primary_agent_id,
            secondary_agent_id=secondary_agent_id,
        )
    )
    await session.flush()
    return True


async def remove_secondary_binding(
    session: AsyncSession, primary_agent_id: str, secondary_agent_id: str
) -> bool:
    """Remove a single secondary agent binding. Returns False if not found."""
    from sqlalchemy import delete

    result = await session.execute(
        delete(AgentSecondaryBinding).where(
            AgentSecondaryBinding.primary_agent_id == primary_agent_id,
            AgentSecondaryBinding.secondary_agent_id == secondary_agent_id,
        )
    )
    return result.rowcount > 0  # type: ignore[union-attr]


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


async def list_conversations(
    session: AsyncSession,
    user_email: str,
    *,
    context_type: str | None = None,
    agent_id: str | None = None,
) -> list[Conversation]:
    """List conversations for a user, optionally filtered by context type and agent."""
    query = (
        select(Conversation)
        .where(Conversation.user_email == user_email)
        .order_by(Conversation.updated_at.desc(), Conversation.conversation_id.asc())
    )
    if context_type is not None:
        query = query.where(Conversation.context_type == context_type)
    if agent_id is not None:
        query = query.where(Conversation.agent_id == agent_id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_latest_active_conversation_for_agent(
    session: AsyncSession,
    user_email: str,
    agent_id: str,
    *,
    context_type: str | None = None,
) -> Conversation | None:
    """Return the most recent active conversation for one user/agent pair.

    When *context_type* is provided the query is further narrowed to
    conversations with a matching ``context_type`` column.
    """

    query = (
        select(Conversation)
        .where(Conversation.user_email == user_email)
        .where(Conversation.agent_id == agent_id)
        .where(Conversation.status == "active")
    )
    if context_type is not None:
        query = query.where(Conversation.context_type == context_type)
    query = query.order_by(
        Conversation.last_message_at.desc().nullslast(),
        Conversation.updated_at.desc(),
        Conversation.conversation_id.asc(),
    ).limit(1)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def update_conversation(
    session: AsyncSession,
    conversation_id: str,
    *,
    title: str | None = None,
) -> bool:
    """Update mutable conversation fields."""
    row = await get_conversation(session, conversation_id)
    if row is None:
        return False
    if title is not None:
        row.title = title
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def mark_conversation_read(session: AsyncSession, conversation_id: str) -> bool:
    """Set last_read_at to now for unread tracking."""
    row = await get_conversation(session, conversation_id)
    if row is None:
        return False
    row.last_read_at = datetime.now(UTC)
    await session.flush()
    return True


async def update_conversation_active_session(
    session: AsyncSession, conversation_id: str, active_session_id: str
) -> bool:
    """Set the active session ID for a conversation."""

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    conversation.active_session_id = active_session_id
    conversation.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def update_conversation_active_session_if_unset(
    session: AsyncSession,
    conversation_id: str,
    active_session_id: str,
) -> bool:
    """Set the active session only when it is currently unset."""

    result = await session.execute(
        update(Conversation)
        .where(
            Conversation.conversation_id == conversation_id,
            Conversation.active_session_id.is_(None),
        )
        .values(
            active_session_id=active_session_id,
            updated_at=datetime.now(UTC),
        )
    )
    return bool(result.rowcount)


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
    previous_session_id: str | None = None,
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
        previous_session_id=previous_session_id,
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


async def get_latest_active_session_for_conversation(
    session: AsyncSession,
    conversation_id: str,
) -> Session | None:
    """Get the most recent active/idle session for a conversation.

    Used for task result delivery as a fallback when the conversation's
    ``active_session_id`` field is not yet populated.
    """
    result = await session.execute(
        select(Session)
        .where(
            Session.conversation_id == conversation_id,
            Session.status.in_(["active", "idle"]),
        )
        .order_by(Session.started_at.desc(), Session.session_id.desc())
        .limit(1)
    )
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
    completion_reason: str | None = None,
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
    if completion_reason is not None:
        session_row.completion_reason = completion_reason
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


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


async def create_task(
    session: AsyncSession,
    *,
    created_by: str,
    agent_id: str,
    title: str,
    description: str = "",
    expected_output: str | None = None,
    status: str = "draft",
    priority: int = 0,
    source_type: str = "api",
    source_ref: str | None = None,
    delivery_mode: str = "same_conversation",
    delivery_target: str | None = None,
    workflow_id: str | None = None,
    workflow_state: dict[str, object] | None = None,
    queue_name: str = "default",
    scheduled_for: datetime | None = None,
    task_id: str | None = None,
) -> Task:
    """Create a new task."""
    row = Task(
        task_id=task_id or f"task_{uuid.uuid4().hex}",
        title=title,
        description=description,
        expected_output=expected_output,
        status=status,
        priority=priority,
        created_by=created_by,
        agent_id=agent_id,
        source_type=source_type,
        source_ref=source_ref,
        delivery_mode=delivery_mode,
        delivery_target=delivery_target,
        workflow_id=workflow_id,
        workflow_state=workflow_state,
        queue_name=queue_name,
        scheduled_for=scheduled_for,
    )
    session.add(row)
    await session.flush()
    return row


async def get_task(session: AsyncSession, task_id: str) -> Task | None:
    """Get a task by ID."""
    result = await session.execute(select(Task).where(Task.task_id == task_id))
    return result.scalar_one_or_none()


async def update_task_status(
    session: AsyncSession,
    task_id: str,
    status: str,
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    result_summary: str | None = None,
    result_data: dict[str, object] | None = None,
) -> bool:
    """Update task status and optional lifecycle fields.

    Uses compare-and-swap: only updates if current status allows transition.
    """
    valid_transitions: dict[str, set[str]] = {
        "draft": {"queued", "cancelled"},
        "queued": {"ready", "cancelled"},
        "ready": {"running", "cancelled"},
        "running": {"queued", "paused", "completed", "failed", "cancelled"},
        "paused": {"queued", "running", "cancelled"},
    }
    allowed_from = [k for k, v in valid_transitions.items() if status in v]
    if not allowed_from:
        return False

    values: dict[str, object] = {"status": status}
    if started_at is not None:
        values["started_at"] = started_at
    if completed_at is not None:
        values["completed_at"] = completed_at
    if result_summary is not None:
        values["result_summary"] = result_summary
    if result_data is not None:
        values["result_data"] = result_data

    stmt = (
        update(Task).where(Task.task_id == task_id, Task.status.in_(allowed_from)).values(**values)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def update_task_workflow_state(
    session: AsyncSession,
    task_id: str,
    workflow_state: dict[str, object],
    *,
    expected_version: int | None = None,
) -> bool:
    """Persist workflow state after a step transition.

    When *expected_version* is provided, the update uses optimistic
    concurrency: it reads the current row, checks the stored version,
    and only writes if it matches.  Returns ``False`` if the version
    has been changed by another writer (stale write detected).
    """
    if expected_version is not None:
        # Read current state to check version (within the same session/tx)
        row = await session.execute(select(Task.workflow_state).where(Task.task_id == task_id))
        current_ws = row.scalar_one_or_none()
        if current_ws is not None and isinstance(current_ws, dict):
            db_version = current_ws.get("version", 0)
            if db_version >= expected_version:
                # Another writer already advanced the version — stale write
                return False

    stmt = update(Task).where(Task.task_id == task_id).values(workflow_state=workflow_state)
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def update_task_fields(
    session: AsyncSession,
    task_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    expected_output: str | None = None,
    priority: int | None = None,
    workflow_id: str | None = None,
) -> bool:
    """Update mutable task fields.  Only allowed for draft/queued tasks."""
    values: dict[str, object] = {}
    if title is not None:
        values["title"] = title
    if description is not None:
        values["description"] = description
    if expected_output is not None:
        values["expected_output"] = expected_output
    if priority is not None:
        values["priority"] = priority
    if workflow_id is not None:
        values["workflow_id"] = workflow_id
    if not values:
        return False
    stmt = (
        update(Task)
        .where(Task.task_id == task_id, Task.status.in_(["draft", "queued"]))
        .values(**values)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_tasks_for_agent(
    session: AsyncSession,
    agent_id: str,
    *,
    statuses: list[str] | None = None,
    limit: int = 50,
) -> list[Task]:
    """List tasks for a specific agent, optionally filtered by status."""
    query = (
        select(Task)
        .where(Task.agent_id == agent_id)
        .order_by(Task.priority.desc(), Task.created_at.desc())
        .limit(limit)
    )
    if statuses:
        query = query.where(Task.status.in_(statuses))
    result = await session.execute(query)
    return list(result.scalars().all())


async def list_tasks_by_status(
    session: AsyncSession,
    statuses: list[str],
    *,
    agent_id: str | None = None,
    queue_name: str | None = None,
    limit: int = 100,
) -> list[Task]:
    """List tasks matching the given statuses, ordered by priority DESC, created_at ASC."""
    query = (
        select(Task)
        .where(Task.status.in_(statuses))
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(limit)
    )
    if agent_id is not None:
        query = query.where(Task.agent_id == agent_id)
    if queue_name is not None:
        query = query.where(Task.queue_name == queue_name)
    result = await session.execute(query)
    return list(result.scalars().all())


async def pick_ready_task(
    session: AsyncSession,
    *,
    queue_name: str = "default",
) -> Task | None:
    """Atomically pick the highest priority ready task using compare-and-swap.

    Returns the task if successfully transitioned to 'running', None otherwise.
    """
    now = _utcnow()

    # Find the best candidate
    query = (
        select(Task.task_id)
        .where(
            Task.status == "ready",
            Task.queue_name == queue_name,
            sa.or_(Task.scheduled_for.is_(None), Task.scheduled_for <= now),
        )
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(1)
    )
    result = await session.execute(query)
    candidate_id = result.scalar_one_or_none()
    if candidate_id is None:
        return None

    # CAS: only transition if still ready
    stmt = (
        update(Task)
        .where(Task.task_id == candidate_id, Task.status == "ready")
        .values(status="running", started_at=now)
    )
    cas_result = await session.execute(stmt)
    if int(getattr(cas_result, "rowcount", 0) or 0) == 0:
        return None  # Another coroutine picked it

    # Fetch the updated row
    fetch_result = await session.execute(select(Task).where(Task.task_id == candidate_id))
    return fetch_result.scalar_one_or_none()


async def count_active_steps(
    session: AsyncSession,
    *,
    agent_id: str | None = None,
) -> int:
    """Count running step_runs, optionally filtered by agent."""
    query = select(sa.func.count()).select_from(StepRun).where(StepRun.status == "running")
    if agent_id is not None:
        query = query.where(StepRun.agent_id == agent_id)
    result = await session.execute(query)
    return result.scalar_one()


async def list_stale_running_tasks(
    session: AsyncSession,
    updated_before: datetime,
) -> list[Task]:
    """Find tasks stuck in running state for recovery."""
    result = await session.execute(
        select(Task).where(Task.status == "running", Task.updated_at < updated_before)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Task Dependencies
# ---------------------------------------------------------------------------


async def add_task_dependency(
    session: AsyncSession,
    task_id: str,
    depends_on: str,
    *,
    required: bool = True,
) -> TaskDependency:
    """Add a dependency edge. Raises ValueError on cycle detection."""
    if task_id == depends_on:
        raise ValueError("Task cannot depend on itself")

    # DFS cycle detection: check if depends_on can reach task_id
    if await _would_create_cycle(session, task_id=depends_on, target=task_id):
        raise ValueError(f"Adding dependency {task_id} -> {depends_on} would create a cycle")

    row = TaskDependency(task_id=task_id, depends_on=depends_on, required=required)
    session.add(row)
    await session.flush()
    return row


async def _would_create_cycle(
    session: AsyncSession,
    *,
    task_id: str,
    target: str,
) -> bool:
    """DFS check: can task_id reach target through existing dependencies?"""
    visited: set[str] = set()
    stack = [task_id]
    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        result = await session.execute(
            select(TaskDependency.depends_on).where(TaskDependency.task_id == current)
        )
        for (dep_id,) in result:
            stack.append(dep_id)
    return False


async def remove_task_dependency(
    session: AsyncSession,
    task_id: str,
    depends_on: str,
) -> bool:
    """Remove a dependency edge."""
    stmt = delete(TaskDependency).where(
        TaskDependency.task_id == task_id,
        TaskDependency.depends_on == depends_on,
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def get_task_dependencies(
    session: AsyncSession,
    task_id: str,
) -> list[TaskDependency]:
    """Get all dependencies for a task."""
    result = await session.execute(select(TaskDependency).where(TaskDependency.task_id == task_id))
    return list(result.scalars().all())


async def get_unmet_dependencies(
    session: AsyncSession,
    task_id: str,
    *,
    required_only: bool = True,
) -> list[TaskDependency]:
    """Get dependencies where the depended-on task is NOT completed.

    When required_only=True (default), only required dependencies block
    the task from becoming ready. Optional dependencies are ignored.
    """
    query = (
        select(TaskDependency)
        .join(Task, Task.task_id == TaskDependency.depends_on)
        .where(TaskDependency.task_id == task_id, Task.status != "completed")
    )
    if required_only:
        query = query.where(TaskDependency.required.is_(True))
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_dependent_tasks(
    session: AsyncSession,
    completed_task_id: str,
) -> list[str]:
    """Get task IDs that depend on the given task."""
    result = await session.execute(
        select(TaskDependency.task_id).where(TaskDependency.depends_on == completed_task_id)
    )
    return [row[0] for row in result]


# ---------------------------------------------------------------------------
# Step Runs
# ---------------------------------------------------------------------------


async def create_step_run(
    session: AsyncSession,
    *,
    task_id: str,
    step_name: str,
    step_type: str,
    agent_id: str,
    attempt: int = 1,
    step_run_id: str | None = None,
    conversation_id: str | None = None,
) -> StepRun:
    """Create a new step run record."""
    row = StepRun(
        step_run_id=step_run_id or f"sr_{uuid.uuid4().hex}",
        task_id=task_id,
        step_name=step_name,
        step_type=step_type,
        agent_id=agent_id,
        attempt=attempt,
        conversation_id=conversation_id,
    )
    session.add(row)
    await session.flush()
    return row


_VALID_STEP_RUN_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "cancelled"},
    "running": {"evaluating", "approved", "rejected", "failed", "paused", "cancelled"},
    "evaluating": {"approved", "rejected", "failed"},
    "approved": set(),  # terminal
    "rejected": {"running"},  # retry
    "paused": {"running", "cancelled"},
    "failed": {"running"},  # retry
    "cancelled": set(),  # terminal
}


async def update_step_run(
    session: AsyncSession,
    step_run_id: str,
    *,
    status: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
    intaris_session_id: str | None = None,
    output: dict[str, object] | None = None,
    evaluation: dict[str, object] | None = None,
    todos: list[dict[str, object]] | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> bool:
    """Update step run fields.

    When *status* is provided, the update uses a CAS guard to enforce
    valid state transitions.  Invalid transitions are silently rejected
    (returns ``False``).
    """
    values: dict[str, object] = {}
    if status is not None:
        values["status"] = status
    if conversation_id is not None:
        values["conversation_id"] = conversation_id
    if session_id is not None:
        values["session_id"] = session_id
    if intaris_session_id is not None:
        values["intaris_session_id"] = intaris_session_id
    if output is not None:
        values["output"] = output
    if evaluation is not None:
        values["evaluation"] = evaluation
    if todos is not None:
        values["todos"] = todos
    if started_at is not None:
        values["started_at"] = started_at
    if completed_at is not None:
        values["completed_at"] = completed_at
    if not values:
        return False

    # When updating status, enforce valid transitions via CAS
    if status is not None:
        allowed_from = [k for k, v in _VALID_STEP_RUN_TRANSITIONS.items() if status in v]
        if allowed_from:
            stmt = (
                update(StepRun)
                .where(StepRun.step_run_id == step_run_id, StepRun.status.in_(allowed_from))
                .values(**values)
            )
        else:
            # No valid source states — this is a terminal state, reject
            return False
    else:
        stmt = update(StepRun).where(StepRun.step_run_id == step_run_id).values(**values)

    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_step_runs_for_task(
    session: AsyncSession,
    task_id: str,
) -> list[StepRun]:
    """List all step runs for a task, ordered by started_at."""
    result = await session.execute(
        select(StepRun)
        .where(StepRun.task_id == task_id)
        .order_by(StepRun.started_at.asc(), StepRun.step_run_id.asc())
    )
    return list(result.scalars().all())


async def get_step_run(session: AsyncSession, step_run_id: str) -> StepRun | None:
    """Get a step run by ID."""
    result = await session.execute(select(StepRun).where(StepRun.step_run_id == step_run_id))
    return result.scalar_one_or_none()


async def get_latest_step_run_for_task_step(
    session: AsyncSession,
    task_id: str,
    step_name: str,
) -> StepRun | None:
    """Return the most recent step run for a given task and step name.

    Ordered by ``attempt`` descending with deterministic tiebreakers
    (``started_at``, ``step_run_id``) so the latest attempt is returned
    even when duplicate attempt numbers exist.
    Used by the workflow engine to reuse a prior session on retry.
    """
    result = await session.execute(
        select(StepRun)
        .where(StepRun.task_id == task_id, StepRun.step_name == step_name)
        .order_by(
            StepRun.attempt.desc(),
            StepRun.started_at.desc(),
            StepRun.step_run_id.desc(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def fail_running_step_runs_for_task(
    session: AsyncSession,
    task_id: str,
    completed_at: datetime,
    *,
    final_status: str = "failed",
) -> int:
    """Finalize all active step runs for a task.

    Used by recovery/cancellation paths where a task should stop owning any
    in-flight or paused step-run rows.
    """
    stmt = (
        update(StepRun)
        .where(StepRun.task_id == task_id, StepRun.status.in_(["running", "paused"]))
        .values(status=final_status, completed_at=completed_at)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


async def create_workflow(
    session: AsyncSession,
    *,
    workflow_id: str,
    name: str,
    definition: dict[str, object],
    description: str = "",
    version: int = 1,
    is_system: bool = False,
    owner_email: str | None = None,
) -> WorkflowRow:
    """Create a workflow record."""
    row = WorkflowRow(
        workflow_id=workflow_id,
        name=name,
        description=description,
        version=version,
        definition=definition,
        is_system=is_system,
        owner_email=owner_email,
    )
    session.add(row)
    await session.flush()
    return row


async def get_workflow(session: AsyncSession, workflow_id: str) -> WorkflowRow | None:
    """Get a workflow by ID."""
    result = await session.execute(
        select(WorkflowRow).where(WorkflowRow.workflow_id == workflow_id)
    )
    return result.scalar_one_or_none()


async def list_workflows(
    session: AsyncSession,
    *,
    owner_email: str | None = None,
    include_system: bool = True,
) -> list[WorkflowRow]:
    """List workflows, optionally filtered by owner."""
    query = select(WorkflowRow).order_by(WorkflowRow.name.asc())
    conditions: list[Any] = []
    if include_system:
        conditions.append(WorkflowRow.is_system.is_(True))
    if owner_email is not None:
        conditions.append(WorkflowRow.owner_email == owner_email)
    if conditions:
        query = query.where(sa.or_(*conditions))
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_workflow(
    session: AsyncSession,
    workflow_id: str,
    *,
    updates: dict[str, Any],
) -> bool:
    """Update a workflow row."""
    row = await get_workflow(session, workflow_id)
    if row is None:
        return False
    for field_name, value in updates.items():
        if hasattr(row, field_name) and value is not None:
            setattr(row, field_name, value)
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def delete_workflow(session: AsyncSession, workflow_id: str) -> bool:
    """Delete a workflow row."""
    result = await session.execute(
        delete(WorkflowRow).where(WorkflowRow.workflow_id == workflow_id)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_model_routing(session: AsyncSession) -> list[ModelRouting]:
    """List all model routing rows."""
    result = await session.execute(select(ModelRouting).order_by(ModelRouting.task_type.asc()))
    return list(result.scalars().all())


async def get_model_routing(session: AsyncSession, task_type: str) -> ModelRouting | None:
    """Get a model routing row by task type."""
    result = await session.execute(select(ModelRouting).where(ModelRouting.task_type == task_type))
    return result.scalar_one_or_none()


async def upsert_model_routing(
    session: AsyncSession,
    *,
    task_type: str,
    provider_id: str | None,
    model: str,
    config: dict[str, Any] | None = None,
) -> ModelRouting:
    """Create or update a model routing row."""
    existing = await get_model_routing(session, task_type)
    if existing is not None:
        existing.provider_id = provider_id
        existing.model = model
        existing.config = config
        existing.updated_at = datetime.now(UTC)
        await session.flush()
        return existing
    row = ModelRouting(task_type=task_type, provider_id=provider_id, model=model, config=config)
    session.add(row)
    await session.flush()
    return row


async def delete_model_routing(session: AsyncSession, task_type: str) -> bool:
    """Delete a model routing row."""
    result = await session.execute(delete(ModelRouting).where(ModelRouting.task_type == task_type))
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


async def create_schedule(
    session: AsyncSession,
    *,
    schedule_id: str | None = None,
    name: str,
    cron_expr: str,
    agent_id: str,
    task_template: dict[str, object],
    created_by: str,
    workflow_id: str | None = None,
    enabled: bool = True,
) -> Schedule:
    """Create a schedule record."""
    row = Schedule(
        schedule_id=schedule_id or f"sched_{uuid.uuid4().hex}",
        name=name,
        cron_expr=cron_expr,
        agent_id=agent_id,
        workflow_id=workflow_id,
        task_template=task_template,
        enabled=enabled,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def get_schedule(session: AsyncSession, schedule_id: str) -> Schedule | None:
    """Get a schedule by ID."""
    result = await session.execute(select(Schedule).where(Schedule.schedule_id == schedule_id))
    return result.scalar_one_or_none()


# --- Skills ---


async def list_skills(session: AsyncSession, owner_email: str | None = None) -> list[SkillRow]:
    """List all skills, optionally filtered by owner."""
    stmt = select(SkillRow).order_by(SkillRow.name)
    if owner_email is not None:
        stmt = stmt.where(
            sa.or_(SkillRow.owner_email == owner_email, SkillRow.owner_email.is_(None))
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_skill(session: AsyncSession, skill_id: str) -> SkillRow | None:
    """Get a skill by ID."""
    result = await session.execute(select(SkillRow).where(SkillRow.skill_id == skill_id))
    return result.scalar_one_or_none()


async def create_skill(
    session: AsyncSession,
    *,
    skill_id: str | None = None,
    name: str,
    description: str | None = None,
    instructions: str,
    tools: list[dict[str, Any]] | dict[str, Any] | None = None,
    prompt_templates: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    auto_load: bool = False,
    source: str = "db",
    owner_email: str | None = None,
) -> SkillRow:
    """Create a skill record."""
    row = SkillRow(
        skill_id=skill_id or f"skill_{uuid.uuid4().hex[:12]}",
        name=name,
        description=description,
        instructions=instructions,
        tools=tools,
        prompt_templates=prompt_templates,
        tags=tags,
        auto_load=auto_load,
        source=source,
        owner_email=owner_email,
    )
    session.add(row)
    await session.flush()
    return row


async def update_skill(
    session: AsyncSession,
    skill_id: str,
    **kwargs: Any,
) -> SkillRow | None:
    """Update a skill by ID."""
    row = await get_skill(session, skill_id)
    if row is None:
        return None
    if row.source != "db":
        raise ValueError("Cannot update file-sourced skills")
    for key, value in kwargs.items():
        if hasattr(row, key):
            setattr(row, key, value)
    await session.flush()
    return row


async def delete_skill(session: AsyncSession, skill_id: str) -> bool:
    """Delete a skill by ID. Only DB-managed skills can be deleted."""
    row = await get_skill(session, skill_id)
    if row is None:
        return False
    if row.source != "db":
        raise ValueError("Cannot delete file-sourced skills")
    await session.execute(delete(SkillRow).where(SkillRow.skill_id == skill_id))
    return True


# --- Executors ---


async def list_executors(
    session: AsyncSession, *, owner_email: str | None = None
) -> list[ExecutorRow]:
    """List all executor configurations."""
    stmt = select(ExecutorRow).order_by(ExecutorRow.name)
    if owner_email is not None:
        stmt = stmt.where(ExecutorRow.owner_email == owner_email)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_executor_row(
    session: AsyncSession,
    executor_id: str,
    *,
    owner_email: str | None = None,
) -> ExecutorRow | None:
    """Get an executor by ID."""
    stmt = select(ExecutorRow).where(ExecutorRow.executor_id == executor_id)
    if owner_email is not None:
        stmt = stmt.where(ExecutorRow.owner_email == owner_email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_default_executor(
    session: AsyncSession, *, owner_email: str | None = None
) -> ExecutorRow | None:
    """Get the default executor (is_default=True)."""
    stmt = select(ExecutorRow).where(ExecutorRow.is_default.is_(True)).limit(1)
    if owner_email is not None:
        stmt = stmt.where(ExecutorRow.owner_email == owner_email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_executor(
    session: AsyncSession,
    *,
    executor_id: str | None = None,
    name: str,
    executor_type: str = "in_process",
    labels: dict[str, Any] | None = None,
    enabled_tools: list[str] | None = None,
    enabled_tool_groups: list[str] | None = None,
    config: dict[str, Any] | None = None,
    is_default: bool = False,
    owner_email: str | None = None,
) -> ExecutorRow:
    """Create an executor configuration."""
    row = ExecutorRow(
        executor_id=executor_id or f"exec_{uuid.uuid4().hex[:12]}",
        name=name,
        executor_type=executor_type,
        labels=labels,
        enabled_tools=enabled_tools or [],
        enabled_tool_groups=enabled_tool_groups or [],
        config=config,
        is_default=is_default,
        owner_email=owner_email,
    )
    session.add(row)
    await session.flush()
    return row


async def update_executor(
    session: AsyncSession,
    executor_id: str,
    *,
    owner_email: str | None = None,
    **kwargs: Any,
) -> ExecutorRow | None:
    """Update an executor by ID."""
    row = await get_executor_row(session, executor_id, owner_email=owner_email)
    if row is None:
        return None
    for key, value in kwargs.items():
        if hasattr(row, key):
            setattr(row, key, value)
    await session.flush()
    return row


async def delete_executor(
    session: AsyncSession, executor_id: str, *, owner_email: str | None = None
) -> bool:
    """Delete an executor by ID."""
    row = await get_executor_row(session, executor_id, owner_email=owner_email)
    if row is None:
        return False
    stmt = delete(ExecutorRow).where(ExecutorRow.executor_id == executor_id)
    if owner_email is not None:
        stmt = stmt.where(ExecutorRow.owner_email == owner_email)
    await session.execute(stmt)
    return True


async def update_executor_runtime_state(
    session: AsyncSession,
    executor_id: str,
    *,
    desired_config_version: int | None = None,
    applied_config_version: int | None = None,
    observed_tools: list[dict[str, Any]] | None = None,
    last_observed_at: datetime | None = None,
    runtime_state: str | None = None,
) -> ExecutorRow | None:
    row = await get_executor_row(session, executor_id)
    if row is None:
        return None
    if desired_config_version is not None:
        row.desired_config_version = desired_config_version
    if applied_config_version is not None:
        row.applied_config_version = applied_config_version
    if observed_tools is not None:
        row.observed_tools = observed_tools
    if last_observed_at is not None:
        row.last_observed_at = last_observed_at
    if runtime_state is not None:
        row.runtime_state = runtime_state
    await session.flush()
    return row


async def ensure_default_executor(session: AsyncSession) -> ExecutorRow:
    """Ensure a default in-process executor exists. Create one if missing."""
    existing = await get_default_executor(session)
    if existing is not None:
        return existing
    return await create_executor(
        session,
        executor_id="default_inprocess",
        name="Local (in-process)",
        executor_type="in_process",
        enabled_tools=[],
        enabled_tool_groups=[],
        is_default=True,
    )


# --- MCP Servers ---


async def list_mcp_servers(
    session: AsyncSession, *, owner_email: str | None = None
) -> list[MCPServerRow]:
    """List all MCP server configurations."""
    stmt = select(MCPServerRow).order_by(MCPServerRow.name)
    if owner_email is not None:
        stmt = stmt.where(MCPServerRow.owner_email == owner_email)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_mcp_server(
    session: AsyncSession,
    server_id: str,
    *,
    owner_email: str | None = None,
) -> MCPServerRow | None:
    """Get an MCP server by ID."""
    stmt = select(MCPServerRow).where(MCPServerRow.server_id == server_id)
    if owner_email is not None:
        stmt = stmt.where(MCPServerRow.owner_email == owner_email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_mcp_server(
    session: AsyncSession,
    *,
    server_id: str | None = None,
    name: str,
    transport: str = "stdio",
    command: str | None = None,
    url: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 30,
    description: str | None = None,
    owner_email: str,
    status: str = "active",
) -> MCPServerRow:
    """Create an MCP server configuration."""
    row = MCPServerRow(
        server_id=server_id or f"mcp_{uuid.uuid4().hex[:12]}",
        name=name,
        transport=transport,
        command=command,
        url=url,
        args=args or [],
        env=env or {},
        timeout_seconds=timeout_seconds,
        description=description,
        owner_email=owner_email,
        status=status,
    )
    session.add(row)
    await session.flush()
    return row


async def update_mcp_server(
    session: AsyncSession,
    server_id: str,
    *,
    owner_email: str | None = None,
    **kwargs: Any,
) -> MCPServerRow | None:
    """Update an MCP server by ID."""
    row = await get_mcp_server(session, server_id, owner_email=owner_email)
    if row is None:
        return None
    for key, value in kwargs.items():
        if hasattr(row, key):
            setattr(row, key, value)
    await session.flush()
    return row


async def delete_mcp_server(
    session: AsyncSession, server_id: str, *, owner_email: str | None = None
) -> bool:
    """Delete an MCP server by ID."""
    row = await get_mcp_server(session, server_id, owner_email=owner_email)
    if row is None:
        return False
    stmt = delete(MCPServerRow).where(MCPServerRow.server_id == server_id)
    if owner_email is not None:
        stmt = stmt.where(MCPServerRow.owner_email == owner_email)
    await session.execute(stmt)
    return True


async def mcp_server_referenced_by_executors(
    session: AsyncSession, server_id: str, *, owner_email: str | None = None
) -> list[str]:
    """Return executor IDs that reference this MCP server in their config."""
    from cognis.models.tool import MCP_SERVER_IDS_KEY

    executors = await list_executors(session, owner_email=owner_email)
    referencing: list[str] = []
    for ex in executors:
        config = ex.config or {}
        ids = config.get(MCP_SERVER_IDS_KEY, [])
        if isinstance(ids, list) and server_id in ids:
            referencing.append(ex.executor_id)
    return referencing


# --- Channel Accounts ---


async def list_channel_accounts(
    session: AsyncSession,
    *,
    enabled_only: bool = False,
    user_email: str | None = None,
) -> list[ChannelAccountRow]:
    """List channel accounts with optional filters."""
    stmt = select(ChannelAccountRow).order_by(ChannelAccountRow.created_at.desc())
    if enabled_only:
        stmt = stmt.where(ChannelAccountRow.enabled.is_(True))
    if user_email:
        stmt = stmt.where(ChannelAccountRow.user_email == user_email)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_channel_account(
    session: AsyncSession,
    account_id: str,
) -> ChannelAccountRow | None:
    """Get a channel account by ID."""
    result = await session.execute(
        select(ChannelAccountRow).where(ChannelAccountRow.account_id == account_id)
    )
    return result.scalar_one_or_none()


async def create_channel_account(
    session: AsyncSession,
    *,
    account_id: str | None = None,
    channel_type: str,
    display_name: str,
    agent_id: str,
    user_email: str,
    config: dict[str, Any] | None = None,
    credential_refs: dict[str, str] | None = None,
    default_conversation_id: str | None = None,
    allow_new_conversations: bool = True,
    adapter_location: str = "controller",
    executor_id: str | None = None,
    allowed_senders: list[str] | None = None,
    dm_policy: str = "pairing",
    group_policy: str = "pairing",
    webhook_secret: str | None = None,
) -> ChannelAccountRow:
    """Create a new channel account."""
    row = ChannelAccountRow(
        account_id=account_id or f"ch_{uuid.uuid4().hex[:12]}",
        channel_type=channel_type,
        display_name=display_name,
        agent_id=agent_id,
        user_email=user_email,
        config=config or {},
        credential_refs=credential_refs or {},
        default_conversation_id=default_conversation_id,
        allow_new_conversations=allow_new_conversations,
        adapter_location=adapter_location,
        executor_id=executor_id,
        allowed_senders=allowed_senders or [],
        dm_policy=dm_policy,
        group_policy=group_policy,
        webhook_secret=webhook_secret,
    )
    session.add(row)
    await session.flush()
    return row


async def update_channel_account(
    session: AsyncSession,
    account_id: str,
    **kwargs: Any,
) -> ChannelAccountRow | None:
    """Update a channel account."""
    row = await get_channel_account(session, account_id)
    if row is None:
        return None
    for key, value in kwargs.items():
        if hasattr(row, key):
            setattr(row, key, value)
    await session.flush()
    return row


async def delete_channel_account(
    session: AsyncSession,
    account_id: str,
) -> bool:
    """Delete a channel account."""
    row = await get_channel_account(session, account_id)
    if row is None:
        return False
    await session.delete(row)
    return True


async def get_latest_active_conversation_for_context(
    session: AsyncSession,
    *,
    user_email: str,
    agent_id: str,
    context_ref: str,
) -> Conversation | None:
    """Find the latest active conversation matching a context ref."""
    result = await session.execute(
        select(Conversation)
        .where(
            Conversation.user_email == user_email,
            Conversation.agent_id == agent_id,
            Conversation.context_ref == context_ref,
            Conversation.status == "active",
        )
        .order_by(Conversation.updated_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# --- Channel Contacts ---


async def get_channel_contact(
    session: AsyncSession,
    channel_type: str,
    sender_id: str,
) -> ChannelContact | None:
    """Look up a channel contact by platform identity."""
    result = await session.execute(
        select(ChannelContact).where(
            ChannelContact.channel_type == channel_type,
            ChannelContact.sender_id == sender_id,
        )
    )
    return result.scalar_one_or_none()


async def create_channel_contact(
    session: AsyncSession,
    *,
    channel_type: str,
    sender_id: str,
    user_email: str,
    display_name: str | None = None,
    verified: bool = False,
) -> ChannelContact:
    """Create a new channel contact mapping."""
    row = ChannelContact(
        contact_id=f"cc_{uuid.uuid4().hex[:12]}",
        channel_type=channel_type,
        sender_id=sender_id,
        user_email=user_email,
        display_name=display_name,
        verified=verified,
    )
    session.add(row)
    await session.flush()
    return row


async def list_channel_contacts(
    session: AsyncSession,
    user_email: str | None = None,
) -> list[ChannelContact]:
    """List channel contacts."""
    stmt = select(ChannelContact).order_by(ChannelContact.created_at.desc())
    if user_email:
        stmt = stmt.where(ChannelContact.user_email == user_email)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# --- Channel Pairing Requests ---


async def get_pending_pairing_request_for_sender(
    session: AsyncSession,
    *,
    account_id: str,
    channel_type: str,
    sender_id: str,
    now: datetime | None = None,
) -> ChannelPairingRequest | None:
    """Return the active pending pairing request for a sender, if any."""
    current_time = now or _utcnow()
    result = await session.execute(
        select(ChannelPairingRequest)
        .where(
            ChannelPairingRequest.account_id == account_id,
            ChannelPairingRequest.channel_type == channel_type,
            ChannelPairingRequest.sender_id == sender_id,
            ChannelPairingRequest.status == "pending",
            ChannelPairingRequest.expires_at > current_time,
        )
        .order_by(ChannelPairingRequest.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def count_recent_pairing_requests(
    session: AsyncSession,
    *,
    account_id: str,
    channel_type: str,
    sender_id: str,
    since: datetime,
) -> int:
    """Count recent pairing requests for rate limiting."""
    result = await session.execute(
        select(sa.func.count(ChannelPairingRequest.request_id)).where(
            ChannelPairingRequest.account_id == account_id,
            ChannelPairingRequest.channel_type == channel_type,
            ChannelPairingRequest.sender_id == sender_id,
            ChannelPairingRequest.created_at >= since,
        )
    )
    return int(result.scalar() or 0)


async def create_pairing_request(
    session: AsyncSession,
    *,
    owner_email: str,
    account_id: str,
    channel_type: str,
    sender_id: str,
    sender_name: str | None,
    chat_id: str,
    chat_name: str | None,
    code: str,
    expires_at: datetime,
) -> ChannelPairingRequest:
    """Create a new pairing challenge."""
    row = ChannelPairingRequest(
        request_id=f"cpr_{uuid.uuid4().hex[:12]}",
        owner_email=owner_email,
        account_id=account_id,
        channel_type=channel_type,
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=chat_id,
        chat_name=chat_name,
        code=code,
        status="pending",
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def list_pairing_requests(
    session: AsyncSession,
    *,
    owner_email: str,
    statuses: list[str] | None = None,
) -> list[ChannelPairingRequest]:
    """List pairing requests for a Cognis user."""
    stmt = select(ChannelPairingRequest).where(ChannelPairingRequest.owner_email == owner_email)
    if statuses:
        stmt = stmt.where(ChannelPairingRequest.status.in_(statuses))
    stmt = stmt.order_by(ChannelPairingRequest.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


# --- Artifacts ---


async def create_artifact_record(
    session: AsyncSession,
    *,
    artifact_id: str,
    namespace: str,
    object_id: str,
    filename: str,
    owner_email: str | None,
    purpose: str,
    kind: str,
    mime_type: str,
    size_bytes: int,
    status: str = "temporary",
    expires_at: datetime | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
    message_role: str | None = None,
) -> ArtifactRecordRow:
    row = ArtifactRecordRow(
        artifact_id=artifact_id,
        namespace=namespace,
        object_id=object_id,
        filename=filename,
        owner_email=owner_email,
        conversation_id=conversation_id,
        session_id=session_id,
        message_role=message_role,
        purpose=purpose,
        kind=kind,
        mime_type=mime_type,
        size_bytes=size_bytes,
        status=status,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def get_artifact_record(session: AsyncSession, artifact_id: str) -> ArtifactRecordRow | None:
    result = await session.execute(
        select(ArtifactRecordRow).where(ArtifactRecordRow.artifact_id == artifact_id)
    )
    return result.scalar_one_or_none()


async def mark_artifacts_attached(
    session: AsyncSession,
    artifact_ids: list[str],
    *,
    owner_email: str | None = None,
    conversation_id: str,
    session_id: str | None = None,
    message_role: str = "user",
) -> int:
    if not artifact_ids:
        return 0
    stmt = update(ArtifactRecordRow).where(ArtifactRecordRow.artifact_id.in_(artifact_ids))
    if owner_email is not None:
        stmt = stmt.where(ArtifactRecordRow.owner_email == owner_email)
    result = await session.execute(
        stmt.values(
            status="attached",
            conversation_id=conversation_id,
            session_id=session_id,
            message_role=message_role,
            expires_at=None,
        )
    )
    return int(result.rowcount or 0)


async def list_expired_temporary_artifacts(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 200,
) -> list[ArtifactRecordRow]:
    result = await session.execute(
        select(ArtifactRecordRow)
        .where(
            ArtifactRecordRow.status == "temporary",
            ArtifactRecordRow.expires_at.is_not(None),
            ArtifactRecordRow.expires_at <= now,
        )
        .order_by(ArtifactRecordRow.expires_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_orphaned_attached_artifacts(
    session: AsyncSession,
    *,
    limit: int = 200,
) -> list[ArtifactRecordRow]:
    result = await session.execute(
        select(ArtifactRecordRow)
        .where(
            ArtifactRecordRow.status == "attached",
            sa.or_(
                sa.and_(
                    ArtifactRecordRow.owner_email.is_not(None),
                    sa.not_(
                        sa.exists(
                            select(User.email).where(User.email == ArtifactRecordRow.owner_email)
                        )
                    ),
                ),
                sa.and_(
                    ArtifactRecordRow.conversation_id.is_not(None),
                    sa.not_(
                        sa.exists(
                            select(Conversation.conversation_id).where(
                                Conversation.conversation_id == ArtifactRecordRow.conversation_id
                            )
                        )
                    ),
                ),
            ),
        )
        .order_by(ArtifactRecordRow.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def mark_artifact_deleted(session: AsyncSession, artifact_id: str) -> bool:
    row = await get_artifact_record(session, artifact_id)
    if row is None:
        return False
    row.status = "deleted"
    row.deleted_at = _utcnow()
    await session.flush()
    return True


async def delete_artifact_record(session: AsyncSession, artifact_id: str) -> bool:
    row = await get_artifact_record(session, artifact_id)
    if row is None:
        return False
    await session.delete(row)
    return True


async def get_pairing_request_by_code(
    session: AsyncSession,
    *,
    owner_email: str | None,
    code: str,
) -> ChannelPairingRequest | None:
    """Look up a pairing request by owner and code."""
    stmt = select(ChannelPairingRequest).where(ChannelPairingRequest.code == code)
    if owner_email is not None:
        stmt = stmt.where(ChannelPairingRequest.owner_email == owner_email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def increment_pairing_request_attempts(
    session: AsyncSession,
    request_id: str,
) -> ChannelPairingRequest | None:
    """Increment redemption attempts for a pairing request."""
    result = await session.execute(
        select(ChannelPairingRequest).where(ChannelPairingRequest.request_id == request_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    row.attempts += 1
    await session.flush()
    return row


async def complete_pairing_request(
    session: AsyncSession,
    request_id: str,
) -> ChannelPairingRequest | None:
    """Mark a pairing request as completed."""
    result = await session.execute(
        select(ChannelPairingRequest).where(ChannelPairingRequest.request_id == request_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    row.status = "completed"
    row.completed_at = _utcnow()
    await session.flush()
    return row


async def reject_pairing_request(
    session: AsyncSession,
    *,
    owner_email: str,
    request_id: str,
) -> ChannelPairingRequest | None:
    """Reject a pairing request owned by the user."""
    result = await session.execute(
        select(ChannelPairingRequest).where(
            ChannelPairingRequest.request_id == request_id,
            ChannelPairingRequest.owner_email == owner_email,
            ChannelPairingRequest.status == "pending",
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    row.status = "rejected"
    row.completed_at = _utcnow()
    await session.flush()
    return row


async def expire_stale_pairing_requests(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Expire all pending pairing requests past their deadline."""
    current_time = now or _utcnow()
    result = await session.execute(
        update(ChannelPairingRequest)
        .where(
            ChannelPairingRequest.status == "pending",
            ChannelPairingRequest.expires_at <= current_time,
        )
        .values(status="expired")
    )
    return int(result.rowcount or 0)

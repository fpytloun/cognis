"""Reusable async query helpers for Cognis DB."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import case, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from cognis.core.agent_direct import agent_direct_context_ref
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.store.models import (
    Agent,
    AgentGrantRow,
    AgentSecondaryBinding,
    ApiKey,
    ArtifactRecordRow,
    BrowserSession,
    ChannelAccountRow,
    ChannelContact,
    ChannelDeliveryOutboxRow,
    ChannelPairingRequest,
    Conversation,
    CredentialRow,
    DeliverableRow,
    ExecutorRow,
    KnowledgebaseArtifactRow,
    KnowledgebaseChunkRow,
    KnowledgebaseIndexJobRow,
    KnowledgebaseRow,
    LLMProvider,
    LLMProviderAuthSession,
    ManagedConversationLink,
    MCPOAuthTokenRow,
    MCPOAuthTransactionRow,
    MCPServerRow,
    ModelRouting,
    NotificationRow,
    ProjectGrantRow,
    ProjectRow,
    ProjectSourceRow,
    ProjectWorkflowRow,
    Schedule,
    Secret,
    Session,
    Setting,
    SkillAssetRow,
    SkillRow,
    SkillVersionRow,
    StepRun,
    SystemAgentOverride,
    SystemWorkflowOverride,
    Task,
    TaskCommentRow,
    TaskDependency,
    ToolClassificationOverrideRow,
    ToolClassificationRow,
    TtsCacheRow,
    User,
    WorkflowRow,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _UnsetValue:
    pass


_UNSET = _UnsetValue()


def _shared_owner_clause(column: Any) -> Any:
    return sa.or_(column == SYSTEM_USER_EMAIL, column.is_(None))


def _exclude_agent_direct_clause() -> sa.ColumnElement[bool]:
    return sa.or_(
        Conversation.context_type != "web",
        sa.and_(
            sa.or_(
                Conversation.context_ref.is_(None),
                Conversation.context_ref.not_like("web:agent_direct:%"),
            ),
            sa.or_(
                Conversation.context_data.is_(None),
                Conversation.context_data["kind"].as_string().is_(None),
                Conversation.context_data["kind"].as_string() != "agent_direct",
            ),
        ),
    )


def _agent_direct_clause(user_email: str, agent_id: str) -> sa.ColumnElement[bool]:
    return sa.or_(
        sa.and_(
            Conversation.context_type == "web",
            Conversation.context_ref == agent_direct_context_ref(user_email, agent_id),
        ),
        sa.and_(
            Conversation.context_type == "web",
            Conversation.context_data["kind"].as_string() == "agent_direct",
        ),
    )


def tool_classification_scope(owner_email: str | None) -> str:
    """Return the persistence scope for tool classifications."""

    return owner_email or "__global__"


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
    password_hash: str | None = None,
) -> User | None:
    """Update mutable user fields. Returns updated user or None if not found."""
    user = await get_user(session, email)
    if user is None:
        return None
    if name is not None:
        user.name = name
    if role is not None:
        user.role = role
    if password_hash is not None:
        user.password_hash = password_hash
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
    await session.execute(delete(AgentGrantRow).where(AgentGrantRow.grantee_user_email == email))
    await session.execute(delete(AgentGrantRow).where(AgentGrantRow.granted_by == email))
    # Schedules reference agents owned by user
    await session.execute(delete(Schedule).where(Schedule.created_by == email))
    # Tasks reference agents and users
    await session.execute(delete(Task).where(Task.created_by == email))
    # Now delete the main tables
    await session.execute(delete(Conversation).where(Conversation.user_email == email))
    await session.execute(delete(Agent).where(Agent.owner_email == email))
    await session.execute(delete(ApiKey).where(ApiKey.user_email == email))
    await session.execute(delete(BrowserSession).where(BrowserSession.user_email == email))
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


def hash_browser_session_token(token: str) -> str:
    """Hash an opaque browser session token before persisting it."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_browser_session(
    session: AsyncSession,
    *,
    user_email: str,
    expires_at: datetime,
    user_agent: str | None = None,
) -> tuple[BrowserSession, str]:
    """Create a new opaque browser session and return the raw token once."""

    raw_token = secrets.token_urlsafe(32)
    row = BrowserSession(
        session_id=f"bs_{uuid.uuid4().hex}",
        user_email=user_email,
        token_hash=hash_browser_session_token(raw_token),
        user_agent=user_agent,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row, raw_token


async def get_browser_session_by_token(session: AsyncSession, token: str) -> BrowserSession | None:
    """Look up a browser session from an opaque session token."""

    token_hash = hash_browser_session_token(token)
    result = await session.execute(
        select(BrowserSession).where(BrowserSession.token_hash == token_hash)
    )
    return result.scalar_one_or_none()


async def revoke_browser_session(session: AsyncSession, session_id: str) -> bool:
    """Revoke one browser session by ID."""

    row = await session.get(BrowserSession, session_id)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = _utcnow()
    row.updated_at = _utcnow()
    await session.flush()
    return True


async def revoke_browser_session_by_token(session: AsyncSession, token: str) -> bool:
    """Revoke one browser session by opaque token."""

    row = await get_browser_session_by_token(session, token)
    if row is None:
        return False
    return await revoke_browser_session(session, row.session_id)


async def touch_browser_session(
    session: AsyncSession,
    session_row: BrowserSession,
    *,
    expires_at: datetime | None = None,
) -> BrowserSession:
    """Update last-used metadata for a browser session."""

    session_row.last_used_at = _utcnow()
    session_row.updated_at = _utcnow()
    if expires_at is not None:
        session_row.expires_at = expires_at
    await session.flush()
    return session_row


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


async def delete_setting(session: AsyncSession, key: str) -> bool:
    """Delete a setting by key."""

    result = await session.execute(delete(Setting).where(Setting.key == key))
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


# --- LLM Providers ---


def _visible_llm_provider_clause(user_email: str | None) -> Any:
    if user_email is None or user_email == SYSTEM_USER_EMAIL:
        return _shared_owner_clause(LLMProvider.owner_email)
    return sa.or_(
        LLMProvider.owner_email == user_email, _shared_owner_clause(LLMProvider.owner_email)
    )


async def list_llm_providers(
    session: AsyncSession,
    acting_user_email: str | None = None,
    *,
    include_inactive: bool = False,
) -> list[LLMProvider]:
    """List all LLM provider configurations."""
    stmt = select(LLMProvider)
    if not include_inactive:
        stmt = stmt.where(LLMProvider.status == "active")
    if acting_user_email is not None:
        stmt = stmt.where(_visible_llm_provider_clause(acting_user_email))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_llm_provider(
    session: AsyncSession,
    *,
    provider_id: str,
    display_name: str,
    location: str,
    backend: str,
    config: dict[str, Any],
    owner_email: str = SYSTEM_USER_EMAIL,
    status: str = "active",
) -> LLMProvider:
    """Create a new LLM provider row."""
    row = LLMProvider(
        provider_id=provider_id,
        display_name=display_name,
        location=location,
        backend=backend,
        owner_email=owner_email,
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
    owner_email: str | None = None,
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
    if owner_email is not None:
        row.owner_email = owner_email
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

    visible = await list_visible_agents(session, owner_email)
    return [
        {
            "agent_id": row.agent_id,
            "name": row.name,
            "description": row.description,
            "status": row.status,
        }
        for row, _grant in visible
        if row.status == "active"
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


async def list_visible_agents(
    session: AsyncSession,
    user_email: str,
) -> list[tuple[Agent, AgentGrantRow | None]]:
    """List agents visible to a user (owned + actively shared)."""

    owned = await list_agents(session, owner_email=user_email)
    shared_result = await session.execute(
        select(Agent, AgentGrantRow)
        .join(AgentGrantRow, AgentGrantRow.agent_id == Agent.agent_id)
        .where(AgentGrantRow.grantee_type == "user")
        .where(AgentGrantRow.grantee_user_email == user_email)
        .where(AgentGrantRow.revoked_at.is_(None))
        .order_by(Agent.updated_at.desc(), Agent.agent_id.asc())
    )
    rows: list[tuple[Agent, AgentGrantRow | None]] = [(row, None) for row in owned]
    seen = {row.agent_id for row in owned}
    for agent_row, grant_row in shared_result.all():
        if agent_row.agent_id in seen:
            continue
        rows.append((agent_row, grant_row))
        seen.add(agent_row.agent_id)
    return rows


async def get_active_agent_grant(
    session: AsyncSession,
    agent_id: str,
    grantee_user_email: str,
) -> AgentGrantRow | None:
    """Return the active user grant for an agent and grantee email."""

    result = await session.execute(
        select(AgentGrantRow)
        .where(AgentGrantRow.agent_id == agent_id)
        .where(AgentGrantRow.grantee_type == "user")
        .where(AgentGrantRow.grantee_user_email == grantee_user_email)
        .where(AgentGrantRow.revoked_at.is_(None))
        .order_by(AgentGrantRow.granted_at.desc(), AgentGrantRow.grant_id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_agent_grants(session: AsyncSession, agent_id: str) -> list[AgentGrantRow]:
    """List active grants for an agent."""

    result = await session.execute(
        select(AgentGrantRow)
        .where(AgentGrantRow.agent_id == agent_id)
        .where(AgentGrantRow.revoked_at.is_(None))
        .order_by(AgentGrantRow.granted_at.desc(), AgentGrantRow.grant_id.asc())
    )
    return list(result.scalars().all())


async def get_agent_grant_for_user(
    session: AsyncSession,
    agent_id: str,
    grantee_user_email: str,
) -> AgentGrantRow | None:
    """Return the latest grant row for a user principal, including revoked grants."""

    result = await session.execute(
        select(AgentGrantRow)
        .where(AgentGrantRow.agent_id == agent_id)
        .where(AgentGrantRow.grantee_type == "user")
        .where(AgentGrantRow.grantee_user_email == grantee_user_email)
        .order_by(AgentGrantRow.granted_at.desc(), AgentGrantRow.grant_id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_agent_grant(session: AsyncSession, grant_id: str) -> AgentGrantRow | None:
    """Get a grant by ID."""

    result = await session.execute(select(AgentGrantRow).where(AgentGrantRow.grant_id == grant_id))
    return result.scalar_one_or_none()


async def create_agent_grant(
    session: AsyncSession,
    *,
    agent_id: str,
    grantee_user_email: str,
    executor_scope: str,
    granted_by: str,
    note: str | None = None,
) -> AgentGrantRow:
    """Create an agent-sharing grant."""

    row = AgentGrantRow(
        grant_id=f"grant_{uuid.uuid4().hex[:12]}",
        agent_id=agent_id,
        grantee_type="user",
        grantee_user_email=grantee_user_email,
        permission="use",
        executor_scope=executor_scope,
        granted_by=granted_by,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def update_agent_grant(
    session: AsyncSession,
    grant_id: str,
    *,
    executor_scope: str | None = None,
    note: str | None = None,
    grantee_overrides: dict[str, Any] | None | object = _UNSET,
    revoked_at: datetime | None | object = _UNSET,
    granted_at: datetime | None | object = _UNSET,
    granted_by: str | None | object = _UNSET,
) -> AgentGrantRow | None:
    """Update mutable fields on an agent-sharing grant."""

    row = await get_agent_grant(session, grant_id)
    if row is None:
        return None
    if executor_scope is not None:
        row.executor_scope = executor_scope
    if note is not None:
        row.note = note
    if grantee_overrides is not _UNSET:
        row.grantee_overrides = grantee_overrides  # type: ignore[assignment]
    if revoked_at is not _UNSET:
        row.revoked_at = revoked_at
    if granted_at is not _UNSET:
        row.granted_at = granted_at
    if granted_by is not _UNSET:
        row.granted_by = granted_by
    await session.flush()
    return row


async def revoke_agent_grant(session: AsyncSession, grant_id: str) -> AgentGrantRow | None:
    """Soft-revoke an agent-sharing grant."""

    row = await get_agent_grant(session, grant_id)
    if row is None:
        return None
    row.revoked_at = datetime.now(UTC)
    await session.flush()
    return row


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


async def get_system_agent_override(
    session: AsyncSession, *, owner_email: str, agent_id: str
) -> SystemAgentOverride | None:
    """Return the per-user override for a shipped system agent."""

    result = await session.execute(
        select(SystemAgentOverride).where(
            SystemAgentOverride.owner_email == owner_email,
            SystemAgentOverride.agent_id == agent_id,
        )
    )
    return result.scalar_one_or_none()


async def list_system_agent_overrides(
    session: AsyncSession, *, owner_email: str
) -> list[SystemAgentOverride]:
    """List all system-agent overrides for a user."""

    result = await session.execute(
        select(SystemAgentOverride).where(SystemAgentOverride.owner_email == owner_email)
    )
    return list(result.scalars().all())


async def upsert_system_agent_override(
    session: AsyncSession,
    *,
    owner_email: str,
    agent_id: str,
    disabled: bool | None = None,
    llm_config_override: dict[str, Any] | None = None,
    skills_override: dict[str, Any] | None = None,
    execution_override: dict[str, Any] | None = None,
) -> SystemAgentOverride:
    """Create or update a per-user system-agent override row."""

    row = await get_system_agent_override(session, owner_email=owner_email, agent_id=agent_id)
    if row is None:
        row = SystemAgentOverride(
            override_id=f"sao_{uuid.uuid4().hex[:12]}",
            owner_email=owner_email,
            agent_id=agent_id,
        )
        session.add(row)
    if disabled is not None:
        row.disabled = disabled
    row.llm_config_override = llm_config_override
    row.skills_override = skills_override
    row.execution_override = execution_override
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def delete_system_agent_override(
    session: AsyncSession, *, owner_email: str, agent_id: str
) -> bool:
    """Delete a per-user system-agent override row."""

    result = await session.execute(
        delete(SystemAgentOverride).where(
            SystemAgentOverride.owner_email == owner_email,
            SystemAgentOverride.agent_id == agent_id,
        )
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


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


async def get_visible_llm_provider(
    session: AsyncSession, provider_id: str, acting_user_email: str | None
) -> LLMProvider | None:
    """Get a provider only if it is visible to the acting user."""

    stmt = select(LLMProvider).where(
        LLMProvider.provider_id == provider_id,
    )
    if acting_user_email is not None:
        stmt = stmt.where(_visible_llm_provider_clause(acting_user_email))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- Conversations ---


async def create_project(
    session: AsyncSession,
    *,
    owner_email: str,
    name: str,
    description: str | None = None,
    instructions: str | None = None,
    default_workflow_id: str | None = None,
    avatar_image_id: str | None = None,
    avatar_url: str | None = None,
    metadata: dict[str, object] | None = None,
    project_id: str | None = None,
) -> ProjectRow:
    row = ProjectRow(
        project_id=project_id or f"proj_{uuid.uuid4().hex}",
        owner_email=owner_email,
        name=name,
        description=description,
        instructions=instructions,
        default_workflow_id=default_workflow_id,
        avatar_image_id=avatar_image_id,
        avatar_url=avatar_url,
        metadata_json=metadata or {},
    )
    session.add(row)
    await session.flush()
    return row


async def get_project(session: AsyncSession, project_id: str) -> ProjectRow | None:
    result = await session.execute(select(ProjectRow).where(ProjectRow.project_id == project_id))
    return result.scalar_one_or_none()


async def list_projects_for_user(
    session: AsyncSession,
    user_email: str,
    *,
    status: str | None = "active",
    query: str | None = None,
) -> list[ProjectRow]:
    grant_project_ids = select(ProjectGrantRow.project_id).where(
        ProjectGrantRow.grantee_type == "user",
        ProjectGrantRow.grantee_user_email == user_email,
        ProjectGrantRow.revoked_at.is_(None),
    )
    stmt = select(ProjectRow).where(
        sa.or_(ProjectRow.owner_email == user_email, ProjectRow.project_id.in_(grant_project_ids))
    )
    if status and status != "all":
        stmt = stmt.where(ProjectRow.status == status)
    if query:
        pattern = f"%{query}%"
        stmt = stmt.where(
            sa.or_(ProjectRow.name.ilike(pattern), ProjectRow.description.ilike(pattern))
        )
    result = await session.execute(
        stmt.order_by(ProjectRow.updated_at.desc(), ProjectRow.name.asc())
    )
    return list(result.scalars().all())


async def update_project(
    session: AsyncSession, project_id: str, **fields: Any
) -> ProjectRow | None:
    row = await get_project(session, project_id)
    if row is None:
        return None
    allowed = {
        "name",
        "description",
        "instructions",
        "default_workflow_id",
        "avatar_image_id",
        "avatar_url",
        "status",
    }
    for key, value in fields.items():
        if key == "metadata":
            row.metadata_json = value
        elif key in allowed:
            setattr(row, key, value)
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def create_project_source(
    session: AsyncSession,
    *,
    project_id: str,
    name: str,
    local_path: str | None = None,
    remote_url: str | None = None,
    default_branch: str | None = None,
    credential_ref: str | None = None,
    instructions: str | None = None,
    metadata: dict[str, object] | None = None,
    source_id: str | None = None,
) -> ProjectSourceRow:
    row = ProjectSourceRow(
        source_id=source_id or f"psrc_{uuid.uuid4().hex}",
        project_id=project_id,
        name=name,
        local_path=local_path,
        remote_url=remote_url,
        default_branch=default_branch,
        credential_ref=credential_ref,
        instructions=instructions,
        metadata_json=metadata or {},
    )
    session.add(row)
    await session.flush()
    return row


async def list_project_sources(session: AsyncSession, project_id: str) -> list[ProjectSourceRow]:
    result = await session.execute(
        select(ProjectSourceRow)
        .where(ProjectSourceRow.project_id == project_id)
        .order_by(ProjectSourceRow.name.asc())
    )
    return list(result.scalars().all())


async def get_project_source(session: AsyncSession, source_id: str) -> ProjectSourceRow | None:
    result = await session.execute(
        select(ProjectSourceRow).where(ProjectSourceRow.source_id == source_id)
    )
    return result.scalar_one_or_none()


async def update_project_source(
    session: AsyncSession, source_id: str, **fields: Any
) -> ProjectSourceRow | None:
    row = await get_project_source(session, source_id)
    if row is None:
        return None
    for key, value in fields.items():
        if key == "metadata":
            row.metadata_json = value
        elif key in {
            "name",
            "local_path",
            "remote_url",
            "default_branch",
            "credential_ref",
            "instructions",
        }:
            setattr(row, key, value)
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def delete_project_source(session: AsyncSession, source_id: str) -> bool:
    result = await session.execute(
        delete(ProjectSourceRow).where(ProjectSourceRow.source_id == source_id)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def attach_project_workflow(
    session: AsyncSession, project_id: str, workflow_id: str
) -> ProjectWorkflowRow:
    row = await session.get(
        ProjectWorkflowRow, {"project_id": project_id, "workflow_id": workflow_id}
    )
    if row is not None:
        return row
    row = ProjectWorkflowRow(project_id=project_id, workflow_id=workflow_id)
    session.add(row)
    await session.flush()
    return row


async def detach_project_workflow(session: AsyncSession, project_id: str, workflow_id: str) -> bool:
    result = await session.execute(
        delete(ProjectWorkflowRow).where(
            ProjectWorkflowRow.project_id == project_id,
            ProjectWorkflowRow.workflow_id == workflow_id,
        )
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_project_workflow_ids(session: AsyncSession, project_id: str) -> list[str]:
    result = await session.execute(
        select(ProjectWorkflowRow.workflow_id).where(ProjectWorkflowRow.project_id == project_id)
    )
    return [str(item) for item in result.scalars().all()]


async def list_bound_workflow_ids(session: AsyncSession) -> set[str]:
    result = await session.execute(select(ProjectWorkflowRow.workflow_id).distinct())
    return {str(item) for item in result.scalars().all()}


async def create_project_grant(
    session: AsyncSession,
    *,
    project_id: str,
    granted_by: str,
    grantee_type: str = "user",
    grantee_user_email: str | None = None,
    grantee_group_id: str | None = None,
    permission: str = "use",
    note: str | None = None,
) -> ProjectGrantRow:
    row = ProjectGrantRow(
        grant_id=f"pgrant_{uuid.uuid4().hex}",
        project_id=project_id,
        grantee_type=grantee_type,
        grantee_user_email=grantee_user_email,
        grantee_group_id=grantee_group_id,
        permission=permission,
        granted_by=granted_by,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def get_active_project_grant(
    session: AsyncSession,
    project_id: str,
    user_email: str,
) -> ProjectGrantRow | None:
    result = await session.execute(
        select(ProjectGrantRow).where(
            ProjectGrantRow.project_id == project_id,
            ProjectGrantRow.grantee_type == "user",
            ProjectGrantRow.grantee_user_email == user_email,
            ProjectGrantRow.revoked_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def list_project_grants(session: AsyncSession, project_id: str) -> list[ProjectGrantRow]:
    result = await session.execute(
        select(ProjectGrantRow)
        .where(ProjectGrantRow.project_id == project_id)
        .order_by(ProjectGrantRow.granted_at.desc())
    )
    return list(result.scalars().all())


async def get_project_grant(session: AsyncSession, grant_id: str) -> ProjectGrantRow | None:
    return await session.get(ProjectGrantRow, grant_id)


async def revoke_project_grant(session: AsyncSession, grant_id: str) -> bool:
    row = await session.get(ProjectGrantRow, grant_id)
    if row is None:
        return False
    row.revoked_at = _utcnow()
    await session.flush()
    return True


async def create_conversation(
    session: AsyncSession,
    user_email: str,
    agent_id: str,
    context_type: str,
    *,
    title: str | None = None,
    title_source: str = "unset",
    context_ref: str | None = None,
    context_data: dict[str, object] | None = None,
    memory_labels: dict[str, object] | None = None,
    conversation_id: str | None = None,
    project_id: str | None = None,
) -> Conversation:
    """Create a new conversation row."""

    conversation = Conversation(
        conversation_id=conversation_id or f"conv_{uuid.uuid4().hex}",
        user_email=user_email,
        agent_id=agent_id,
        title=title,
        title_source=title_source,
        context_type=context_type,
        context_ref=context_ref,
        project_id=project_id,
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


async def get_conversation_channel_route(
    session: AsyncSession,
    conversation_id: str,
) -> tuple[str, str, str, str | None, str] | None:
    """Resolve stored channel routing metadata for a conversation.

    Returns ``(channel_type, account_id, chat_id, thread_id, user_email)``
    or ``None`` when the conversation is not channel-bound.
    """

    row = await get_conversation(session, conversation_id)
    if row is None or row.context_type in {"web", "api"}:
        return None

    platform_data = row.context_data or {}
    channel_type = platform_data.get("channel_type")
    account_id = platform_data.get("account_id")
    chat_id = platform_data.get("chat_id")
    thread_id = platform_data.get("thread_id")

    if not all([channel_type, account_id, chat_id]):
        if row.context_ref and ":" in row.context_ref:
            parts = row.context_ref.split(":", 3)
            if len(parts) >= 3:
                channel_type = parts[0]
                account_id = parts[1]
                chat_id = parts[2]
                thread_id = parts[3] if len(parts) > 3 else None
            else:
                return None
        else:
            return None

    return (
        str(channel_type),
        str(account_id),
        str(chat_id),
        str(thread_id) if thread_id else None,
        row.user_email,
    )


async def list_conversations(
    session: AsyncSession,
    user_email: str,
    *,
    context_type: str | None = None,
    agent_id: str | None = None,
    status: str = "active",
    project_id: str | None = None,
    include_agent_direct: bool = True,
) -> list[Conversation]:
    """List conversations for a user, optionally filtered by context type and agent.

    Ordered by the latest accepted conversation activity. Conversations with
    no messages fall back to creation time and deterministic ID ordering.
    """
    ordering_activity = sa.func.coalesce(
        Conversation.last_message_at,
        Conversation.created_at,
    )
    query = (
        select(Conversation)
        .where(Conversation.user_email == user_email)
        .order_by(
            ordering_activity.desc(),
            Conversation.created_at.desc(),
            Conversation.conversation_id.asc(),
        )
    )
    if status == "active":
        query = query.where(Conversation.status == "active")
    elif status == "archived":
        query = query.where(Conversation.status == "archived")
    elif status == "starred":
        query = query.where(
            Conversation.starred_at.is_not(None),
            Conversation.status == "active",
        )
    elif status != "all":
        raise ValueError(f"Unsupported conversation status filter: {status}")
    if context_type is not None:
        query = query.where(Conversation.context_type == context_type)
    if agent_id is not None:
        query = query.where(Conversation.agent_id == agent_id)
    if project_id is not None:
        query = query.where(Conversation.project_id == project_id)
    if not include_agent_direct:
        query = query.where(_exclude_agent_direct_clause())
    result = await session.execute(query)
    return list(result.scalars().all())


async def list_sessions_by_ids(
    session: AsyncSession,
    session_ids: list[str],
) -> dict[str, Session]:
    """Return session rows keyed by session ID."""

    ids = sorted({session_id for session_id in session_ids if session_id})
    if not ids:
        return {}
    result = await session.execute(select(Session).where(Session.session_id.in_(ids)))
    return {row.session_id: row for row in result.scalars().all()}


async def list_pending_notification_types_by_conversation(
    session: AsyncSession,
    user_email: str,
    conversation_ids: list[str],
) -> dict[str, list[str]]:
    """Return pending notification types keyed by conversation ID."""

    ids = sorted({conversation_id for conversation_id in conversation_ids if conversation_id})
    if not ids:
        return {}
    result = await session.execute(
        select(NotificationRow.conversation_id, NotificationRow.notification_type)
        .where(NotificationRow.user_email == user_email)
        .where(NotificationRow.conversation_id.in_(ids))
        .where(NotificationRow.status == "pending")
        .order_by(NotificationRow.created_at.asc(), NotificationRow.notification_id.asc())
    )
    pending: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for conversation_id, notification_type in result.all():
        type_seen = seen.setdefault(conversation_id, set())
        if notification_type in type_seen:
            continue
        type_seen.add(notification_type)
        pending.setdefault(conversation_id, []).append(notification_type)
    return pending


async def get_latest_active_conversation_for_agent(
    session: AsyncSession,
    user_email: str,
    agent_id: str,
    *,
    context_type: str | None = None,
    include_agent_direct: bool = False,
) -> Conversation | None:
    """Return the most recent active conversation for one user/agent pair.

    When *context_type* is provided the query is further narrowed to
    conversations with a matching ``context_type`` column.
    """

    ordering_activity = sa.func.coalesce(
        Conversation.last_message_at,
        Conversation.created_at,
    )
    query = (
        select(Conversation)
        .where(Conversation.user_email == user_email)
        .where(Conversation.agent_id == agent_id)
        .where(Conversation.status == "active")
    )
    if context_type is not None:
        query = query.where(Conversation.context_type == context_type)
    if not include_agent_direct:
        query = query.where(_exclude_agent_direct_clause())
    query = query.order_by(
        ordering_activity.desc(),
        Conversation.created_at.desc(),
        Conversation.conversation_id.asc(),
    ).limit(1)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def get_agent_direct_conversation(
    session: AsyncSession,
    user_email: str,
    agent_id: str,
) -> Conversation | None:
    """Return the active sticky web direct chat for one user/agent pair."""

    has_sessions = (
        select(sa.literal(1))
        .where(Session.conversation_id == Conversation.conversation_id)
        .limit(1)
        .exists()
    )
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_email == user_email)
        .where(Conversation.agent_id == agent_id)
        .where(Conversation.status == "active")
        .where(_agent_direct_clause(user_email, agent_id))
        .order_by(
            case((has_sessions, 1), else_=0).desc(),
            sa.func.coalesce(
                Conversation.last_message_at, Conversation.updated_at, Conversation.created_at
            ).desc(),
            Conversation.created_at.desc(),
            Conversation.conversation_id.asc(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_managed_conversation_link(
    session: AsyncSession,
    *,
    user_email: str,
    controller_agent_id: str,
    controller_conversation_id: str,
    controller_session_id: str,
    target_agent_id: str,
    target_conversation_id: str,
    target_session_id: str,
    title: str,
    turn_state: str = "idle",
    notify_on_completion: bool = False,
) -> ManagedConversationLink:
    """Create a durable controller-to-target managed conversation link."""

    row = ManagedConversationLink(
        user_email=user_email,
        controller_agent_id=controller_agent_id,
        controller_conversation_id=controller_conversation_id,
        controller_session_id=controller_session_id,
        target_agent_id=target_agent_id,
        target_conversation_id=target_conversation_id,
        target_session_id=target_session_id,
        title=title,
        conversation_state="open",
        turn_state=turn_state,
        notify_on_completion=notify_on_completion,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_managed_conversation_link(
    session: AsyncSession,
    link_id: str,
    *,
    user_email: str | None = None,
) -> ManagedConversationLink | None:
    """Return a managed conversation link by ID."""

    query = select(ManagedConversationLink).where(ManagedConversationLink.link_id == link_id)
    if user_email is not None:
        query = query.where(ManagedConversationLink.user_email == user_email)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def get_managed_conversation_link_for_target(
    session: AsyncSession,
    target_conversation_id: str,
    *,
    user_email: str | None = None,
) -> ManagedConversationLink | None:
    """Return the latest managed conversation link for a target conversation."""

    query = (
        select(ManagedConversationLink)
        .where(ManagedConversationLink.target_conversation_id == target_conversation_id)
        .order_by(ManagedConversationLink.created_at.desc(), ManagedConversationLink.link_id.asc())
        .limit(1)
    )
    if user_email is not None:
        query = query.where(ManagedConversationLink.user_email == user_email)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def list_managed_conversation_links(
    session: AsyncSession,
    *,
    user_email: str,
    controller_conversation_id: str | None = None,
    status: str | None = None,
    limit: int = 25,
) -> list[ManagedConversationLink]:
    """List managed conversation links for one user."""

    query = select(ManagedConversationLink).where(ManagedConversationLink.user_email == user_email)
    if controller_conversation_id is not None:
        query = query.where(
            ManagedConversationLink.controller_conversation_id == controller_conversation_id
        )
    if status is not None and status != "all":
        query = query.where(
            sa.or_(
                ManagedConversationLink.conversation_state == status,
                ManagedConversationLink.turn_state == status,
            )
        )
    query = query.order_by(
        ManagedConversationLink.updated_at.desc(),
        ManagedConversationLink.created_at.desc(),
    ).limit(max(1, min(limit, 100)))
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_managed_conversation_link(
    session: AsyncSession,
    link_id: str,
    *,
    conversation_state: str | None = None,
    turn_state: str | None = None,
    target_session_id: str | None = None,
    active_turn_id: str | None = None,
    clear_active_turn_id: bool = False,
    notify_on_completion: bool | None = None,
    last_result_summary: str | None | _UnsetValue = _UNSET,
    last_error: str | None | _UnsetValue = _UNSET,
    control_metadata: dict[str, Any] | None = None,
    completed: bool = False,
    closed: bool = False,
) -> ManagedConversationLink | None:
    """Update managed conversation lifecycle state."""

    row = await get_managed_conversation_link(session, link_id)
    if row is None:
        return None
    if conversation_state is not None:
        row.conversation_state = conversation_state
    if turn_state is not None:
        row.turn_state = turn_state
    if target_session_id is not None:
        row.target_session_id = target_session_id
    if clear_active_turn_id:
        row.active_turn_id = None
    if active_turn_id is not None:
        row.active_turn_id = active_turn_id
    if notify_on_completion is not None:
        row.notify_on_completion = notify_on_completion
    if not isinstance(last_result_summary, _UnsetValue):
        row.last_result_summary = cast(str | None, last_result_summary)
    if not isinstance(last_error, _UnsetValue):
        row.last_error = cast(str | None, last_error)
    if control_metadata is not None:
        row.control_metadata = control_metadata
    if completed:
        row.completed_at = datetime.now(UTC)
    if closed:
        row.closed_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(row)
    return row


async def update_conversation(
    session: AsyncSession,
    conversation_id: str,
    *,
    title: str | None = None,
    title_source: str | None = None,
    project_id: str | None = None,
) -> bool:
    """Update mutable conversation fields."""
    row = await get_conversation(session, conversation_id)
    if row is None:
        return False
    if title is not None:
        row.title = title
    if title_source is not None:
        row.title_source = title_source
    if project_id is not None:
        row.project_id = project_id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def update_conversation_context_data(
    session: AsyncSession,
    conversation_id: str,
    *,
    context_data: dict[str, object],
) -> bool:
    """Replace conversation context_data with the provided payload."""

    row = await get_conversation(session, conversation_id)
    if row is None:
        return False
    row.context_data = dict(context_data)
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def mark_conversation_agent_direct(
    session: AsyncSession,
    conversation_id: str,
    *,
    user_email: str,
    agent_id: str,
) -> bool:
    """Mark an existing web conversation as the sticky direct chat."""

    row = await get_conversation(session, conversation_id)
    if row is None:
        return False
    existing = await get_agent_direct_conversation(session, user_email, agent_id)
    if existing is not None and existing.conversation_id != conversation_id:
        existing.status = "archived"
        existing.updated_at = datetime.now(UTC)
    context_data = dict(row.context_data or {})
    context_data["kind"] = "agent_direct"
    row.context_type = "web"
    row.context_ref = agent_direct_context_ref(user_email, agent_id)
    row.context_data = context_data
    row.title_source = "agent_direct"
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


HISTORY_REBASE_CONTEXT_KEY = "history_rebase"


async def set_conversation_history_rebase_metadata(
    session: AsyncSession,
    conversation_id: str,
    metadata: dict[str, object],
) -> bool:
    """Set redo metadata while preserving unrelated conversation context data."""

    row = await get_conversation(session, conversation_id)
    if row is None:
        return False
    context_data = dict(row.context_data or {})
    context_data[HISTORY_REBASE_CONTEXT_KEY] = dict(metadata)
    row.context_data = context_data
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def clear_conversation_history_rebase_metadata(
    session: AsyncSession,
    conversation_id: str,
) -> bool:
    """Clear redo metadata while preserving unrelated conversation context data."""

    row = await get_conversation(session, conversation_id)
    if row is None:
        return False
    context_data = dict(row.context_data or {})
    if HISTORY_REBASE_CONTEXT_KEY not in context_data:
        return True
    context_data.pop(HISTORY_REBASE_CONTEXT_KEY, None)
    row.context_data = context_data
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
    session: AsyncSession, conversation_id: str, active_session_id: str | None
) -> bool:
    """Set the active session ID for a conversation."""

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    conversation.active_session_id = active_session_id
    conversation.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def get_latest_root_session_for_conversation(
    session: AsyncSession,
    conversation_id: str,
) -> Session | None:
    """Return the newest root session for a conversation.

    Used when an archived conversation no longer has an active session pointer but
    still needs its readable history bootstrapped from the most recent root-session
    lineage.
    """

    result = await session.execute(
        select(Session)
        .where(
            Session.conversation_id == conversation_id,
            Session.parent_session_id.is_(None),
        )
        .order_by(Session.started_at.desc(), Session.session_id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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


async def set_conversation_active_executor(
    session: AsyncSession,
    conversation_id: str,
    active_executor_id: str | None,
    *,
    assigned_at: datetime | None = None,
    expires_at: datetime | None = None,
    source: str | None = None,
) -> bool:
    """Set the conversation-level active executor ID (Stage 36).

    Used by the ``switch_executor`` controller tool, the ``/executor``
    slash command, and the controller's one-time initial pick when the
    conversation first needs an executor.
    """

    conversation = await get_conversation(session, conversation_id)
    if conversation is None:
        return False
    timestamp = assigned_at or datetime.now(UTC)
    conversation.active_executor_id = active_executor_id
    conversation.active_executor_assigned_at = timestamp if active_executor_id else None
    conversation.active_executor_expires_at = expires_at if active_executor_id else None
    conversation.active_executor_source = source if active_executor_id else None
    conversation.updated_at = timestamp
    await session.flush()
    return True


async def initialize_conversation_active_executor(
    session: AsyncSession,
    conversation_id: str,
    active_executor_id: str,
    *,
    assigned_at: datetime | None = None,
    expires_at: datetime | None = None,
    source: str = "initial",
) -> bool:
    """Set the active executor only if it is currently unset (Stage 36).

    The controller is allowed to make exactly one such initial pick per
    conversation. After that, only ``switch_executor`` / ``/executor``
    may change the binding.
    """

    timestamp = assigned_at or datetime.now(UTC)
    result = await session.execute(
        update(Conversation)
        .where(
            Conversation.conversation_id == conversation_id,
            Conversation.active_executor_id.is_(None),
        )
        .values(
            active_executor_id=active_executor_id,
            active_executor_assigned_at=timestamp,
            active_executor_expires_at=expires_at,
            active_executor_source=source,
            updated_at=timestamp,
        )
    )
    return bool(result.rowcount)


async def reset_conversation_active_executor(
    session: AsyncSession,
    conversation_id: str,
) -> bool:
    """Clear the active executor pin and lifecycle metadata for a conversation."""

    return await set_conversation_active_executor(session, conversation_id, None)


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


async def mark_conversation_unread(
    session: AsyncSession, conversation_id: str, when: datetime | None = None
) -> bool:
    """Mark a conversation as unread by advancing its activity timestamp."""

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


async def list_conversation_sessions(
    session: AsyncSession,
    conversation_id: str,
    *,
    parent_session_id: str | None = None,
    parent_only: bool | None = None,
    root_only: bool = False,
    statuses: list[str] | None = None,
    order: str = "asc",
    limit: int | None = None,
) -> list[Session]:
    """List sessions for a conversation with optional bounded filters."""

    stmt = select(Session).where(Session.conversation_id == conversation_id)
    if parent_session_id is not None:
        stmt = stmt.where(Session.parent_session_id == parent_session_id)
    elif parent_only is True:
        stmt = stmt.where(Session.parent_session_id.is_not(None))
    elif root_only:
        stmt = stmt.where(Session.parent_session_id.is_(None))
    if statuses:
        stmt = stmt.where(Session.status.in_(statuses))
    if order == "desc":
        stmt = stmt.order_by(Session.started_at.desc(), Session.session_id.desc())
    else:
        stmt = stmt.order_by(Session.started_at, Session.session_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_conversation_intaris_session_ids(
    session: AsyncSession, conversation_id: str
) -> list[str]:
    """Return Intaris session identifiers for all sessions in one conversation."""

    rows = await list_conversation_sessions(session, conversation_id)
    return [row.intaris_session_id or row.session_id for row in rows]


async def list_sessions_by_intaris_session_ids(
    session: AsyncSession,
    session_ids: list[str],
) -> list[Session]:
    """Resolve Intaris search session IDs back to Cognis session rows."""

    ids = [item for item in dict.fromkeys(session_ids) if item]
    if not ids:
        return []
    result = await session.execute(
        select(Session)
        .where(sa.or_(Session.intaris_session_id.in_(ids), Session.session_id.in_(ids)))
        .order_by(Session.started_at, Session.session_id)
    )
    return list(result.scalars().all())


async def get_root_session_chain(
    session: AsyncSession,
    conversation_id: str,
    active_session_id: str,
    *,
    max_depth: int = 1000,
) -> tuple[list[Session], bool]:
    """Walk the root-session lineage backwards via ``previous_session_id``.

    Returns ``(chain, truncated)`` where *chain* is a list of session
    rows ordered oldest-first (the active session is last) and
    *truncated* is ``True`` when the lineage exceeded *max_depth*.
    Only follows root sessions (``parent_session_id IS NULL``).
    Uses a visited set for cycle detection.
    """

    chain: list[Session] = []
    visited: set[str] = set()
    current_id: str | None = active_session_id
    truncated = False

    while current_id and len(chain) < max_depth:
        if current_id in visited:
            break
        visited.add(current_id)
        row = await get_session_row(session, current_id)
        if row is None:
            break
        if row.conversation_id != conversation_id:
            break
        # Only follow root sessions (skip delegation sub-sessions)
        if row.parent_session_id is not None:
            break
        chain.append(row)
        current_id = row.previous_session_id

    if current_id and current_id not in visited:
        truncated = True

    chain.reverse()
    return chain, truncated


async def get_root_session_chain_page(
    session: AsyncSession,
    conversation_id: str,
    active_session_id: str,
    *,
    before_session_id: str | None = None,
    max_depth: int = 1000,
) -> tuple[list[Session], bool]:
    """Return root-session lineage up to an optional upper-bound session.

    The returned chain is oldest-first and excludes ``before_session_id``.
    This supports latest-first history pages without loading session events
    from the whole lineage.
    """

    chain: list[Session] = []
    visited: set[str] = set()
    current_id: str | None = active_session_id
    truncated = False
    include_current = before_session_id is None

    while current_id and len(chain) < max_depth:
        if current_id in visited:
            break
        visited.add(current_id)
        row = await get_session_row(session, current_id)
        if row is None:
            break
        if row.conversation_id != conversation_id:
            break
        if row.parent_session_id is not None:
            break
        if before_session_id is not None and row.session_id == before_session_id:
            include_current = True
        if include_current:
            chain.append(row)
        current_id = row.previous_session_id

    if current_id and current_id not in visited:
        truncated = True

    chain.reverse()
    return chain, truncated


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
    result_content: str | None = None,
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
    if result_content is not None:
        session_row.result_content = result_content
    if completion_reason is not None:
        session_row.completion_reason = completion_reason
    session_row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def update_session_status(
    session: AsyncSession,
    session_id: str,
    status: str,
    **kwargs: object,
) -> bool:
    """Backward-compatible alias for updating session lifecycle state."""

    return await set_session_status(session, session_id, status, **kwargs)


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


async def set_session_active(session: AsyncSession, session_id: str) -> bool:
    """Mark a session active again after it was idle."""

    session_row = await get_session_row(session, session_id)
    if session_row is None:
        return False
    session_row.status = "active"
    session_row.idle_since = None
    session_row.updated_at = datetime.now(UTC)
    await session.flush()
    return True


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


async def get_credential_row(
    session: AsyncSession, user_email: str, credential_id: str
) -> CredentialRow | None:
    result = await session.execute(
        select(CredentialRow).where(
            CredentialRow.user_email == user_email,
            CredentialRow.credential_id == credential_id,
        )
    )
    return result.scalar_one_or_none()


async def list_credential_rows(session: AsyncSession, user_email: str) -> list[CredentialRow]:
    result = await session.execute(
        select(CredentialRow)
        .where(CredentialRow.user_email == user_email)
        .order_by(CredentialRow.label, CredentialRow.credential_id)
    )
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
    created_by_agent_id: str | None = None,
    source_type: str = "api",
    source_ref: str | None = None,
    delivery_mode: str = "same_conversation",
    delivery_target: str | None = None,
    completion_mode_family: str = "default",
    allow_silent_completion: bool = False,
    interaction_mode_override: str | None = None,
    session_policy: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    project_id: str | None = None,
    workspace_root: str | None = None,
    working_directory: str | None = None,
    workflow_state: dict[str, object] | None = None,
    queue_name: str = "default",
    scheduled_for: datetime | None = None,
    task_id: str | None = None,
) -> Task:
    """Create a new task."""
    if working_directory and not workspace_root:
        workspace_root = working_directory
    if workspace_root and not working_directory:
        working_directory = workspace_root
    _validate_task_execution_paths(workspace_root, working_directory)
    row = Task(
        task_id=task_id or f"task_{uuid.uuid4().hex}",
        title=title,
        description=description,
        expected_output=expected_output,
        status=status,
        priority=priority,
        created_by=created_by,
        agent_id=agent_id,
        created_by_agent_id=created_by_agent_id,
        source_type=source_type,
        source_ref=source_ref,
        delivery_mode=delivery_mode,
        delivery_target=delivery_target,
        completion_mode_family=completion_mode_family,
        allow_silent_completion=allow_silent_completion,
        interaction_mode_override=interaction_mode_override,
        session_policy=session_policy,
        workflow_id=workflow_id,
        project_id=project_id,
        workspace_root=workspace_root,
        working_directory=working_directory,
        workflow_state=workflow_state,
        queue_name=queue_name,
        scheduled_for=scheduled_for,
    )
    session.add(row)
    await session.flush()
    return row


def _validate_task_execution_paths(
    workspace_root: str | None, working_directory: str | None
) -> None:
    if not workspace_root or not working_directory:
        return
    root = Path(os.path.realpath(os.path.expanduser(workspace_root)))
    cwd = Path(os.path.realpath(os.path.expanduser(working_directory)))
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError("working_directory must be equal to or inside workspace_root") from exc


async def get_task(session: AsyncSession, task_id: str) -> Task | None:
    """Get a task by ID."""
    result = await session.execute(select(Task).where(Task.task_id == task_id))
    return result.scalar_one_or_none()


async def set_task_active_executor(
    session: AsyncSession,
    task_id: str,
    active_executor_id: str | None,
    *,
    assigned_at: datetime | None = None,
    expires_at: datetime | None = None,
    source: str | None = None,
) -> bool:
    """Set the task-level active executor ID (Stage 36).

    Used by the ``switch_executor`` controller tool and the ``/executor``
    slash command when invoked from a task-step conversation, so the
    binding carries forward to subsequent steps of the same task.
    """

    task = await get_task(session, task_id)
    if task is None:
        return False
    timestamp = assigned_at or datetime.now(UTC)
    task.active_executor_id = active_executor_id
    task.active_executor_assigned_at = timestamp if active_executor_id else None
    task.active_executor_expires_at = expires_at if active_executor_id else None
    task.active_executor_source = source if active_executor_id else None
    task.updated_at = timestamp
    await session.flush()
    return True


async def initialize_task_active_executor(
    session: AsyncSession,
    task_id: str,
    active_executor_id: str,
    *,
    assigned_at: datetime | None = None,
    expires_at: datetime | None = None,
    source: str = "initial",
) -> bool:
    """Set the task active executor only if it is currently unset (Stage 36).

    The controller is allowed to make exactly one such initial pick per task.
    After that, only ``switch_executor`` / ``/executor`` may change it.
    """

    timestamp = assigned_at or datetime.now(UTC)
    result = await session.execute(
        update(Task)
        .where(
            Task.task_id == task_id,
            Task.active_executor_id.is_(None),
        )
        .values(
            active_executor_id=active_executor_id,
            active_executor_assigned_at=timestamp,
            active_executor_expires_at=expires_at,
            active_executor_source=source,
            updated_at=timestamp,
        )
    )
    return bool(result.rowcount)


async def create_task_comment(
    session: AsyncSession,
    *,
    task_id: str,
    author_email: str,
    body: str,
    intent: str = "record_only",
    noop: bool = True,
    target_step: str | None = None,
    attempt_number: int = 1,
    metadata: dict[str, object] | None = None,
) -> TaskCommentRow:
    row = TaskCommentRow(
        comment_id=f"tcmt_{uuid.uuid4().hex}",
        task_id=task_id,
        author_email=author_email,
        body=body,
        intent=intent,
        noop=noop,
        target_step=target_step,
        attempt_number=attempt_number,
        metadata_json=metadata or {},
    )
    session.add(row)
    await session.flush()
    return row


async def list_task_comments(session: AsyncSession, task_id: str) -> list[TaskCommentRow]:
    result = await session.execute(
        select(TaskCommentRow)
        .where(TaskCommentRow.task_id == task_id)
        .order_by(TaskCommentRow.created_at.asc(), TaskCommentRow.comment_id.asc())
    )
    return list(result.scalars().all())


async def claim_pending_context_task_comments(
    session: AsyncSession,
    *,
    task_id: str,
    step_name: str,
    attempt_number: int,
    step_run_id: str | None,
    reason: str,
) -> list[TaskCommentRow]:
    """Claim pending context-only task comments for live workflow-step injection."""

    result = await session.execute(
        select(TaskCommentRow)
        .where(
            TaskCommentRow.task_id == task_id,
            TaskCommentRow.intent == "context_only",
            TaskCommentRow.applied.is_(False),
            TaskCommentRow.attempt_number == attempt_number,
            sa.or_(
                TaskCommentRow.target_step.is_(None),
                TaskCommentRow.target_step == "",
                TaskCommentRow.target_step == step_name,
            ),
        )
        .order_by(TaskCommentRow.created_at.asc(), TaskCommentRow.comment_id.asc())
    )
    rows = list(result.scalars().all())
    claimed_at = _utcnow()
    claimed: list[TaskCommentRow] = []
    for row in rows:
        metadata = dict(row.metadata_json or {})
        metadata.update(
            {
                "applied_at": claimed_at.isoformat(),
                "applied_reason": reason,
                "applied_step": step_name,
            }
        )
        if step_run_id:
            metadata["applied_step_run_id"] = step_run_id
        claim_result = await session.execute(
            update(TaskCommentRow)
            .where(
                TaskCommentRow.comment_id == row.comment_id,
                TaskCommentRow.applied.is_(False),
            )
            .values(
                applied=True,
                metadata_json=metadata,
                updated_at=claimed_at,
            )
        )
        if int(getattr(claim_result, "rowcount", 0) or 0) <= 0:
            continue
        row.applied = True
        row.metadata_json = metadata
        row.updated_at = claimed_at
        claimed.append(row)
    await session.flush()
    return claimed


async def get_task_comment(session: AsyncSession, comment_id: str) -> TaskCommentRow | None:
    result = await session.execute(
        select(TaskCommentRow).where(TaskCommentRow.comment_id == comment_id)
    )
    return result.scalar_one_or_none()


async def update_task_comment(
    session: AsyncSession,
    comment_id: str,
    **fields: Any,
) -> TaskCommentRow | None:
    row = await get_task_comment(session, comment_id)
    if row is None:
        return None
    for key, value in fields.items():
        if key == "metadata":
            row.metadata_json = value
        elif key in {"body", "intent", "noop", "target_step", "applied"}:
            setattr(row, key, value)
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def update_task_status(
    session: AsyncSession,
    task_id: str,
    status: str,
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    result_summary: str | None = None,
    result_data: dict[str, object] | None = None,
    applied_completion_mode: str | None = None,
    applied_completion_reason: str | None = None,
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
    if applied_completion_mode is not None:
        values["applied_completion_mode"] = applied_completion_mode
    if applied_completion_reason is not None:
        values["applied_completion_reason"] = applied_completion_reason

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
    project_id: str | None = None,
    session_policy: dict[str, Any] | None = None,
    clear_workflow_id: bool = False,
    clear_project_id: bool = False,
) -> bool:
    """Update mutable task fields.  Only allowed for draft/queued tasks.

    Pass ``clear_workflow_id=True`` / ``clear_project_id=True`` to explicitly
    set the corresponding column to ``NULL``.
    """
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
    elif clear_workflow_id:
        values["workflow_id"] = None
    if project_id is not None:
        values["project_id"] = project_id
    elif clear_project_id:
        values["project_id"] = None
    if session_policy is not None:
        values["session_policy"] = session_policy
    if not values:
        return False
    stmt = (
        update(Task)
        .where(Task.task_id == task_id, Task.status.in_(["draft", "queued"]))
        .values(**values)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def update_task_execution_paths(
    session: AsyncSession,
    task_id: str,
    *,
    workspace_root: str | None,
    working_directory: str | None,
) -> bool:
    """Persist resolved execution paths for any task lifecycle state."""

    if working_directory and not workspace_root:
        workspace_root = working_directory
    if workspace_root and not working_directory:
        working_directory = workspace_root
    _validate_task_execution_paths(workspace_root, working_directory)
    result = await session.execute(
        update(Task)
        .where(Task.task_id == task_id)
        .values(
            workspace_root=workspace_root,
            working_directory=working_directory,
            updated_at=_utcnow(),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_tasks_for_agent(
    session: AsyncSession,
    agent_id: str,
    *,
    statuses: list[str] | None = None,
    limit: int = 50,
) -> list[Task]:
    """List tasks owned by or created from a specific agent.

    Optionally filters by task status.
    """
    query = (
        select(Task)
        .where(sa.or_(Task.agent_id == agent_id, Task.created_by_agent_id == agent_id))
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
# Deliverables
# ---------------------------------------------------------------------------


async def create_deliverable(
    session: AsyncSession,
    *,
    step_run_id: str,
    content: str,
    format: str = "markdown",
    title: str | None = None,
    target: str | None = None,
    outputs: dict[str, Any] | None = None,
    deliverable_id: str | None = None,
    attempt_number: int | None = None,
) -> DeliverableRow:
    """Create a new versioned deliverable for a step run."""

    version_result = await session.execute(
        select(sa.func.max(DeliverableRow.version)).where(DeliverableRow.step_run_id == step_run_id)
    )
    next_version = int(version_result.scalar_one_or_none() or 0) + 1
    if attempt_number is None:
        step_run = await session.get(StepRun, step_run_id)
        attempt_number = getattr(step_run, "attempt_number", 1) if step_run is not None else 1

    await session.execute(
        update(DeliverableRow)
        .where(
            DeliverableRow.step_run_id == step_run_id,
            DeliverableRow.status.in_(["buffered", "approved"]),
        )
        .values(status="superseded", updated_at=_utcnow())
    )

    row = DeliverableRow(
        deliverable_id=deliverable_id or f"dlv_{uuid.uuid4().hex}",
        step_run_id=step_run_id,
        version=next_version,
        attempt_number=attempt_number,
        content=content,
        format=format,
        title=title,
        target=target,
        outputs=outputs,
        status="buffered",
    )
    session.add(row)
    await session.flush()
    return row


async def get_deliverable(session: AsyncSession, deliverable_id: str) -> DeliverableRow | None:
    """Get a deliverable by ID."""

    result = await session.execute(
        select(DeliverableRow).where(DeliverableRow.deliverable_id == deliverable_id)
    )
    return result.scalar_one_or_none()


async def list_deliverables_for_step_run(
    session: AsyncSession,
    step_run_id: str,
) -> list[DeliverableRow]:
    """List deliverable versions for a step run, newest first."""

    result = await session.execute(
        select(DeliverableRow)
        .where(DeliverableRow.step_run_id == step_run_id)
        .order_by(DeliverableRow.version.desc(), DeliverableRow.created_at.desc())
    )
    return list(result.scalars().all())


async def get_latest_active_deliverable_for_step_run(
    session: AsyncSession,
    step_run_id: str,
) -> DeliverableRow | None:
    """Return the latest non-rejected, non-superseded deliverable for a step run."""

    result = await session.execute(
        select(DeliverableRow)
        .where(
            DeliverableRow.step_run_id == step_run_id,
            DeliverableRow.status.in_(["buffered", "approved", "delivered"]),
        )
        .order_by(DeliverableRow.version.desc(), DeliverableRow.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def update_deliverable_status(
    session: AsyncSession,
    deliverable_id: str,
    *,
    status: str,
    evaluator_feedback: str | None | object = _UNSET,
) -> bool:
    """Update a deliverable lifecycle state."""

    values: dict[str, object] = {"status": status, "updated_at": _utcnow()}
    if evaluator_feedback is not _UNSET:
        values["evaluator_feedback"] = evaluator_feedback
    result = await session.execute(
        update(DeliverableRow)
        .where(DeliverableRow.deliverable_id == deliverable_id)
        .values(**values)
    )
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def get_latest_approved_deliverable_for_step_run(
    session: AsyncSession,
    step_run_id: str,
) -> DeliverableRow | None:
    """Return the latest approved deliverable for a step run."""

    result = await session.execute(
        select(DeliverableRow)
        .where(
            DeliverableRow.step_run_id == step_run_id,
            DeliverableRow.status == "approved",
        )
        .order_by(DeliverableRow.version.desc(), DeliverableRow.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_rejected_deliverable_for_step_run(
    session: AsyncSession,
    step_run_id: str,
) -> DeliverableRow | None:
    """Return the latest rejected deliverable for a step run."""

    result = await session.execute(
        select(DeliverableRow)
        .where(
            DeliverableRow.step_run_id == step_run_id,
            DeliverableRow.status == "rejected",
        )
        .order_by(DeliverableRow.version.desc(), DeliverableRow.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
    attempt_number: int = 1,
    step_run_id: str | None = None,
    conversation_id: str | None = None,
    workspace_root: str | None = None,
    working_directory: str | None = None,
    deliverable_id: str | None = None,
    require_deliverable: bool | None = None,
    runtime_info: dict[str, object] | None = None,
    status: str = "pending",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> StepRun:
    """Create a new step run record."""
    row = StepRun(
        step_run_id=step_run_id or f"sr_{uuid.uuid4().hex}",
        task_id=task_id,
        step_name=step_name,
        step_type=step_type,
        agent_id=agent_id,
        attempt=attempt,
        attempt_number=attempt_number,
        workspace_root=workspace_root,
        working_directory=working_directory,
        conversation_id=conversation_id,
        deliverable_id=deliverable_id,
        require_deliverable=require_deliverable,
        runtime_info=runtime_info,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )
    session.add(row)
    await session.flush()
    return row


_VALID_STEP_RUN_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "cancelled"},
    "running": {"evaluating", "approved", "rejected", "failed", "paused", "cancelled"},
    "evaluating": {"approved", "rejected", "failed"},
    "approved": {"running"},
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
    attempt: int | object = _UNSET,
    conversation_id: str | None | object = _UNSET,
    session_id: str | None | object = _UNSET,
    intaris_session_id: str | None | object = _UNSET,
    workspace_root: str | None | object = _UNSET,
    working_directory: str | None | object = _UNSET,
    deliverable_id: str | None | object = _UNSET,
    require_deliverable: bool | None | object = _UNSET,
    output: dict[str, object] | None | object = _UNSET,
    evaluation: dict[str, object] | None | object = _UNSET,
    todos: list[dict[str, object]] | None | object = _UNSET,
    runtime_info: dict[str, object] | None | object = _UNSET,
    started_at: datetime | None | object = _UNSET,
    completed_at: datetime | None | object = _UNSET,
) -> bool:
    """Update step run fields.

    When *status* is provided, the update uses a CAS guard to enforce
    valid state transitions.  Invalid transitions are silently rejected
    (returns ``False``).
    """
    values: dict[str, object] = {}
    if status is not None:
        values["status"] = status
    if attempt is not _UNSET:
        values["attempt"] = attempt
    if conversation_id is not _UNSET:
        values["conversation_id"] = conversation_id
    if session_id is not _UNSET:
        values["session_id"] = session_id
    if intaris_session_id is not _UNSET:
        values["intaris_session_id"] = intaris_session_id
    if workspace_root is not _UNSET:
        values["workspace_root"] = workspace_root
    if working_directory is not _UNSET:
        values["working_directory"] = working_directory
    if deliverable_id is not _UNSET:
        values["deliverable_id"] = deliverable_id
    if require_deliverable is not _UNSET:
        values["require_deliverable"] = require_deliverable
    if output is not _UNSET:
        values["output"] = output
    if evaluation is not _UNSET:
        values["evaluation"] = evaluation
    if todos is not _UNSET:
        values["todos"] = todos
    if runtime_info is not _UNSET:
        values["runtime_info"] = runtime_info
    if started_at is not _UNSET:
        values["started_at"] = started_at
    if completed_at is not _UNSET:
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


async def list_step_run_history(
    session: AsyncSession,
    task_id: str,
    step_name: str,
) -> list[StepRun]:
    """List all attempts for one task step."""

    result = await session.execute(
        select(StepRun)
        .where(StepRun.task_id == task_id, StepRun.step_name == step_name)
        .order_by(StepRun.attempt_number.asc(), StepRun.attempt.asc(), StepRun.started_at.asc())
    )
    return list(result.scalars().all())


async def supersede_step_runs_for_revision(
    session: AsyncSession,
    task_id: str,
    step_names: list[str],
    *,
    before_attempt_number: int,
) -> int:
    """Mark historical step runs superseded when a human revision rewinds a task."""

    if not step_names:
        return 0
    result = await session.execute(
        update(StepRun)
        .where(
            StepRun.task_id == task_id,
            StepRun.step_name.in_(step_names),
            StepRun.attempt_number < before_attempt_number,
            StepRun.status.in_(
                ["pending", "running", "evaluating", "approved", "rejected", "paused", "failed"]
            ),
        )
        .values(status="superseded", updated_at=_utcnow())
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def get_step_run(session: AsyncSession, step_run_id: str) -> StepRun | None:
    """Get a step run by ID."""
    result = await session.execute(select(StepRun).where(StepRun.step_run_id == step_run_id))
    return result.scalar_one_or_none()


async def get_latest_step_run_for_task_step(
    session: AsyncSession,
    task_id: str,
    step_name: str,
    *,
    attempt_number: int | None = None,
) -> StepRun | None:
    """Return the most recent step run for a given task and step name.

    Ordered by ``attempt`` descending with deterministic tiebreakers
    (``started_at``, ``step_run_id``) so the latest attempt is returned
    even when duplicate attempt numbers exist.
    Used by the workflow engine to reuse a prior session on retry.
    """
    stmt = select(StepRun).where(StepRun.task_id == task_id, StepRun.step_name == step_name)
    if attempt_number is not None:
        stmt = stmt.where(StepRun.attempt_number == attempt_number)
    result = await session.execute(
        stmt.order_by(
            StepRun.attempt_number.desc(),
            StepRun.attempt.desc(),
            StepRun.started_at.desc(),
            StepRun.step_run_id.desc(),
        ).limit(1)
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


async def fail_orphaned_running_step_runs(
    session: AsyncSession,
    completed_at: datetime,
    *,
    final_status: str = "failed",
) -> int:
    """Finalize non-terminal step runs whose parent task is already terminal.

    Covers every non-terminal ``StepRun`` status (``running``, ``paused``,
    ``evaluating``, ``pending``) so a terminal task can never leave active
    step run rows behind to saturate queue capacity. Cancellation
    semantics are preserved: step runs under a ``cancelled`` parent are
    marked ``cancelled`` regardless of ``final_status``.
    """

    terminal_parent_statuses = ["failed", "completed", "cancelled"]
    non_terminal_step_statuses = ["running", "paused", "evaluating", "pending"]
    parent_status = select(Task.status).where(Task.task_id == StepRun.task_id).scalar_subquery()
    stmt = (
        update(StepRun)
        .where(
            StepRun.status.in_(non_terminal_step_statuses),
            StepRun.task_id.in_(
                select(Task.task_id).where(Task.status.in_(terminal_parent_statuses))
            ),
        )
        .values(
            status=sa.case(
                (parent_status == "cancelled", "cancelled"),
                else_=final_status,
            ),
            completed_at=completed_at,
        )
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
    lifecycle: str = "persistent",
    archived_at: datetime | None = None,
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
        lifecycle=lifecycle,
        archived_at=archived_at,
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
    include_ephemeral: bool = False,
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
    if not include_ephemeral:
        query = query.where(
            WorkflowRow.lifecycle == "persistent", WorkflowRow.archived_at.is_(None)
        )
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


async def get_system_workflow_override(
    session: AsyncSession, *, owner_email: str, workflow_id: str
) -> SystemWorkflowOverride | None:
    """Return the per-user override for a shipped system workflow."""

    result = await session.execute(
        select(SystemWorkflowOverride).where(
            SystemWorkflowOverride.owner_email == owner_email,
            SystemWorkflowOverride.workflow_id == workflow_id,
        )
    )
    return result.scalar_one_or_none()


async def list_system_workflow_overrides(
    session: AsyncSession, *, owner_email: str
) -> list[SystemWorkflowOverride]:
    """List all system-workflow overrides for a user."""

    result = await session.execute(
        select(SystemWorkflowOverride).where(SystemWorkflowOverride.owner_email == owner_email)
    )
    return list(result.scalars().all())


async def upsert_system_workflow_override(
    session: AsyncSession,
    *,
    owner_email: str,
    workflow_id: str,
    disabled: bool | None = None,
    step_overrides: dict[str, Any] | None = None,
) -> SystemWorkflowOverride:
    """Create or update a per-user system-workflow override row."""

    row = await get_system_workflow_override(
        session, owner_email=owner_email, workflow_id=workflow_id
    )
    if row is None:
        row = SystemWorkflowOverride(
            override_id=f"swo_{uuid.uuid4().hex[:12]}",
            owner_email=owner_email,
            workflow_id=workflow_id,
        )
        session.add(row)
    if disabled is not None:
        row.disabled = disabled
    row.step_overrides = step_overrides
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def delete_system_workflow_override(
    session: AsyncSession, *, owner_email: str, workflow_id: str
) -> bool:
    """Delete a per-user system-workflow override row."""

    result = await session.execute(
        delete(SystemWorkflowOverride).where(
            SystemWorkflowOverride.owner_email == owner_email,
            SystemWorkflowOverride.workflow_id == workflow_id,
        )
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_model_routing(
    session: AsyncSession, owner_email: str | None = None
) -> list[ModelRouting]:
    """List all model routing rows."""
    stmt = select(ModelRouting)
    if owner_email is not None:
        stmt = stmt.where(ModelRouting.owner_email == owner_email)
    result = await session.execute(
        stmt.order_by(ModelRouting.owner_email.asc(), ModelRouting.task_type.asc())
    )
    return list(result.scalars().all())


async def get_model_routing(
    session: AsyncSession, task_type: str, owner_email: str = SYSTEM_USER_EMAIL
) -> ModelRouting | None:
    """Get a model routing row by task type."""
    result = await session.execute(
        select(ModelRouting).where(
            ModelRouting.task_type == task_type,
            ModelRouting.owner_email == owner_email,
        )
    )
    return result.scalar_one_or_none()


async def upsert_model_routing(
    session: AsyncSession,
    *,
    task_type: str,
    provider_id: str | None,
    model: str,
    owner_email: str = SYSTEM_USER_EMAIL,
    config: dict[str, Any] | None = None,
) -> ModelRouting:
    """Create or update a model routing row."""
    existing = await get_model_routing(session, task_type, owner_email)
    if existing is not None:
        existing.provider_id = provider_id
        existing.model = model
        existing.config = config
        existing.updated_at = datetime.now(UTC)
        await session.flush()
        return existing
    row = ModelRouting(
        route_id=f"route_{uuid.uuid4().hex[:12]}",
        task_type=task_type,
        owner_email=owner_email,
        provider_id=provider_id,
        model=model,
        config=config,
    )
    session.add(row)
    await session.flush()
    return row


async def delete_model_routing(
    session: AsyncSession, task_type: str, owner_email: str = SYSTEM_USER_EMAIL
) -> bool:
    """Delete a model routing row."""
    result = await session.execute(
        delete(ModelRouting).where(
            ModelRouting.task_type == task_type,
            ModelRouting.owner_email == owner_email,
        )
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def create_llm_provider_auth_session(
    session: AsyncSession,
    *,
    setup_id: str,
    provider_id: str,
    owner_email: str,
    actor_email: str,
    executor_id: str | None,
    credential_id: str,
    expires_at: datetime,
    status_payload: dict[str, Any] | None = None,
) -> LLMProviderAuthSession:
    row = LLMProviderAuthSession(
        setup_id=setup_id,
        provider_id=provider_id,
        owner_email=owner_email,
        actor_email=actor_email,
        executor_id=executor_id,
        state="awaiting_user",
        credential_id=credential_id,
        expires_at=expires_at,
        status_payload=status_payload or {},
    )
    session.add(row)
    await session.flush()
    return row


async def get_active_llm_provider_auth_session(
    session: AsyncSession, provider_id: str, owner_email: str
) -> LLMProviderAuthSession | None:
    result = await session.execute(
        select(LLMProviderAuthSession)
        .where(
            LLMProviderAuthSession.provider_id == provider_id,
            LLMProviderAuthSession.owner_email == owner_email,
            LLMProviderAuthSession.state.in_(
                [
                    "created",
                    "executor_starting",
                    "awaiting_user",
                    "authorizing",
                    "code_submitted",
                ]
            ),
        )
        .order_by(LLMProviderAuthSession.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_llm_provider_auth_session(
    session: AsyncSession, setup_id: str
) -> LLMProviderAuthSession | None:
    return await session.get(LLMProviderAuthSession, setup_id)


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


async def create_schedule(
    session: AsyncSession,
    *,
    schedule_id: str | None = None,
    name: str,
    description: str | None = None,
    schedule_type: str = "cron",
    cron_expr: str | None = None,
    interval_seconds: int | None = None,
    one_shot_at: datetime | None = None,
    timezone: str = "UTC",
    agent_id: str,
    workflow_id: str | None = None,
    project_id: str | None = None,
    skill_id: str | None = None,
    task_template: dict[str, object],
    enabled: bool = True,
    max_concurrent_runs: int = 1,
    delete_after_run: bool = False,
    completion_mode_family: str = "default",
    allow_silent_completion: bool = False,
    interaction_mode_override: str | None = "none",
    next_fire_at: datetime | None = None,
    created_by: str,
) -> Schedule:
    """Create a schedule record."""
    row = Schedule(
        schedule_id=schedule_id or f"sched_{uuid.uuid4().hex}",
        name=name,
        description=description,
        schedule_type=schedule_type,
        cron_expr=cron_expr,
        interval_seconds=interval_seconds,
        one_shot_at=one_shot_at,
        timezone=timezone,
        agent_id=agent_id,
        workflow_id=workflow_id,
        project_id=project_id,
        skill_id=skill_id,
        task_template=task_template,
        enabled=enabled,
        max_concurrent_runs=max_concurrent_runs,
        delete_after_run=delete_after_run,
        completion_mode_family=completion_mode_family,
        allow_silent_completion=allow_silent_completion,
        interaction_mode_override=interaction_mode_override,
        next_fire_at=next_fire_at,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def get_schedule(session: AsyncSession, schedule_id: str) -> Schedule | None:
    """Get a schedule by ID."""
    result = await session.execute(select(Schedule).where(Schedule.schedule_id == schedule_id))
    return result.scalar_one_or_none()


async def list_schedules(
    session: AsyncSession,
    *,
    created_by: str | None = None,
    enabled: bool | None = None,
    schedule_type: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
) -> list[Schedule]:
    """List schedules with optional filters."""
    stmt = select(Schedule).order_by(Schedule.name)
    if created_by is not None:
        stmt = stmt.where(Schedule.created_by == created_by)
    if enabled is not None:
        stmt = stmt.where(Schedule.enabled == enabled)
    if schedule_type is not None:
        stmt = stmt.where(Schedule.schedule_type == schedule_type)
    if agent_id is not None:
        stmt = stmt.where(Schedule.agent_id == agent_id)
    if project_id is not None:
        stmt = stmt.where(Schedule.project_id == project_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_schedule(
    session: AsyncSession,
    schedule_id: str,
    **fields: Any,
) -> Schedule | None:
    """Update mutable fields on a schedule. Returns the updated row or None."""
    row = await get_schedule(session, schedule_id)
    if row is None:
        return None
    allowed = {
        "name",
        "description",
        "schedule_type",
        "cron_expr",
        "interval_seconds",
        "one_shot_at",
        "timezone",
        "agent_id",
        "workflow_id",
        "project_id",
        "skill_id",
        "task_template",
        "enabled",
        "max_concurrent_runs",
        "delete_after_run",
        "completion_mode_family",
        "allow_silent_completion",
        "interaction_mode_override",
        "next_fire_at",
    }
    for key, value in fields.items():
        if key in allowed:
            setattr(row, key, value)
    row.updated_at = datetime.now(UTC)
    await session.flush()
    return row


async def delete_schedule(session: AsyncSession, schedule_id: str) -> bool:
    """Delete a schedule by ID."""
    result = await session.execute(delete(Schedule).where(Schedule.schedule_id == schedule_id))
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def list_due_schedules(session: AsyncSession, now: datetime) -> list[Schedule]:
    """Return enabled schedules whose next_fire_at is at or before *now*."""
    stmt = (
        select(Schedule)
        .where(
            Schedule.enabled.is_(True),
            Schedule.next_fire_at.isnot(None),
            Schedule.next_fire_at <= now,
        )
        .order_by(Schedule.next_fire_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_schedule_fire_state(
    session: AsyncSession,
    schedule_id: str,
    *,
    last_fired_at: datetime,
    next_fire_at: datetime | None,
    last_run_status: str,
    consecutive_errors: int,
    disabled_reason: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Atomically update schedule state after a fire attempt."""
    values: dict[str, Any] = {
        "last_fired_at": last_fired_at,
        "next_fire_at": next_fire_at,
        "last_run_status": last_run_status,
        "consecutive_errors": consecutive_errors,
        "updated_at": datetime.now(UTC),
    }
    if disabled_reason is not None:
        values["disabled_reason"] = disabled_reason
    if enabled is not None:
        values["enabled"] = enabled
    await session.execute(
        update(Schedule).where(Schedule.schedule_id == schedule_id).values(**values)
    )
    await session.flush()


async def count_active_tasks_for_schedule(session: AsyncSession, schedule_id: str) -> int:
    """Count running/queued/ready tasks created by a specific schedule."""
    result = await session.execute(
        select(sa.func.count())
        .select_from(Task)
        .where(
            Task.source_type == "scheduler",
            Task.source_ref == schedule_id,
            Task.status.in_(["queued", "ready", "running", "paused"]),
        )
    )
    return int(result.scalar_one())


async def count_active_schedules_for_project(
    session: AsyncSession,
    project_id: str,
    *,
    created_by: str | None = None,
) -> int:
    """Count currently active schedules bound to a project."""
    stmt = (
        select(sa.func.count())
        .select_from(Schedule)
        .where(
            Schedule.project_id == project_id,
            Schedule.enabled.is_(True),
            Schedule.next_fire_at.isnot(None),
        )
    )
    if created_by is not None:
        stmt = stmt.where(Schedule.created_by == created_by)
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def get_latest_schedule_task_runs(
    session: AsyncSession,
    schedule_ids: list[str],
    *,
    created_by: str,
) -> dict[str, tuple[str, datetime | None]]:
    """Return the newest scheduler-created task status per schedule."""
    if not schedule_ids:
        return {}

    ranked_runs = (
        select(
            Task.source_ref.label("schedule_id"),
            Task.status.label("status"),
            Task.created_at.label("created_at"),
            sa.func.row_number()
            .over(
                partition_by=Task.source_ref,
                order_by=(Task.created_at.desc(), Task.task_id.desc()),
            )
            .label("row_number"),
        )
        .where(
            Task.source_type == "scheduler",
            Task.source_ref.in_(schedule_ids),
            Task.created_by == created_by,
        )
        .subquery()
    )

    result = await session.execute(
        select(
            ranked_runs.c.schedule_id,
            ranked_runs.c.status,
            ranked_runs.c.created_at,
        ).where(ranked_runs.c.row_number == 1)
    )
    return {
        str(schedule_id): (str(status), created_at)
        for schedule_id, status, created_at in result.all()
        if schedule_id is not None and status is not None
    }


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
    linked_tool_ids: list[str] | None = None,
    prompt_templates: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    auto_load: bool = False,
    is_system: bool = False,
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
        linked_tool_ids=linked_tool_ids,
        prompt_templates=prompt_templates,
        tags=tags,
        auto_load=auto_load,
        is_system=is_system,
        source=source,
        owner_email=owner_email,
    )
    session.add(row)
    await session.flush()
    return row


async def update_skill(
    session: AsyncSession,
    skill_id: str,
    *,
    owner_email: str | None = None,
    **kwargs: Any,
) -> SkillRow | None:
    """Update a skill by ID.

    When ``owner_email`` is provided, only skills owned by that user or
    with ``source='db'``/``source='imported'`` can be updated.  Global
    skills (owner_email is None) cannot be mutated by non-owners.
    """
    if owner_email is not None:
        row = await get_skill_scoped(session, skill_id, owner_email=owner_email)
    else:
        row = await get_skill(session, skill_id)
    if row is None:
        return None
    if row.source not in ("db", "imported"):
        raise ValueError("Cannot update read-only skills")
    if row.is_system:
        raise ValueError("Cannot modify system skills directly")
    # Prevent non-owners from mutating global skills
    if owner_email is not None and row.owner_email is None:
        raise ValueError("Cannot modify global skills")
    for key, value in kwargs.items():
        if hasattr(row, key) and key != "owner_email":
            setattr(row, key, value)
    await session.flush()
    return row


async def reset_skill_to_defaults(
    session: AsyncSession,
    skill_id: str,
    *,
    name: str,
    description: str | None,
    instructions: str,
    tools: list[dict[str, Any]] | None,
    linked_tool_ids: list[str] | None,
    prompt_templates: dict[str, Any] | None,
    tags: list[str] | None,
    auto_load: bool,
) -> SkillRow | None:
    """Reset a system skill row to canonical default content."""

    row = await get_skill(session, skill_id)
    if row is None:
        return None
    row.name = name
    row.description = description
    row.instructions = instructions
    row.tools = tools
    row.linked_tool_ids = linked_tool_ids
    row.prompt_templates = prompt_templates
    row.tags = tags
    row.auto_load = auto_load
    row.is_system = True
    await session.flush()
    return row


async def delete_skill(
    session: AsyncSession, skill_id: str, *, owner_email: str | None = None
) -> bool:
    """Delete a skill by ID.

    Only DB-managed or imported skills can be deleted.  When
    ``owner_email`` is provided, global skills (owner_email is None)
    cannot be deleted by non-owners.
    """
    if owner_email is not None:
        row = await get_skill_scoped(session, skill_id, owner_email=owner_email)
    else:
        row = await get_skill(session, skill_id)
    if row is None:
        return False
    if row.source not in ("db", "imported"):
        raise ValueError("Cannot delete read-only skills")
    if row.is_system:
        raise ValueError("Cannot delete system skills")
    if owner_email is not None and row.owner_email is None:
        raise ValueError("Cannot delete global skills")
    await session.execute(delete(SkillRow).where(SkillRow.skill_id == skill_id))
    return True


async def get_skill_scoped(
    session: AsyncSession, skill_id: str, *, owner_email: str | None = None
) -> SkillRow | None:
    """Get a skill by ID with owner scoping.

    Returns the skill only if it belongs to the given owner or is global
    (owner_email is None).  This fixes the original unscoped get_skill.
    """
    stmt = select(SkillRow).where(SkillRow.skill_id == skill_id)
    if owner_email is not None:
        stmt = stmt.where(
            sa.or_(SkillRow.owner_email == owner_email, SkillRow.owner_email.is_(None))
        )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- Skill Versions ---


async def create_skill_version(
    session: AsyncSession,
    *,
    version_id: str | None = None,
    skill_id: str,
    version_number: int,
    content_hash: str,
    instructions: str,
    tools: list[dict[str, Any]] | None = None,
    linked_tool_ids: list[str] | None = None,
    prompt_templates: dict[str, Any] | None = None,
    secret_placeholders: list[str] | None = None,
    steps: list[dict[str, Any]] | None = None,
    decomposition_source_hash: str | None = None,
    source_url: str | None = None,
    resolved_url: str | None = None,
    commit_sha: str | None = None,
    import_checksum: str | None = None,
    imported_at: Any | None = None,
    import_format: str | None = None,
    asset_manifest: list[dict[str, Any]] | None = None,
    schema_version: int = 1,
) -> SkillVersionRow:
    """Create an immutable skill version record."""
    row = SkillVersionRow(
        version_id=version_id or f"sv_{uuid.uuid4().hex[:12]}",
        skill_id=skill_id,
        version_number=version_number,
        content_hash=content_hash,
        schema_version=schema_version,
        instructions=instructions,
        tools=tools,
        linked_tool_ids=linked_tool_ids,
        prompt_templates=prompt_templates,
        secret_placeholders=secret_placeholders,
        steps=steps,
        decomposition_source_hash=decomposition_source_hash,
        source_url=source_url,
        resolved_url=resolved_url,
        commit_sha=commit_sha,
        import_checksum=import_checksum,
        imported_at=imported_at,
        import_format=import_format,
        asset_manifest=asset_manifest,
    )
    session.add(row)
    await session.flush()
    return row


async def get_skill_version(session: AsyncSession, version_id: str) -> SkillVersionRow | None:
    """Get a skill version by ID."""
    result = await session.execute(
        select(SkillVersionRow).where(SkillVersionRow.version_id == version_id)
    )
    return result.scalar_one_or_none()


async def list_skill_versions(session: AsyncSession, skill_id: str) -> list[SkillVersionRow]:
    """List all versions of a skill, ordered by version number descending."""
    result = await session.execute(
        select(SkillVersionRow)
        .where(SkillVersionRow.skill_id == skill_id)
        .order_by(SkillVersionRow.version_number.desc())
    )
    return list(result.scalars().all())


async def get_next_version_number(session: AsyncSession, skill_id: str) -> int:
    """Get the next version number for a skill.

    Locks the parent skill row (FOR UPDATE) to serialize concurrent
    version creation.  This works on both SQLite (no-op lock) and
    PostgreSQL (row-level lock).
    """
    # Lock the parent skill row to serialize concurrent version writes
    await session.execute(select(SkillRow).where(SkillRow.skill_id == skill_id).with_for_update())
    result = await session.execute(
        select(sa.func.max(SkillVersionRow.version_number)).where(
            SkillVersionRow.skill_id == skill_id
        )
    )
    current_max = result.scalar_one_or_none()
    return (current_max or 0) + 1


async def set_current_version(session: AsyncSession, skill_id: str, version_id: str) -> bool:
    """Set the current version of a skill (atomic publish)."""
    result = await session.execute(
        update(SkillRow).where(SkillRow.skill_id == skill_id).values(current_version_id=version_id)
    )
    return result.rowcount > 0


# --- Skill Assets ---


async def create_skill_asset(
    session: AsyncSession,
    *,
    asset_id: str | None = None,
    skill_version_id: str,
    filename: str,
    artifact_namespace: str = "skills",
    artifact_object_id: str,
    content_hash: str,
    size_bytes: int = 0,
    content_type: str = "application/octet-stream",
) -> SkillAssetRow:
    """Create a skill asset record linked to the artifact store."""
    row = SkillAssetRow(
        asset_id=asset_id or f"sa_{uuid.uuid4().hex[:12]}",
        skill_version_id=skill_version_id,
        filename=filename,
        artifact_namespace=artifact_namespace,
        artifact_object_id=artifact_object_id,
        content_hash=content_hash,
        size_bytes=size_bytes,
        content_type=content_type,
    )
    session.add(row)
    await session.flush()
    return row


async def list_skill_assets(session: AsyncSession, skill_version_id: str) -> list[SkillAssetRow]:
    """List all assets for a skill version."""
    result = await session.execute(
        select(SkillAssetRow)
        .where(SkillAssetRow.skill_version_id == skill_version_id)
        .order_by(SkillAssetRow.filename.asc())
    )
    return list(result.scalars().all())


async def get_skill_asset(session: AsyncSession, asset_id: str) -> SkillAssetRow | None:
    """Get a skill asset by ID."""

    result = await session.execute(select(SkillAssetRow).where(SkillAssetRow.asset_id == asset_id))
    return result.scalar_one_or_none()


async def get_skill_asset_by_artifact_object(
    session: AsyncSession,
    *,
    artifact_namespace: str,
    artifact_object_id: str,
    filename: str,
) -> SkillAssetRow | None:
    """Get a skill asset by its artifact-store object reference."""

    result = await session.execute(
        select(SkillAssetRow)
        .where(
            SkillAssetRow.artifact_namespace == artifact_namespace,
            SkillAssetRow.artifact_object_id == artifact_object_id,
            SkillAssetRow.filename == filename,
        )
        .order_by(SkillAssetRow.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# --- Executors ---


async def list_executors(
    session: AsyncSession,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
) -> list[ExecutorRow]:
    """List all executor configurations."""
    stmt = select(ExecutorRow).order_by(ExecutorRow.name)
    if owner_email is not None:
        if include_shared:
            stmt = stmt.where(
                sa.or_(
                    ExecutorRow.owner_email == owner_email,
                    _shared_owner_clause(ExecutorRow.owner_email),
                )
            )
        else:
            stmt = stmt.where(ExecutorRow.owner_email == owner_email)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_websocket_executors_for_mcp_server(
    session: AsyncSession,
    server_id: str,
) -> list[ExecutorRow]:
    """List websocket executors whose config references an MCP server."""

    rows = await list_executors(session)
    result = []
    for row in rows:
        if row.executor_type != "websocket":
            continue
        config = row.config if isinstance(row.config, dict) else {}
        server_ids = config.get("mcp_server_ids")
        if isinstance(server_ids, list) and server_id in server_ids:
            result.append(row)
    return result


async def get_executor_row(
    session: AsyncSession,
    executor_id: str,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
) -> ExecutorRow | None:
    """Get an executor by ID."""
    stmt = select(ExecutorRow).where(ExecutorRow.executor_id == executor_id)
    if owner_email is not None:
        if include_shared:
            stmt = stmt.where(
                sa.or_(
                    ExecutorRow.owner_email == owner_email,
                    _shared_owner_clause(ExecutorRow.owner_email),
                )
            )
        else:
            stmt = stmt.where(ExecutorRow.owner_email == owner_email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_default_executor(
    session: AsyncSession,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
) -> ExecutorRow | None:
    """Get the default executor (is_default=True)."""
    stmt = select(ExecutorRow).where(ExecutorRow.is_default.is_(True)).limit(1)
    if owner_email is not None:
        if include_shared:
            stmt = stmt.where(
                sa.or_(
                    ExecutorRow.owner_email == owner_email,
                    _shared_owner_clause(ExecutorRow.owner_email),
                )
            )
        else:
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
    shared: bool = False,
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
        owner_email=SYSTEM_USER_EMAIL if shared else owner_email,
    )
    session.add(row)
    await session.flush()
    return row


async def update_executor(
    session: AsyncSession,
    executor_id: str,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
    **kwargs: Any,
) -> ExecutorRow | None:
    """Update an executor by ID."""
    row = await get_executor_row(
        session,
        executor_id,
        owner_email=owner_email,
        include_shared=include_shared,
    )
    if row is None:
        return None
    if "shared" in kwargs:
        shared = bool(kwargs.pop("shared"))
        row.owner_email = SYSTEM_USER_EMAIL if shared else owner_email
    for key, value in kwargs.items():
        if hasattr(row, key):
            setattr(row, key, value)
    await session.flush()
    return row


async def delete_executor(
    session: AsyncSession,
    executor_id: str,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
) -> bool:
    """Delete an executor by ID."""
    row = await get_executor_row(
        session,
        executor_id,
        owner_email=owner_email,
        include_shared=include_shared,
    )
    if row is None:
        return False
    stmt = delete(ExecutorRow).where(ExecutorRow.executor_id == executor_id)
    if owner_email is not None:
        if include_shared:
            stmt = stmt.where(
                sa.or_(
                    ExecutorRow.owner_email == owner_email,
                    _shared_owner_clause(ExecutorRow.owner_email),
                )
            )
        else:
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
    runtime_metadata: dict[str, Any] | None = None,
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
    if runtime_metadata is not None:
        row.runtime_metadata = runtime_metadata
    if last_observed_at is not None:
        row.last_observed_at = last_observed_at
    if runtime_state is not None:
        row.runtime_state = runtime_state
    await session.flush()
    return row


async def get_tool_classification_rows(
    session: AsyncSession,
    *,
    scope_key: str,
    tool_ids: list[str],
) -> list[ToolClassificationRow]:
    """Load persisted tool classifications for a scope and tool ids."""

    if not tool_ids:
        return []
    result = await session.execute(
        select(ToolClassificationRow).where(
            ToolClassificationRow.scope_key == scope_key,
            ToolClassificationRow.tool_id.in_(tool_ids),
        )
    )
    return list(result.scalars().all())


async def get_tool_classification_override_rows(
    session: AsyncSession,
    *,
    scope_key: str,
    tool_ids: list[str],
) -> list[ToolClassificationOverrideRow]:
    """Load manual tool classification overrides for a scope and tool ids."""

    if not tool_ids:
        return []
    result = await session.execute(
        select(ToolClassificationOverrideRow).where(
            ToolClassificationOverrideRow.scope_key == scope_key,
            ToolClassificationOverrideRow.tool_id.in_(tool_ids),
        )
    )
    return list(result.scalars().all())


async def upsert_tool_classification(
    session: AsyncSession,
    *,
    scope_key: str,
    owner_email: str | None,
    tool_id: str,
    source_type: str,
    fingerprint: str,
    tool_payload: dict[str, Any],
    category: str | None = None,
    capabilities: list[str] | None = None,
    classification_source: str | None = None,
    classification_confidence: float | None = None,
    status: str = "pending",
    attempts: int | None = None,
    next_retry_at: datetime | None = None,
    last_attempt_at: datetime | None = None,
    last_error: str | None = None,
) -> ToolClassificationRow:
    """Create or update persisted tool classification state."""

    dialect_name = session.get_bind().dialect.name
    if dialect_name in {"postgresql", "sqlite"}:
        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert

        now = _utcnow()
        insert_values: dict[str, Any] = {
            "classification_id": f"tc_{uuid.uuid4().hex}",
            "scope_key": scope_key,
            "owner_email": owner_email,
            "tool_id": tool_id,
            "source_type": source_type,
            "fingerprint": fingerprint,
            "tool_payload": tool_payload,
            "status": status,
            "category": category,
            "capabilities": capabilities,
            "classification_source": classification_source,
            "classification_confidence": classification_confidence,
            "attempts": attempts or 0,
            "next_retry_at": next_retry_at,
            "last_attempt_at": last_attempt_at,
            "last_error": last_error,
            "updated_at": now,
        }
        update_values: dict[str, Any] = {
            "owner_email": owner_email,
            "source_type": source_type,
            "fingerprint": fingerprint,
            "tool_payload": tool_payload,
            "status": status,
            "category": category,
            "capabilities": capabilities,
            "classification_source": classification_source,
            "classification_confidence": classification_confidence,
            "last_error": last_error,
            "updated_at": now,
        }
        if attempts is not None:
            update_values["attempts"] = attempts
        if next_retry_at is not None or status == "ready":
            update_values["next_retry_at"] = next_retry_at
        if last_attempt_at is not None or status == "pending":
            update_values["last_attempt_at"] = last_attempt_at

        stmt = (
            insert(ToolClassificationRow)
            .values(**insert_values)
            .on_conflict_do_update(
                index_elements=["scope_key", "tool_id"],
                set_=update_values,
            )
            .returning(ToolClassificationRow)
        )
        result = await session.execute(stmt)
        row = result.scalar_one()
        await session.flush()
        return row

    result = await session.execute(
        select(ToolClassificationRow).where(
            ToolClassificationRow.scope_key == scope_key,
            ToolClassificationRow.tool_id == tool_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = ToolClassificationRow(
            classification_id=f"tc_{uuid.uuid4().hex}",
            scope_key=scope_key,
            owner_email=owner_email,
            tool_id=tool_id,
            source_type=source_type,
            fingerprint=fingerprint,
            tool_payload=tool_payload,
            status=status,
            category=category,
            capabilities=capabilities,
            classification_source=classification_source,
            classification_confidence=classification_confidence,
            attempts=attempts or 0,
            next_retry_at=next_retry_at,
            last_attempt_at=last_attempt_at,
            last_error=last_error,
        )
        session.add(row)
        await session.flush()
        return row

    row.owner_email = owner_email
    row.source_type = source_type
    row.fingerprint = fingerprint
    row.tool_payload = tool_payload
    row.status = status
    row.category = category
    row.capabilities = capabilities
    row.classification_source = classification_source
    row.classification_confidence = classification_confidence
    if attempts is not None:
        row.attempts = attempts
    if next_retry_at is not None or status == "ready":
        row.next_retry_at = next_retry_at
    if last_attempt_at is not None or status == "pending":
        row.last_attempt_at = last_attempt_at
    row.last_error = last_error
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def upsert_tool_classification_override(
    session: AsyncSession,
    *,
    scope_key: str,
    owner_email: str | None,
    tool_id: str,
    profile_group: str,
    capabilities: list[str],
) -> ToolClassificationOverrideRow:
    """Create or update a manual tool classification override."""

    result = await session.execute(
        select(ToolClassificationOverrideRow).where(
            ToolClassificationOverrideRow.scope_key == scope_key,
            ToolClassificationOverrideRow.tool_id == tool_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = ToolClassificationOverrideRow(
            override_id=f"tco_{uuid.uuid4().hex}",
            scope_key=scope_key,
            owner_email=owner_email,
            tool_id=tool_id,
            profile_group=profile_group,
            capabilities=capabilities,
        )
        session.add(row)
        await session.flush()
        return row
    row.owner_email = owner_email
    row.profile_group = profile_group
    row.capabilities = capabilities
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def delete_tool_classification_override(
    session: AsyncSession,
    *,
    scope_key: str,
    tool_id: str,
) -> bool:
    """Delete a manual tool classification override."""

    result = await session.execute(
        delete(ToolClassificationOverrideRow).where(
            ToolClassificationOverrideRow.scope_key == scope_key,
            ToolClassificationOverrideRow.tool_id == tool_id,
        )
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0) > 0


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
        shared=True,
    )


# --- MCP Servers ---


async def list_mcp_servers(
    session: AsyncSession,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
) -> list[MCPServerRow]:
    """List all MCP server configurations."""
    stmt = select(MCPServerRow).order_by(MCPServerRow.name)
    if owner_email is not None:
        if include_shared:
            stmt = stmt.where(
                sa.or_(
                    MCPServerRow.owner_email == owner_email,
                    MCPServerRow.owner_email == SYSTEM_USER_EMAIL,
                )
            )
        else:
            stmt = stmt.where(MCPServerRow.owner_email == owner_email)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_mcp_server(
    session: AsyncSession,
    server_id: str,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
) -> MCPServerRow | None:
    """Get an MCP server by ID."""
    stmt = select(MCPServerRow).where(MCPServerRow.server_id == server_id)
    if owner_email is not None:
        if include_shared:
            stmt = stmt.where(
                sa.or_(
                    MCPServerRow.owner_email == owner_email,
                    MCPServerRow.owner_email == SYSTEM_USER_EMAIL,
                )
            )
        else:
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
    headers: dict[str, str] | None = None,
    auth_config: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
    description: str | None = None,
    owner_email: str,
    status: str = "active",
    shared: bool = False,
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
        headers=headers or {},
        auth_config=auth_config,
        timeout_seconds=timeout_seconds,
        description=description,
        owner_email=SYSTEM_USER_EMAIL if shared else owner_email,
        status=status,
    )
    session.add(row)
    await session.flush()
    return row


def mcp_oauth_resource_key(resource: str | None) -> str:
    """Normalize nullable OAuth resource into a deterministic uniqueness key."""

    return (resource or "").strip()


async def get_mcp_oauth_token(
    session: AsyncSession,
    *,
    user_email: str,
    mcp_server_id: str,
    issuer: str,
    resource: str | None,
) -> MCPOAuthTokenRow | None:
    result = await session.execute(
        select(MCPOAuthTokenRow).where(
            MCPOAuthTokenRow.user_email == user_email,
            MCPOAuthTokenRow.mcp_server_id == mcp_server_id,
            MCPOAuthTokenRow.issuer == issuer,
            MCPOAuthTokenRow.resource_key == mcp_oauth_resource_key(resource),
        )
    )
    return result.scalar_one_or_none()


async def get_mcp_oauth_token_for_server(
    session: AsyncSession,
    *,
    user_email: str,
    mcp_server_id: str,
) -> MCPOAuthTokenRow | None:
    result = await session.execute(
        select(MCPOAuthTokenRow)
        .where(
            MCPOAuthTokenRow.user_email == user_email,
            MCPOAuthTokenRow.mcp_server_id == mcp_server_id,
        )
        .order_by(
            case((MCPOAuthTokenRow.status == "active", 0), else_=1),
            MCPOAuthTokenRow.updated_at.desc(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_mcp_oauth_token(
    session: AsyncSession,
    *,
    user_email: str,
    mcp_server_id: str,
    issuer: str,
    resource: str | None,
    client_id: str | None,
    scopes: list[str],
    token_type: str,
    expires_at: datetime | None,
    encrypted_payload: bytes,
    status: str = "active",
) -> MCPOAuthTokenRow:
    row = await get_mcp_oauth_token(
        session,
        user_email=user_email,
        mcp_server_id=mcp_server_id,
        issuer=issuer,
        resource=resource,
    )
    if row is None:
        row = MCPOAuthTokenRow(
            token_id=f"mcptok_{uuid.uuid4().hex[:12]}",
            user_email=user_email,
            mcp_server_id=mcp_server_id,
            issuer=issuer,
            resource=resource,
            resource_key=mcp_oauth_resource_key(resource),
            client_id=client_id,
            scopes=scopes,
            token_type=token_type or "Bearer",
            expires_at=expires_at,
            status=status,
            encrypted_payload=encrypted_payload,
        )
        session.add(row)
    else:
        row.client_id = client_id
        row.scopes = scopes
        row.token_type = token_type or "Bearer"
        row.expires_at = expires_at
        row.status = status
        row.encrypted_payload = encrypted_payload
        row.version = int(row.version or 0) + 1
        row.updated_at = _utcnow()
    await session.flush()
    return row


async def mark_mcp_oauth_token_status(
    session: AsyncSession,
    *,
    token_id: str,
    status: str,
) -> None:
    row = await session.get(MCPOAuthTokenRow, token_id)
    if row is not None:
        row.status = status
        row.updated_at = _utcnow()
        await session.flush()


async def get_mcp_oauth_transaction(
    session: AsyncSession,
    transaction_id: str,
) -> MCPOAuthTransactionRow | None:
    return await session.get(MCPOAuthTransactionRow, transaction_id)


async def create_mcp_oauth_transaction(
    session: AsyncSession,
    *,
    transaction_id: str,
    user_email: str,
    mcp_server_id: str,
    issuer: str,
    authorization_server: str,
    resource: str | None,
    scopes: list[str],
    redirect_uri: str,
    client_id: str,
    code_challenge: str,
    state_hash: str,
    encrypted_payload: bytes,
    expires_at: datetime,
    task_id: str | None = None,
    step_name: str | None = None,
    step_run_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    notification_id: str | None = None,
) -> MCPOAuthTransactionRow:
    row = MCPOAuthTransactionRow(
        transaction_id=transaction_id,
        user_email=user_email,
        mcp_server_id=mcp_server_id,
        issuer=issuer,
        authorization_server=authorization_server,
        resource=resource,
        resource_key=mcp_oauth_resource_key(resource),
        scopes=scopes,
        redirect_uri=redirect_uri,
        client_id=client_id,
        code_challenge=code_challenge,
        state_hash=state_hash,
        encrypted_payload=encrypted_payload,
        expires_at=expires_at,
        task_id=task_id,
        step_name=step_name,
        step_run_id=step_run_id,
        session_id=session_id,
        conversation_id=conversation_id,
        notification_id=notification_id,
    )
    session.add(row)
    await session.flush()
    return row


async def update_mcp_server(
    session: AsyncSession,
    server_id: str,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
    **kwargs: Any,
) -> MCPServerRow | None:
    """Update an MCP server by ID."""
    row = await get_mcp_server(
        session,
        server_id,
        owner_email=owner_email,
        include_shared=include_shared,
    )
    if row is None:
        return None
    if "shared" in kwargs:
        shared = bool(kwargs.pop("shared"))
        row.owner_email = SYSTEM_USER_EMAIL if shared else owner_email or row.owner_email
    for key, value in kwargs.items():
        if hasattr(row, key):
            setattr(row, key, value)
    await session.flush()
    return row


async def delete_mcp_server(
    session: AsyncSession,
    server_id: str,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
) -> bool:
    """Delete an MCP server by ID."""
    row = await get_mcp_server(
        session,
        server_id,
        owner_email=owner_email,
        include_shared=include_shared,
    )
    if row is None:
        return False
    stmt = delete(MCPServerRow).where(MCPServerRow.server_id == server_id)
    if owner_email is not None:
        if include_shared:
            stmt = stmt.where(
                sa.or_(
                    MCPServerRow.owner_email == owner_email,
                    MCPServerRow.owner_email == SYSTEM_USER_EMAIL,
                )
            )
        else:
            stmt = stmt.where(MCPServerRow.owner_email == owner_email)
    await session.execute(stmt)
    return True


async def mcp_server_referenced_by_executors(
    session: AsyncSession,
    server_id: str,
    *,
    owner_email: str | None = None,
    include_shared: bool = False,
) -> list[str]:
    """Return executor IDs that reference this MCP server in their config."""
    from cognis.models.tool import MCP_SERVER_IDS_KEY

    executors = await list_executors(
        session,
        owner_email=owner_email,
        include_shared=include_shared,
    )
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
    preferred_for_task_delivery: bool = False,
    adapter_location: str = "controller",
    executor_id: str | None = None,
    allowed_senders: list[str] | None = None,
    dm_policy: str = "pairing",
    group_policy: str = "pairing",
    webhook_secret: str | None = None,
) -> ChannelAccountRow:
    """Create a new channel account."""
    if preferred_for_task_delivery:
        await session.execute(
            update(ChannelAccountRow)
            .where(ChannelAccountRow.user_email == user_email)
            .where(ChannelAccountRow.agent_id == agent_id)
            .values(preferred_for_task_delivery=False)
        )
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
        preferred_for_task_delivery=preferred_for_task_delivery,
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
    next_user_email = str(kwargs.get("user_email") or row.user_email)
    next_agent_id = str(kwargs.get("agent_id") or row.agent_id)
    if kwargs.get("preferred_for_task_delivery") is True:
        await session.execute(
            update(ChannelAccountRow)
            .where(ChannelAccountRow.user_email == next_user_email)
            .where(ChannelAccountRow.agent_id == next_agent_id)
            .where(ChannelAccountRow.account_id != account_id)
            .values(preferred_for_task_delivery=False)
        )
    for key, value in kwargs.items():
        if hasattr(row, key):
            setattr(row, key, value)
    await session.flush()
    return row


async def get_preferred_channel_account_for_agent(
    session: AsyncSession,
    *,
    user_email: str,
    agent_id: str,
) -> ChannelAccountRow | None:
    """Return the preferred enabled task-delivery channel account for an agent."""

    result = await session.execute(
        select(ChannelAccountRow)
        .where(ChannelAccountRow.user_email == user_email)
        .where(ChannelAccountRow.agent_id == agent_id)
        .where(ChannelAccountRow.enabled.is_(True))
        .where(ChannelAccountRow.preferred_for_task_delivery.is_(True))
        .order_by(ChannelAccountRow.updated_at.desc(), ChannelAccountRow.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_active_conversation_for_channel_account(
    session: AsyncSession,
    *,
    user_email: str,
    agent_id: str,
    account_id: str,
) -> Conversation | None:
    """Return the latest active conversation bound to a channel account."""

    latest_activity = case(
        (Conversation.last_message_at.is_(None), Conversation.updated_at),
        (Conversation.updated_at > Conversation.last_message_at, Conversation.updated_at),
        else_=Conversation.last_message_at,
    )
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_email == user_email)
        .where(Conversation.agent_id == agent_id)
        .where(Conversation.status == "active")
        .where(Conversation.context_type.notin_(["web", "api"]))
        .order_by(latest_activity.desc(), Conversation.updated_at.desc())
    )
    for conversation in result.scalars().all():
        platform_data = conversation.context_data or {}
        if str(platform_data.get("account_id") or "") == account_id:
            return conversation
        context_ref = conversation.context_ref or ""
        parts = context_ref.split(":", 3)
        if len(parts) >= 3 and parts[1] == account_id:
            return conversation
    return None


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
    content_hash: str | None = None,
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
        content_hash=content_hash,
    )
    session.add(row)
    await session.flush()
    return row


async def get_artifact_record(session: AsyncSession, artifact_id: str) -> ArtifactRecordRow | None:
    result = await session.execute(
        select(ArtifactRecordRow).where(ArtifactRecordRow.artifact_id == artifact_id)
    )
    return result.scalar_one_or_none()


def _artifact_discovery_stmt(
    *,
    owner_email: str,
    kind: str | None = None,
    purpose: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> sa.Select[tuple[ArtifactRecordRow]]:
    stmt = select(ArtifactRecordRow).where(
        ArtifactRecordRow.owner_email == owner_email,
        ArtifactRecordRow.status != "deleted",
        sa.or_(ArtifactRecordRow.expires_at.is_(None), ArtifactRecordRow.expires_at > _utcnow()),
    )
    if kind is not None:
        stmt = stmt.where(ArtifactRecordRow.kind == kind)
    if purpose is not None:
        stmt = stmt.where(ArtifactRecordRow.purpose == purpose)
    if conversation_id is not None:
        stmt = stmt.where(ArtifactRecordRow.conversation_id == conversation_id)
    if session_id is not None:
        stmt = stmt.where(ArtifactRecordRow.session_id == session_id)
    if created_after is not None:
        stmt = stmt.where(ArtifactRecordRow.created_at >= created_after)
    if created_before is not None:
        stmt = stmt.where(ArtifactRecordRow.created_at <= created_before)
    return stmt


async def list_recent_artifact_records(
    session: AsyncSession,
    *,
    owner_email: str,
    limit: int = 10,
    kind: str | None = None,
    purpose: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> list[ArtifactRecordRow]:
    result = await session.execute(
        _artifact_discovery_stmt(
            owner_email=owner_email,
            kind=kind,
            purpose=purpose,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        .order_by(ArtifactRecordRow.created_at.desc(), ArtifactRecordRow.artifact_id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def search_artifact_records(
    session: AsyncSession,
    *,
    owner_email: str,
    query: str | None = None,
    limit: int = 10,
    kind: str | None = None,
    purpose: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> list[ArtifactRecordRow]:
    stmt = _artifact_discovery_stmt(
        owner_email=owner_email,
        kind=kind,
        purpose=purpose,
        conversation_id=conversation_id,
        session_id=session_id,
        created_after=created_after,
        created_before=created_before,
    )
    normalized_query = " ".join((query or "").strip().lower().split())
    if normalized_query:
        for token in normalized_query.split():
            pattern = f"%{token}%"
            stmt = stmt.where(
                sa.or_(
                    sa.func.lower(ArtifactRecordRow.filename).like(pattern),
                    sa.func.lower(ArtifactRecordRow.artifact_id).like(pattern),
                    sa.func.lower(ArtifactRecordRow.purpose).like(pattern),
                )
            )
    result = await session.execute(
        stmt.order_by(
            ArtifactRecordRow.created_at.desc(), ArtifactRecordRow.artifact_id.desc()
        ).limit(limit)
    )
    return list(result.scalars().all())


async def find_tool_artifact_record(
    session: AsyncSession,
    *,
    owner_email: str,
    source_tool_call_id: str,
    source_anchor: str,
    source_hash: str,
) -> ArtifactRecordRow | None:
    # Tool artifact refs are lazy aliases to saved tool output anchors. We avoid
    # one DB row per discovered candidate; only first access creates a regular
    # artifact. Existing artifact columns carry the stable source identity:
    # purpose=tool_artifact, conversation_id=source call_id,
    # session_id=source anchor, content_hash=source fingerprint.
    stmt = (
        select(ArtifactRecordRow)
        .where(
            ArtifactRecordRow.owner_email == owner_email,
            ArtifactRecordRow.status != "deleted",
            ArtifactRecordRow.purpose == "tool_artifact",
            ArtifactRecordRow.content_hash == source_hash,
            ArtifactRecordRow.conversation_id == source_tool_call_id,
            ArtifactRecordRow.session_id == source_anchor,
            sa.or_(
                ArtifactRecordRow.expires_at.is_(None), ArtifactRecordRow.expires_at > _utcnow()
            ),
        )
        .order_by(ArtifactRecordRow.created_at.desc(), ArtifactRecordRow.artifact_id.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
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
    await mark_knowledgebase_artifact_removed(session, artifact_id=artifact_id)
    await session.flush()
    return True


async def delete_artifact_record(session: AsyncSession, artifact_id: str) -> bool:
    row = await get_artifact_record(session, artifact_id)
    if row is None:
        return False
    await mark_knowledgebase_artifact_removed(session, artifact_id=artifact_id)
    await session.delete(row)
    return True


# --- Knowledgebases ---


def _kb_id() -> str:
    return f"kb_{uuid.uuid4().hex[:16]}"


def _kb_artifact_id() -> str:
    return f"kba_{uuid.uuid4().hex[:16]}"


def _kb_job_id() -> str:
    return f"kbj_{uuid.uuid4().hex[:16]}"


async def mark_knowledgebase_artifact_removed(
    session: AsyncSession, *, artifact_id: str
) -> list[KnowledgebaseArtifactRow]:
    result = await session.execute(
        select(KnowledgebaseArtifactRow).where(
            KnowledgebaseArtifactRow.artifact_id == artifact_id,
            KnowledgebaseArtifactRow.status.not_in(["detached", "removed"]),
        )
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.status = "removed"
        row.removed_at = _utcnow()
        row.last_error = "canonical_artifact_removed"
        job = await enqueue_knowledgebase_job(
            session,
            knowledgebase_id=row.knowledgebase_id,
            kb_artifact_id=row.kb_artifact_id,
            artifact_id=artifact_id,
            job_type="delete_artifact_index",
            priority=5,
        )
        row.last_job_id = job.job_id
    await session.flush()
    return rows


async def create_knowledgebase(
    session: AsyncSession,
    *,
    owner_email: str,
    name: str,
    description: str | None = None,
    metadata_schema: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> KnowledgebaseRow:
    row = KnowledgebaseRow(
        knowledgebase_id=_kb_id(),
        owner_email=owner_email,
        name=name,
        description=description,
        metadata_schema=metadata_schema or {},
        settings=settings or {},
    )
    session.add(row)
    await session.flush()
    return row


async def delete_knowledgebase(
    session: AsyncSession, *, owner_email: str, knowledgebase_id: str
) -> bool:
    row = await get_knowledgebase(
        session, owner_email=owner_email, knowledgebase_id=knowledgebase_id
    )
    if row is None:
        return False
    row.status = "deleted"
    row.deleted_at = _utcnow()
    attachments = await list_knowledgebase_artifacts(session, knowledgebase_id=knowledgebase_id)
    for attachment in attachments:
        if attachment.status in {"detached", "removed"}:
            continue
        attachment.status = "removed"
        attachment.removed_at = _utcnow()
        job = await enqueue_knowledgebase_job(
            session,
            knowledgebase_id=knowledgebase_id,
            kb_artifact_id=attachment.kb_artifact_id,
            artifact_id=attachment.artifact_id,
            job_type="delete_artifact_index",
            priority=5,
        )
        attachment.last_job_id = job.job_id
    await session.flush()
    return True


async def update_knowledgebase(
    session: AsyncSession,
    *,
    owner_email: str,
    knowledgebase_id: str,
    updates: dict[str, Any],
) -> KnowledgebaseRow | None:
    row = await get_knowledgebase(
        session, owner_email=owner_email, knowledgebase_id=knowledgebase_id
    )
    if row is None:
        return None
    if "name" in updates:
        row.name = updates["name"]
    if "description" in updates:
        row.description = updates["description"]
    if "metadata_schema" in updates:
        row.metadata_schema = updates["metadata_schema"] or {}
    if "settings" in updates:
        row.settings = updates["settings"] or {}
    if "status" in updates:
        status = str(updates["status"])
        if status == "deleted":
            raise ValueError("Use delete_knowledgebase for deletion")
        row.status = status
        row.archived_at = _utcnow() if status == "archived" else None
    await session.flush()
    return row


async def list_knowledgebases(session: AsyncSession, *, owner_email: str) -> list[KnowledgebaseRow]:
    result = await session.execute(
        select(KnowledgebaseRow)
        .where(KnowledgebaseRow.owner_email == owner_email, KnowledgebaseRow.status != "deleted")
        .order_by(KnowledgebaseRow.created_at.desc())
    )
    return list(result.scalars().all())


async def list_knowledgebases_by_ids(
    session: AsyncSession, *, owner_email: str, knowledgebase_ids: list[str]
) -> list[KnowledgebaseRow]:
    if not knowledgebase_ids:
        return []
    result = await session.execute(
        select(KnowledgebaseRow)
        .where(
            KnowledgebaseRow.owner_email == owner_email,
            KnowledgebaseRow.knowledgebase_id.in_(knowledgebase_ids),
            KnowledgebaseRow.status != "deleted",
        )
        .order_by(KnowledgebaseRow.created_at.desc())
    )
    return list(result.scalars().all())


async def get_knowledgebase(
    session: AsyncSession, *, owner_email: str, knowledgebase_id: str
) -> KnowledgebaseRow | None:
    result = await session.execute(
        select(KnowledgebaseRow).where(
            KnowledgebaseRow.knowledgebase_id == knowledgebase_id,
            KnowledgebaseRow.owner_email == owner_email,
            KnowledgebaseRow.status != "deleted",
        )
    )
    return result.scalar_one_or_none()


async def get_knowledgebase_by_id(
    session: AsyncSession, *, knowledgebase_id: str
) -> KnowledgebaseRow | None:
    result = await session.execute(
        select(KnowledgebaseRow).where(
            KnowledgebaseRow.knowledgebase_id == knowledgebase_id,
            KnowledgebaseRow.status != "deleted",
        )
    )
    return result.scalar_one_or_none()


async def assign_knowledgebase_to_agent(
    session: AsyncSession,
    *,
    owner_email: str,
    knowledgebase_id: str,
    agent_id: str,
) -> bool:
    kb = await get_knowledgebase(
        session, owner_email=owner_email, knowledgebase_id=knowledgebase_id
    )
    agent = await get_agent(session, agent_id)
    if kb is None or agent is None or agent.owner_email != owner_email:
        return False
    permissions = dict(agent.permissions or {})
    allowed = list(permissions.get("allowed_knowledgebases") or [])
    if knowledgebase_id not in allowed:
        allowed.append(knowledgebase_id)
    permissions["allowed_knowledgebases"] = allowed
    agent.permissions = permissions
    flag_modified(agent, "permissions")
    agent.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def unassign_knowledgebase_from_agent(
    session: AsyncSession,
    *,
    owner_email: str,
    knowledgebase_id: str,
    agent_id: str,
) -> bool:
    kb = await get_knowledgebase(
        session, owner_email=owner_email, knowledgebase_id=knowledgebase_id
    )
    agent = await get_agent(session, agent_id)
    if kb is None or agent is None or agent.owner_email != owner_email:
        return False
    permissions = dict(agent.permissions or {})
    allowed = [
        value
        for value in permissions.get("allowed_knowledgebases") or []
        if value != knowledgebase_id
    ]
    permissions["allowed_knowledgebases"] = allowed
    agent.permissions = permissions
    flag_modified(agent, "permissions")
    agent.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def list_knowledgebase_agent_assignments(
    session: AsyncSession, *, owner_email: str, knowledgebase_id: str
) -> list[str] | None:
    if (
        await get_knowledgebase(session, owner_email=owner_email, knowledgebase_id=knowledgebase_id)
        is None
    ):
        return None
    agents = await list_agents(session, owner_email=owner_email)
    assigned: list[str] = []
    for agent in agents:
        permissions = agent.permissions or {}
        if knowledgebase_id in (permissions.get("allowed_knowledgebases") or []):
            assigned.append(agent.agent_id)
    return assigned


async def enqueue_knowledgebase_job(
    session: AsyncSession,
    *,
    knowledgebase_id: str,
    job_type: str,
    kb_artifact_id: str | None = None,
    artifact_id: str | None = None,
    priority: int = 100,
) -> KnowledgebaseIndexJobRow:
    job = KnowledgebaseIndexJobRow(
        job_id=_kb_job_id(),
        knowledgebase_id=knowledgebase_id,
        kb_artifact_id=kb_artifact_id,
        artifact_id=artifact_id,
        job_type=job_type,
        status="queued",
        priority=priority,
    )
    session.add(job)
    await session.flush()
    return job


async def attach_artifact_to_knowledgebase(
    session: AsyncSession,
    *,
    owner_email: str,
    knowledgebase_id: str,
    artifact_id: str,
    metadata: dict[str, Any] | None = None,
) -> KnowledgebaseArtifactRow | None:
    kb = await get_knowledgebase(
        session, owner_email=owner_email, knowledgebase_id=knowledgebase_id
    )
    artifact = await get_artifact_record(session, artifact_id)
    if (
        kb is None
        or artifact is None
        or artifact.owner_email != owner_email
        or artifact.status == "deleted"
    ):
        return None
    result = await session.execute(
        select(KnowledgebaseArtifactRow).where(
            KnowledgebaseArtifactRow.knowledgebase_id == knowledgebase_id,
            KnowledgebaseArtifactRow.artifact_id == artifact_id,
            KnowledgebaseArtifactRow.status.not_in(["detached", "removed"]),
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.status = "queued"
        existing.metadata_json = metadata or existing.metadata_json or {}
        existing.last_error = None
        await enqueue_knowledgebase_job(
            session,
            knowledgebase_id=knowledgebase_id,
            kb_artifact_id=existing.kb_artifact_id,
            artifact_id=artifact_id,
            job_type="reindex_artifact",
        )
        await session.flush()
        return existing
    artifact.status = "attached"
    artifact.expires_at = None
    row = KnowledgebaseArtifactRow(
        kb_artifact_id=_kb_artifact_id(),
        knowledgebase_id=knowledgebase_id,
        artifact_id=artifact_id,
        status="queued",
        source_hash=artifact.content_hash,
        source_size_bytes=artifact.size_bytes,
        source_mime_type=artifact.mime_type,
        source_filename=artifact.filename,
        metadata_json=metadata or {},
    )
    session.add(row)
    await session.flush()
    job = await enqueue_knowledgebase_job(
        session,
        knowledgebase_id=knowledgebase_id,
        kb_artifact_id=row.kb_artifact_id,
        artifact_id=artifact_id,
        job_type="index_artifact",
    )
    row.last_job_id = job.job_id
    await session.flush()
    return row


async def list_knowledgebase_artifacts(
    session: AsyncSession, *, knowledgebase_id: str
) -> list[KnowledgebaseArtifactRow]:
    result = await session.execute(
        select(KnowledgebaseArtifactRow)
        .where(KnowledgebaseArtifactRow.knowledgebase_id == knowledgebase_id)
        .order_by(KnowledgebaseArtifactRow.attached_at.desc())
    )
    return list(result.scalars().all())


async def detach_knowledgebase_artifact(
    session: AsyncSession,
    *,
    owner_email: str,
    knowledgebase_id: str,
    artifact_id: str,
) -> KnowledgebaseArtifactRow | None:
    if (
        await get_knowledgebase(session, owner_email=owner_email, knowledgebase_id=knowledgebase_id)
        is None
    ):
        return None
    result = await session.execute(
        select(KnowledgebaseArtifactRow).where(
            KnowledgebaseArtifactRow.knowledgebase_id == knowledgebase_id,
            KnowledgebaseArtifactRow.artifact_id == artifact_id,
            KnowledgebaseArtifactRow.status.not_in(["detached", "removed"]),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    row.status = "detached"
    row.removed_at = _utcnow()
    job = await enqueue_knowledgebase_job(
        session,
        knowledgebase_id=knowledgebase_id,
        kb_artifact_id=row.kb_artifact_id,
        artifact_id=artifact_id,
        job_type="delete_artifact_index",
        priority=10,
    )
    row.last_job_id = job.job_id
    await session.flush()
    return row


async def list_knowledgebase_jobs(
    session: AsyncSession, *, knowledgebase_id: str, limit: int = 100
) -> list[KnowledgebaseIndexJobRow]:
    result = await session.execute(
        select(KnowledgebaseIndexJobRow)
        .where(KnowledgebaseIndexJobRow.knowledgebase_id == knowledgebase_id)
        .order_by(KnowledgebaseIndexJobRow.queued_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_knowledgebase_job(
    session: AsyncSession, *, knowledgebase_id: str, job_id: str
) -> KnowledgebaseIndexJobRow | None:
    result = await session.execute(
        select(KnowledgebaseIndexJobRow).where(
            KnowledgebaseIndexJobRow.knowledgebase_id == knowledgebase_id,
            KnowledgebaseIndexJobRow.job_id == job_id,
        )
    )
    return result.scalar_one_or_none()


async def enqueue_knowledgebase_artifact_reindex(
    session: AsyncSession,
    *,
    owner_email: str,
    knowledgebase_id: str,
    artifact_id: str,
) -> KnowledgebaseIndexJobRow | None:
    if (
        await get_knowledgebase(session, owner_email=owner_email, knowledgebase_id=knowledgebase_id)
        is None
    ):
        return None
    result = await session.execute(
        select(KnowledgebaseArtifactRow).where(
            KnowledgebaseArtifactRow.knowledgebase_id == knowledgebase_id,
            KnowledgebaseArtifactRow.artifact_id == artifact_id,
            KnowledgebaseArtifactRow.status.not_in(["detached", "removed"]),
        )
    )
    row = result.scalar_one_or_none()
    if row is None or row.artifact_id is None:
        return None
    row.status = "queued"
    row.last_error = None
    job = await enqueue_knowledgebase_job(
        session,
        knowledgebase_id=knowledgebase_id,
        kb_artifact_id=row.kb_artifact_id,
        artifact_id=row.artifact_id,
        job_type="reindex_artifact",
    )
    row.last_job_id = job.job_id
    await session.flush()
    return job


async def enqueue_knowledgebase_reindex(
    session: AsyncSession,
    *,
    owner_email: str,
    knowledgebase_id: str,
) -> list[KnowledgebaseIndexJobRow] | None:
    if (
        await get_knowledgebase(session, owner_email=owner_email, knowledgebase_id=knowledgebase_id)
        is None
    ):
        return None
    result = await session.execute(
        select(KnowledgebaseArtifactRow).where(
            KnowledgebaseArtifactRow.knowledgebase_id == knowledgebase_id,
            KnowledgebaseArtifactRow.status.not_in(["detached", "removed"]),
        )
    )
    jobs: list[KnowledgebaseIndexJobRow] = []
    for row in result.scalars().all():
        if row.artifact_id is None:
            continue
        row.status = "queued"
        row.last_error = None
        job = await enqueue_knowledgebase_job(
            session,
            knowledgebase_id=knowledgebase_id,
            kb_artifact_id=row.kb_artifact_id,
            artifact_id=row.artifact_id,
            job_type="reindex_artifact",
        )
        row.last_job_id = job.job_id
        jobs.append(job)
    await session.flush()
    return jobs


async def enqueue_retry_knowledgebase_job(
    session: AsyncSession,
    *,
    owner_email: str,
    knowledgebase_id: str,
    job_id: str,
) -> KnowledgebaseIndexJobRow | None:
    if (
        await get_knowledgebase(session, owner_email=owner_email, knowledgebase_id=knowledgebase_id)
        is None
    ):
        return None
    job = await get_knowledgebase_job(session, knowledgebase_id=knowledgebase_id, job_id=job_id)
    if job is None or job.status not in {"failed", "cancelled"}:
        return None
    new_job = await enqueue_knowledgebase_job(
        session,
        knowledgebase_id=knowledgebase_id,
        kb_artifact_id=job.kb_artifact_id,
        artifact_id=job.artifact_id,
        job_type=job.job_type,
        priority=job.priority,
    )
    if job.kb_artifact_id is not None:
        attachment = (
            await session.execute(
                select(KnowledgebaseArtifactRow).where(
                    KnowledgebaseArtifactRow.kb_artifact_id == job.kb_artifact_id
                )
            )
        ).scalar_one_or_none()
        if attachment is not None and attachment.status not in {"detached", "removed"}:
            attachment.status = "queued"
            attachment.last_error = None
            attachment.last_job_id = new_job.job_id
    await session.flush()
    return new_job


async def claim_next_knowledgebase_job(session: AsyncSession) -> KnowledgebaseIndexJobRow | None:
    result = await session.execute(
        select(KnowledgebaseIndexJobRow)
        .where(KnowledgebaseIndexJobRow.status == "queued")
        .order_by(KnowledgebaseIndexJobRow.priority.asc(), KnowledgebaseIndexJobRow.queued_at.asc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.started_at = _utcnow()
    job.updated_at = _utcnow()
    job.attempts += 1
    await session.flush()
    return job


async def delete_knowledgebase_chunks(session: AsyncSession, *, kb_artifact_id: str) -> list[str]:
    result = await session.execute(
        select(KnowledgebaseChunkRow.vector_id).where(
            KnowledgebaseChunkRow.kb_artifact_id == kb_artifact_id
        )
    )
    vector_ids = [value for value in result.scalars().all() if value]
    await session.execute(
        delete(KnowledgebaseChunkRow).where(KnowledgebaseChunkRow.kb_artifact_id == kb_artifact_id)
    )
    await session.flush()
    return vector_ids


async def insert_knowledgebase_chunks(
    session: AsyncSession,
    *,
    rows: list[KnowledgebaseChunkRow],
) -> None:
    session.add_all(rows)
    await session.flush()


async def list_knowledgebase_chunks(
    session: AsyncSession, *, knowledgebase_id: str
) -> list[KnowledgebaseChunkRow]:
    result = await session.execute(
        select(KnowledgebaseChunkRow).where(
            KnowledgebaseChunkRow.knowledgebase_id == knowledgebase_id
        )
    )
    return list(result.scalars().all())


async def get_knowledgebase_chunk(
    session: AsyncSession, *, knowledgebase_id: str, chunk_id: str
) -> KnowledgebaseChunkRow | None:
    result = await session.execute(
        select(KnowledgebaseChunkRow).where(
            KnowledgebaseChunkRow.knowledgebase_id == knowledgebase_id,
            KnowledgebaseChunkRow.chunk_id == chunk_id,
        )
    )
    return result.scalar_one_or_none()


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


# --- Channel delivery outbox ---


async def create_channel_delivery_outbox(
    session: AsyncSession,
    *,
    delivery_id: str,
    user_email: str,
    conversation_id: str,
    session_id: str | None,
    source_type: str,
    source_id: str | None,
    channel_type: str,
    account_id: str,
    chat_id: str,
    thread_id: str | None,
    fallback_text: str | None,
    next_attempt_at: datetime | None = None,
) -> ChannelDeliveryOutboxRow:
    row = ChannelDeliveryOutboxRow(
        delivery_id=delivery_id,
        user_email=user_email,
        conversation_id=conversation_id,
        session_id=session_id,
        source_type=source_type,
        source_id=source_id,
        channel_type=channel_type,
        account_id=account_id,
        chat_id=chat_id,
        thread_id=thread_id,
        fallback_text=fallback_text,
        status="pending",
        next_attempt_at=next_attempt_at,
    )
    session.add(row)
    await session.flush()
    return row


async def get_channel_delivery_outbox(
    session: AsyncSession,
    delivery_id: str,
) -> ChannelDeliveryOutboxRow | None:
    return await session.get(ChannelDeliveryOutboxRow, delivery_id)


async def has_channel_delivery_outbox_for_source(
    session: AsyncSession,
    *,
    conversation_id: str,
    source_type: str,
    source_id: str,
) -> bool:
    result = await session.execute(
        select(ChannelDeliveryOutboxRow.delivery_id).where(
            ChannelDeliveryOutboxRow.conversation_id == conversation_id,
            ChannelDeliveryOutboxRow.source_type == source_type,
            ChannelDeliveryOutboxRow.source_id == source_id,
            ChannelDeliveryOutboxRow.status != "suppressed",
        )
    )
    return result.scalar_one_or_none() is not None


async def claim_channel_delivery_outbox(
    session: AsyncSession,
    *,
    delivery_id: str,
    lease_token: str,
    lease_expires_at: datetime,
    ignore_next_attempt: bool = False,
) -> ChannelDeliveryOutboxRow | None:
    due_clause = sa.true()
    if not ignore_next_attempt:
        due_clause = sa.or_(
            ChannelDeliveryOutboxRow.next_attempt_at.is_(None),
            ChannelDeliveryOutboxRow.next_attempt_at <= _utcnow(),
        )
    result = await session.execute(
        update(ChannelDeliveryOutboxRow)
        .where(
            ChannelDeliveryOutboxRow.delivery_id == delivery_id,
            ChannelDeliveryOutboxRow.status.in_(["pending", "failed"]),
            due_clause,
        )
        .values(
            status="sending",
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            updated_at=_utcnow(),
        )
    )
    if not getattr(result, "rowcount", 0):
        return None
    return await get_channel_delivery_outbox(session, delivery_id)


async def mark_channel_delivery_sent(
    session: AsyncSession,
    *,
    delivery_id: str,
    lease_token: str,
) -> bool:
    result = await session.execute(
        update(ChannelDeliveryOutboxRow)
        .where(
            ChannelDeliveryOutboxRow.delivery_id == delivery_id,
            ChannelDeliveryOutboxRow.status == "sending",
            ChannelDeliveryOutboxRow.lease_token == lease_token,
        )
        .values(
            status="sent",
            sent_at=_utcnow(),
            lease_token=None,
            lease_expires_at=None,
            last_error=None,
            updated_at=_utcnow(),
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def mark_channel_delivery_failed(
    session: AsyncSession,
    *,
    delivery_id: str,
    lease_token: str,
    last_error: str | None,
    next_attempt_at: datetime,
) -> bool:
    result = await session.execute(
        update(ChannelDeliveryOutboxRow)
        .where(
            ChannelDeliveryOutboxRow.delivery_id == delivery_id,
            ChannelDeliveryOutboxRow.status == "sending",
            ChannelDeliveryOutboxRow.lease_token == lease_token,
        )
        .values(
            status="failed",
            attempt_count=ChannelDeliveryOutboxRow.attempt_count + 1,
            last_error=last_error,
            next_attempt_at=next_attempt_at,
            lease_token=None,
            lease_expires_at=None,
            updated_at=_utcnow(),
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def mark_channel_delivery_uncertain(
    session: AsyncSession,
    *,
    delivery_id: str,
) -> bool:
    result = await session.execute(
        update(ChannelDeliveryOutboxRow)
        .where(ChannelDeliveryOutboxRow.delivery_id == delivery_id)
        .values(
            status="uncertain",
            lease_token=None,
            lease_expires_at=None,
            updated_at=_utcnow(),
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def list_channel_delivery_outbox_due(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 100,
) -> list[ChannelDeliveryOutboxRow]:
    result = await session.execute(
        select(ChannelDeliveryOutboxRow)
        .where(
            ChannelDeliveryOutboxRow.status.in_(["pending", "failed"]),
            sa.or_(
                ChannelDeliveryOutboxRow.next_attempt_at.is_(None),
                ChannelDeliveryOutboxRow.next_attempt_at <= now,
            ),
        )
        .order_by(ChannelDeliveryOutboxRow.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_channel_delivery_outbox_stale_sending(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 100,
) -> list[ChannelDeliveryOutboxRow]:
    result = await session.execute(
        select(ChannelDeliveryOutboxRow)
        .where(
            ChannelDeliveryOutboxRow.status == "sending",
            ChannelDeliveryOutboxRow.lease_expires_at.is_not(None),
            ChannelDeliveryOutboxRow.lease_expires_at <= now,
        )
        .order_by(ChannelDeliveryOutboxRow.updated_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


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


# --- TTS cache ---


async def get_tts_cache_entry(
    session: AsyncSession,
    *,
    message_id: str,
    voice: str,
    model: str,
) -> TtsCacheRow | None:
    """Look up a cached TTS audio entry by ``(message_id, voice, model)``."""
    result = await session.execute(
        select(TtsCacheRow).where(
            TtsCacheRow.message_id == message_id,
            TtsCacheRow.voice == voice,
            TtsCacheRow.model == model,
        )
    )
    return result.scalar_one_or_none()


async def insert_tts_cache_entry(
    session: AsyncSession,
    *,
    message_id: str,
    voice: str,
    model: str,
    artifact_id: str,
    artifact_filename: str,
    content_type: str,
    owner_email: str | None,
    duration_seconds: float | None,
    size_bytes: int,
) -> TtsCacheRow:
    """Insert (or replace) a TTS cache row."""
    existing = await get_tts_cache_entry(session, message_id=message_id, voice=voice, model=model)
    if existing is not None:
        existing.artifact_id = artifact_id
        existing.artifact_filename = artifact_filename
        existing.content_type = content_type
        existing.owner_email = owner_email
        existing.duration_seconds = duration_seconds
        existing.size_bytes = size_bytes
        existing.created_at = _utcnow()
        await session.flush()
        return existing
    row = TtsCacheRow(
        message_id=message_id,
        voice=voice,
        model=model,
        artifact_id=artifact_id,
        artifact_filename=artifact_filename,
        content_type=content_type,
        owner_email=owner_email,
        duration_seconds=duration_seconds,
        size_bytes=size_bytes,
    )
    session.add(row)
    await session.flush()
    return row


async def delete_expired_tts_cache_entries(
    session: AsyncSession,
    *,
    older_than: datetime,
) -> list[TtsCacheRow]:
    """Return cache rows older than ``older_than`` and delete them.

    The artifact rows themselves are removed by the caller — this just
    returns the cache metadata so the artifact store can clean up the
    underlying blobs.
    """
    result = await session.execute(select(TtsCacheRow).where(TtsCacheRow.created_at < older_than))
    rows = list(result.scalars().all())
    for row in rows:
        await session.delete(row)
    await session.flush()
    return rows

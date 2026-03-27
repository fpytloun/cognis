"""Reusable async query helpers for Cognis DB."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import Agent, ApiKey, LLMProvider, Secret, Setting, User

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


async def get_llm_provider(session: AsyncSession, provider_id: str) -> LLMProvider | None:
    """Get an LLM provider by ID."""
    result = await session.execute(
        select(LLMProvider).where(LLMProvider.provider_id == provider_id)
    )
    return result.scalar_one_or_none()


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

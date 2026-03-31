"""Bootstrap services for filesystem, keys, database, and default settings."""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cognis.config import CognisConfig
from cognis.logging import get_logger
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import count_users, create_user, upsert_setting

logger = get_logger(__name__)

DEFAULT_SETTINGS: Final[dict[str, tuple[str, object]]] = {
    "session.max_context_tokens": ("session", 128000),
    "session.compaction_threshold": ("session", 0.85),
    "session.compaction_preserve_turns": ("session", 10),
    "session.max_tool_calls_per_turn": ("session", 50),
    "session.idle_timeout_seconds": ("session", 1800),
    "session.max_session_age_seconds": ("session", 86400),
    "session.max_delegation_depth": ("session", 5),
    "session.max_queued_messages": ("session", 5),
    "session.escalation_timeout_seconds": ("session", 300),
    "session.cache_max_entries": ("session", 200),
    "decision_engine.inline_max_length": ("decision_engine", 200),
    "security.non_bypassable_tools": (
        "security",
        ["shell", "bash", "write_file", "delete_file"],
    ),
    "security.api_read_requests_per_minute": ("security", 600),
    "security.api_write_requests_per_minute": ("security", 200),
    "security.token_ttl_seconds": ("security", 3600),
    "security.max_connections": ("security", 100),
    "security.ws_auth_timeout_seconds": ("security", 10),
    "web.backend": ("web", "direct"),
}


class SetupTokenManager:
    """In-memory one-time setup token store."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def issue(self) -> str:
        self._token = secrets.token_urlsafe(32)
        self._expires_at = datetime.now(UTC) + timedelta(minutes=15)
        return self._token

    def validate(self, token: str) -> bool:
        if self._token is None or self._expires_at is None:
            return False
        if datetime.now(UTC) > self._expires_at:
            return False
        return secrets.compare_digest(self._token, token)

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = None


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(data)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        logger.warning("Could not set file permissions", extra={"extra_data": {"path": str(path)}})
    tmp_path.replace(path)


def ensure_data_dir(config: CognisConfig) -> None:
    """Create required filesystem paths."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.jwt_private_key_path.parent.mkdir(parents=True, exist_ok=True)


def ensure_jwt_keypair(config: CognisConfig) -> None:
    """Generate ES256 keypair if missing or incomplete."""
    private_exists = config.jwt_private_key_path.exists()
    public_exists = config.jwt_public_key_path.exists()
    if private_exists and public_exists:
        return

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _write_bytes_atomic(config.jwt_private_key_path, private_bytes)
    _write_bytes_atomic(config.jwt_public_key_path, public_bytes)


def ensure_secrets_key(config: CognisConfig) -> None:
    """Generate AES-256-GCM key if missing."""
    if config.secrets_key_path.exists():
        return
    _write_bytes_atomic(config.secrets_key_path, base64.urlsafe_b64encode(os.urandom(32)))


async def run_schema_bootstrap(engine: AsyncEngine) -> None:
    """Create schema directly for MVP before/alongside Alembic use."""
    from cognis.store.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_session_lifecycle_columns)
        await conn.run_sync(_ensure_session_compaction_columns)
        await conn.run_sync(_ensure_api_key_columns)
        await conn.run_sync(_ensure_agent_sync_metadata_column)
        await conn.run_sync(_ensure_provider_is_default_column)
        await conn.run_sync(_ensure_active_session_id_column)
        await conn.run_sync(_ensure_task_expected_output_column)


def _ensure_session_lifecycle_columns(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "idle_since" not in session_columns:
        execute(text("ALTER TABLE sessions ADD COLUMN idle_since TIMESTAMP WITH TIME ZONE"))
    if "updated_at" not in session_columns:
        execute(
            text(
                "ALTER TABLE sessions ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"
            )
        )
        execute(text("UPDATE sessions SET updated_at = COALESCE(updated_at, started_at)"))


def _ensure_session_compaction_columns(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "previous_session_id" not in session_columns:
        execute(text("ALTER TABLE sessions ADD COLUMN previous_session_id VARCHAR"))
    if "completion_reason" not in session_columns:
        execute(text("ALTER TABLE sessions ADD COLUMN completion_reason VARCHAR"))


def _ensure_api_key_columns(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    api_key_columns = {column["name"] for column in inspector.get_columns("api_keys")}
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "last_used_at" not in api_key_columns:
        execute(text("ALTER TABLE api_keys ADD COLUMN last_used_at TIMESTAMP WITH TIME ZONE"))


def _ensure_agent_sync_metadata_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "sync_metadata" not in agent_columns:
        execute(text("ALTER TABLE agents ADD COLUMN sync_metadata JSON DEFAULT '{}'"))


def _ensure_provider_is_default_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        provider_columns = {column["name"] for column in inspector.get_columns("llm_providers")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "is_default" not in provider_columns:
        execute(text("ALTER TABLE llm_providers ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT 0"))


def _ensure_active_session_id_column(sync_conn: object) -> None:
    """Rename root_session_id → active_session_id on conversations table."""
    inspector = cast(Any, inspect(sync_conn))
    try:
        conv_columns = {column["name"] for column in inspector.get_columns("conversations")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "root_session_id" in conv_columns and "active_session_id" not in conv_columns:
        execute(
            text("ALTER TABLE conversations RENAME COLUMN root_session_id TO active_session_id")
        )
    elif "active_session_id" not in conv_columns:
        execute(text("ALTER TABLE conversations ADD COLUMN active_session_id VARCHAR"))


def _ensure_task_expected_output_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "expected_output" not in task_columns:
        execute(text("ALTER TABLE tasks ADD COLUMN expected_output TEXT"))


async def seed_default_settings(session: AsyncSession) -> None:
    """Seed application settings into the settings table."""
    for key, (category, value) in DEFAULT_SETTINGS.items():
        await upsert_setting(session, key=key, value=value, category=category)


async def maybe_seed_initial_admin(
    session: AsyncSession,
    config: CognisConfig,
    password_hasher: object,
) -> CognisConfig:
    """Seed initial admin from env vars if configured and no users exist."""
    if await count_users(session) != 0:
        return config
    if config.initial_admin_email is None or config.initial_admin_password is None:
        return config

    password_hash = password_hasher.hash(config.initial_admin_password)  # type: ignore[attr-defined]
    await create_user(
        session,
        email=config.initial_admin_email,
        name="Admin",
        password_hash=password_hash,
        role="admin",
    )
    os.environ.pop("COGNIS_INITIAL_ADMIN_PASSWORD", None)
    return replace(config, initial_admin_password=None)


async def bootstrap_runtime(
    config: CognisConfig,
    password_hasher: object,
) -> tuple[CognisConfig, AsyncEngine, async_sessionmaker[AsyncSession], SetupTokenManager]:
    """Run bootstrap and return engine/session factory/token manager."""
    ensure_data_dir(config)
    ensure_jwt_keypair(config)
    ensure_secrets_key(config)

    engine = create_engine(config.database_url)
    session_factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)

    async with session_factory() as session:
        await seed_default_settings(session)
        config = await maybe_seed_initial_admin(session, config, password_hasher)
        await session.commit()

    return config, engine, session_factory, SetupTokenManager()

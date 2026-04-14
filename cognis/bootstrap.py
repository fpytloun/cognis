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
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cognis.config import CognisConfig
from cognis.core.system_skills import SYSTEM_SKILL_DEFAULTS, get_system_skill_default
from cognis.logging import get_logger
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import (
    count_users,
    create_skill,
    create_skill_version,
    create_user,
    get_next_version_number,
    get_setting,
    get_skill,
    set_current_version,
    upsert_setting,
)
from cognis.tools.skill_parser import compute_content_hash

logger = get_logger(__name__)

DEFAULT_SETTINGS: Final[dict[str, tuple[str, object]]] = {
    "session.max_context_tokens": ("session", 250000),
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
    "executors.allow_in_process": ("executors", True),
    "executors.allow_subprocess": ("executors", True),
}

_BUILTIN_MANAGEMENT_SKILLS: Final[list[dict[str, object]]] = list(SYSTEM_SKILL_DEFAULTS.values())


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
    """Generate ES256 keypair if missing or incomplete.

    When ``config.require_external_crypto`` is True, raise instead of
    generating — production deployments must supply pre-generated keys.
    """
    private_exists = config.jwt_private_key_path.exists()
    public_exists = config.jwt_public_key_path.exists()
    if private_exists and public_exists:
        return

    if config.require_external_crypto:
        missing = []
        if not private_exists:
            missing.append(str(config.jwt_private_key_path))
        if not public_exists:
            missing.append(str(config.jwt_public_key_path))
        msg = (
            "COGNIS_REQUIRE_EXTERNAL_CRYPTO is enabled but JWT key files "
            f"are missing: {', '.join(missing)}. "
            "Mount pre-generated keys or disable the flag for local dev."
        )
        raise RuntimeError(msg)

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
    """Generate AES-256-GCM key if missing.

    When ``config.require_external_crypto`` is True, raise instead of
    generating.
    """
    if config.secrets_key_path.exists():
        return

    if config.require_external_crypto:
        msg = (
            "COGNIS_REQUIRE_EXTERNAL_CRYPTO is enabled but secrets key "
            f"is missing: {config.secrets_key_path}. "
            "Mount a pre-generated key or disable the flag for local dev."
        )
        raise RuntimeError(msg)

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
        await conn.run_sync(_ensure_step_run_conversation_id_column)
        await conn.run_sync(_ensure_agent_type_columns)
        await conn.run_sync(_ensure_user_management_columns)
        await conn.run_sync(_ensure_conversation_last_read_at)
        await conn.run_sync(_ensure_avatar_image_id_column)
        await conn.run_sync(_ensure_executor_runtime_state_columns)
        await conn.run_sync(_ensure_skill_versioning_columns)
        await conn.run_sync(_ensure_skill_system_column)
        await conn.run_sync(_ensure_schedule_extended_columns)
        await conn.run_sync(_ensure_conversation_title_source_column)
        await conn.run_sync(_ensure_mcp_server_headers_column)


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


def _ensure_conversation_title_source_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "title_source" not in conversation_columns:
        execute(
            text(
                "ALTER TABLE conversations ADD COLUMN title_source VARCHAR NOT NULL DEFAULT 'unset'"
            )
        )
        execute(
            text(
                "UPDATE conversations SET title_source = CASE "
                "WHEN title IS NULL OR TRIM(title) = '' THEN 'unset' "
                "ELSE 'manual' END"
            )
        )


def _ensure_step_run_conversation_id_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("step_runs")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "conversation_id" not in columns:
        execute(text("ALTER TABLE step_runs ADD COLUMN conversation_id VARCHAR"))


def _ensure_agent_type_columns(sync_conn: object) -> None:
    """Add agent_type, is_system, hidden columns to agents table."""
    inspector = cast(Any, inspect(sync_conn))
    try:
        agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "agent_type" not in agent_columns:
        execute(
            text("ALTER TABLE agents ADD COLUMN agent_type VARCHAR(20) NOT NULL DEFAULT 'primary'")
        )
    if "is_system" not in agent_columns:
        execute(text("ALTER TABLE agents ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT 0"))
    if "hidden" not in agent_columns:
        execute(text("ALTER TABLE agents ADD COLUMN hidden BOOLEAN NOT NULL DEFAULT 0"))


def _ensure_user_management_columns(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "is_active" not in user_columns:
        execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"))
    if "updated_at" not in user_columns:
        execute(text("ALTER TABLE users ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE"))
    if "last_login_at" not in user_columns:
        execute(text("ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP WITH TIME ZONE"))
    if "disabled_at" not in user_columns:
        execute(text("ALTER TABLE users ADD COLUMN disabled_at TIMESTAMP WITH TIME ZONE"))
    if "disabled_by" not in user_columns:
        execute(text("ALTER TABLE users ADD COLUMN disabled_by VARCHAR"))


def _ensure_conversation_last_read_at(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("conversations")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "last_read_at" not in columns:
        execute(text("ALTER TABLE conversations ADD COLUMN last_read_at TIMESTAMP"))


def _ensure_executor_runtime_state_columns(sync_conn: object) -> None:
    """Add executor runtime-state columns for existing databases."""
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("executors")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "desired_config_version" not in columns:
        execute(
            text(
                "ALTER TABLE executors ADD COLUMN desired_config_version INTEGER NOT NULL DEFAULT 0"
            )
        )
    if "applied_config_version" not in columns:
        execute(
            text(
                "ALTER TABLE executors ADD COLUMN applied_config_version INTEGER NOT NULL DEFAULT 0"
            )
        )
    if "observed_tools" not in columns:
        execute(text("ALTER TABLE executors ADD COLUMN observed_tools JSON"))
    if "runtime_metadata" not in columns:
        execute(text("ALTER TABLE executors ADD COLUMN runtime_metadata JSON"))
    if "last_observed_at" not in columns:
        execute(text("ALTER TABLE executors ADD COLUMN last_observed_at TIMESTAMP WITH TIME ZONE"))
    if "runtime_state" not in columns:
        execute(
            text(
                "ALTER TABLE executors ADD COLUMN runtime_state VARCHAR NOT NULL DEFAULT 'offline'"
            )
        )


def _ensure_avatar_image_id_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("agents")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "avatar_image_id" not in columns:
        execute(text("ALTER TABLE agents ADD COLUMN avatar_image_id VARCHAR"))


def _ensure_skill_versioning_columns(sync_conn: object) -> None:
    """Add skill versioning columns for existing databases."""
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("skills")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "current_version_id" not in columns:
        execute(text("ALTER TABLE skills ADD COLUMN current_version_id VARCHAR"))


def _ensure_skill_system_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("skills")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "is_system" not in columns:
        execute(text("ALTER TABLE skills ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT 0"))


def _ensure_schedule_extended_columns(sync_conn: object) -> None:
    """Add schedule type, error tracking, and heartbeat columns."""
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("schedules")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "description" not in columns:
        execute(text("ALTER TABLE schedules ADD COLUMN description TEXT"))
    if "schedule_type" not in columns:
        execute(
            text("ALTER TABLE schedules ADD COLUMN schedule_type VARCHAR NOT NULL DEFAULT 'cron'")
        )
    if "interval_seconds" not in columns:
        execute(text("ALTER TABLE schedules ADD COLUMN interval_seconds INTEGER"))
    if "one_shot_at" not in columns:
        execute(text("ALTER TABLE schedules ADD COLUMN one_shot_at TIMESTAMP WITH TIME ZONE"))
    if "timezone" not in columns:
        execute(text("ALTER TABLE schedules ADD COLUMN timezone VARCHAR NOT NULL DEFAULT 'UTC'"))
    if "max_concurrent_runs" not in columns:
        execute(
            text("ALTER TABLE schedules ADD COLUMN max_concurrent_runs INTEGER NOT NULL DEFAULT 1")
        )
    if "delete_after_run" not in columns:
        execute(
            text("ALTER TABLE schedules ADD COLUMN delete_after_run BOOLEAN NOT NULL DEFAULT false")
        )
    if "last_run_status" not in columns:
        execute(text("ALTER TABLE schedules ADD COLUMN last_run_status VARCHAR"))
    if "consecutive_errors" not in columns:
        execute(
            text("ALTER TABLE schedules ADD COLUMN consecutive_errors INTEGER NOT NULL DEFAULT 0")
        )
    if "disabled_reason" not in columns:
        execute(text("ALTER TABLE schedules ADD COLUMN disabled_reason TEXT"))
    if "suppress_empty" not in columns:
        execute(
            text("ALTER TABLE schedules ADD COLUMN suppress_empty BOOLEAN NOT NULL DEFAULT false")
        )
    # Make cron_expr nullable (PostgreSQL: drop NOT NULL constraint).
    # Idempotent — if already nullable, it's a no-op.
    # SQLite doesn't support ALTER COLUMN; create_all handles it there.
    import contextlib

    with contextlib.suppress(Exception):
        execute(text("ALTER TABLE schedules ALTER COLUMN cron_expr DROP NOT NULL"))


def _ensure_mcp_server_headers_column(sync_conn: object) -> None:
    """Add HTTP headers column for MCP server configs."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("mcp_servers")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "headers" not in columns:
        execute(text("ALTER TABLE mcp_servers ADD COLUMN headers JSON"))


_SYSTEM_USER_EMAIL = "system@cognis.local"


async def _ensure_system_user(session: AsyncSession) -> None:
    """Create the system user if it doesn't exist (for FK integrity)."""
    from cognis.store.models import User

    existing = await session.execute(select(User).where(User.email == _SYSTEM_USER_EMAIL))
    if existing.scalar_one_or_none() is not None:
        return
    session.add(
        User(
            email=_SYSTEM_USER_EMAIL,
            name="System",
            role="system",
        )
    )
    await session.flush()


async def seed_system_agents(session: AsyncSession) -> None:
    """Upsert system agent rows into the DB for FK integrity.

    System agents are defined as Python constants in AgentRegistry.
    The DB rows exist solely so that FK constraints (e.g. conversations.agent_id)
    work when a workflow step uses a system agent. The AgentRegistry still
    treats Python constants as authoritative.
    """
    from cognis.core.agent_registry import SYSTEM_AGENTS
    from cognis.store.models import Agent

    await _ensure_system_user(session)

    for agent_def in SYSTEM_AGENTS.values():
        existing = await session.execute(select(Agent).where(Agent.agent_id == agent_def.agent_id))
        if existing.scalar_one_or_none() is not None:
            continue
        session.add(
            Agent(
                agent_id=agent_def.agent_id,
                owner_email=_SYSTEM_USER_EMAIL,
                name=agent_def.name,
                description=agent_def.description,
                system_prompt=agent_def.system_prompt,
                tools=agent_def.tools if isinstance(agent_def.tools, dict) else None,
                agent_type=agent_def.agent_type,
                is_system=True,
                hidden=agent_def.hidden,
                status="active",
            )
        )
    await session.flush()


async def seed_default_settings(session: AsyncSession) -> None:
    """Seed application settings into the settings table.

    Only inserts defaults for keys that do not already exist.  Existing
    values (e.g. executor flags changed via the UI) are never overwritten.
    """
    for key, (category, value) in DEFAULT_SETTINGS.items():
        existing = await get_setting(session, key)
        if existing is None:
            await upsert_setting(session, key=key, value=value, category=category)
            continue

        # Upgrade the legacy default context cap from 128k to 250k while
        # preserving any user-customised value.
        if key == "session.max_context_tokens" and existing.value == 128000 and value == 250000:
            await upsert_setting(session, key=key, value=value, category=category)


async def seed_builtin_management_skills(session: AsyncSession) -> None:
    """Seed first-party Cognis management skills if they do not exist."""

    for skill in _BUILTIN_MANAGEMENT_SKILLS:
        defaults = get_system_skill_default(str(skill["skill_id"]))
        assert defaults is not None
        existing = await get_skill(session, str(skill["skill_id"]))
        if existing is not None:
            updates: dict[str, object] = {"is_system": True}
            if existing.owner_email is None and existing.current_version_id is None:
                updates.update(
                    {
                        "name": defaults["name"],
                        "description": defaults["description"],
                        "instructions": defaults["instructions"],
                        "tools": defaults["tools"],
                        "prompt_templates": defaults["prompt_templates"],
                        "tags": defaults["tags"],
                        "auto_load": False,
                    }
                )
            for key, value in updates.items():
                setattr(existing, key, value)
            if existing.current_version_id is None:
                content_hash = compute_content_hash(
                    existing.instructions,
                    existing.tools,
                    existing.prompt_templates,
                )
                version_row = await create_skill_version(
                    session,
                    skill_id=existing.skill_id,
                    version_number=await get_next_version_number(session, existing.skill_id),
                    content_hash=content_hash,
                    instructions=existing.instructions,
                    tools=existing.tools,
                    prompt_templates=existing.prompt_templates,
                    secret_placeholders=None,
                )
                await set_current_version(session, existing.skill_id, version_row.version_id)
                existing.current_version_id = version_row.version_id
            continue
        row = await create_skill(
            session,
            skill_id=str(skill["skill_id"]),
            name=str(defaults["name"]),
            description=(
                str(defaults["description"]) if defaults.get("description") is not None else None
            ),
            instructions=str(defaults["instructions"]),
            tools=defaults.get("tools"),
            prompt_templates=defaults.get("prompt_templates"),
            tags=list(defaults["tags"]),
            auto_load=False,
            is_system=True,
            source="db",
            owner_email=None,
        )
        version_row = await create_skill_version(
            session,
            skill_id=row.skill_id,
            version_number=1,
            content_hash=compute_content_hash(
                row.instructions,
                row.tools,
                row.prompt_templates,
            ),
            instructions=row.instructions,
            tools=row.tools,
            prompt_templates=row.prompt_templates,
            secret_placeholders=None,
        )
        await set_current_version(session, row.skill_id, version_row.version_id)
        row.current_version_id = version_row.version_id


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
        await seed_system_agents(session)
        await seed_builtin_management_skills(session)
        config = await maybe_seed_initial_admin(session, config, password_hasher)
        await session.commit()

    return config, engine, session_factory, SetupTokenManager()

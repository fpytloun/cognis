"""Bootstrap services for filesystem, keys, database, and default settings."""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cognis.config import CognisConfig
from cognis.core.system_skills import SYSTEM_SKILL_DEFAULTS, get_system_skill_default
from cognis.logging import get_logger
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.settings_schema import DEFAULT_SETTINGS
from cognis.store.database import create_engine, create_session_factory
from cognis.store.migrations.compat import normalize_legacy_profile_override_revision
from cognis.store.models import Base
from cognis.store.queries import (
    count_users,
    create_skill,
    create_skill_version,
    create_user,
    get_next_version_number,
    get_setting,
    get_skill,
    get_skill_version,
    set_current_version,
    upsert_setting,
)
from cognis.tools.skill_parser import compute_content_hash

logger = get_logger(__name__)

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

    async with engine.begin() as conn:
        await conn.run_sync(normalize_legacy_profile_override_revision)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_session_lifecycle_columns)
        await conn.run_sync(_ensure_delegate_lineage_column)
        await conn.run_sync(_ensure_session_compaction_columns)
        await conn.run_sync(_ensure_api_key_columns)
        await conn.run_sync(_ensure_agent_capabilities_column)
        await conn.run_sync(_ensure_agent_sync_metadata_column)
        await conn.run_sync(_ensure_provider_is_default_column)
        await conn.run_sync(_ensure_llm_provider_owner_schema)
        await conn.run_sync(_ensure_active_session_id_column)
        await conn.run_sync(_ensure_task_expected_output_column)
        await conn.run_sync(_ensure_step_run_conversation_id_column)
        await conn.run_sync(_ensure_agent_type_columns)
        await conn.run_sync(_ensure_user_management_columns)
        await conn.run_sync(_ensure_conversation_last_read_at)
        await conn.run_sync(_ensure_conversation_active_executor_id)
        await conn.run_sync(_ensure_task_active_executor_id)
        await conn.run_sync(_ensure_active_executor_lifecycle_columns)
        await conn.run_sync(_ensure_avatar_image_id_column)
        await conn.run_sync(_ensure_executor_runtime_state_columns)
        await conn.run_sync(_ensure_executor_token_version_column)
        await conn.run_sync(_ensure_skill_versioning_columns)
        await conn.run_sync(_ensure_skill_linked_tools_column)
        await conn.run_sync(_ensure_skill_decomposition_columns)
        await conn.run_sync(_ensure_skill_system_column)
        await conn.run_sync(_ensure_schedule_extended_columns)
        await conn.run_sync(_ensure_conversation_title_source_column)
        await conn.run_sync(_ensure_conversation_starred_at_column)
        await conn.run_sync(_ensure_conversation_sidebar_activity_index)
        await conn.run_sync(_ensure_mcp_server_headers_column)
        await conn.run_sync(_ensure_mcp_oauth_schema)
        await conn.run_sync(_ensure_system_override_tables)
        await conn.run_sync(_ensure_task_execution_paths)
        await conn.run_sync(_ensure_task_completion_delivery_columns)
        await conn.run_sync(_ensure_task_interaction_override_columns)
        await conn.run_sync(_ensure_task_creator_agent_column)
        await conn.run_sync(_ensure_task_session_policy_column)
        await conn.run_sync(_ensure_task_board_indexes)
        await conn.run_sync(_ensure_agent_profile_columns)
        await conn.run_sync(_ensure_managed_conversation_lineage)
        await conn.run_sync(_ensure_step_run_execution_paths)
        await conn.run_sync(_ensure_deliverables_table)
        await conn.run_sync(_ensure_step_run_deliverable_columns)
        await conn.run_sync(_ensure_step_run_runtime_info_column)
        await conn.run_sync(_ensure_canonical_chart_payloads)
        await conn.run_sync(_ensure_workflow_lifecycle_columns)
        await conn.run_sync(_ensure_system_agent_override_columns)
        await conn.run_sync(_ensure_agent_grants_table)
        await conn.run_sync(_ensure_agent_grant_overrides_column)
        await conn.run_sync(_ensure_harness_recovery_tables)
        await conn.run_sync(_ensure_tool_classification_table)
        await conn.run_sync(_ensure_tool_classification_override_table)
        await conn.run_sync(_ensure_browser_sessions_table)
        await conn.run_sync(_ensure_push_subscriptions_table)
        await conn.run_sync(_ensure_channel_preferred_delivery_column)
        await conn.run_sync(_ensure_projects_tables)
        await conn.run_sync(_ensure_project_links_workflows_grants)
        await conn.run_sync(_ensure_step_history_columns)
        await conn.run_sync(_ensure_task_comments_table)
        await conn.run_sync(_ensure_tts_cache_table)
        await conn.run_sync(_ensure_knowledgebase_schema)
        await conn.run_sync(_ensure_todos_tables)
        await conn.run_sync(_ensure_local_model_runtime_columns)
        await conn.run_sync(_ensure_local_model_byte_counter_types)
        await conn.run_sync(_ensure_local_model_provider_columns)
        await conn.run_sync(_ensure_channel_delivery_progress_columns)


def _ensure_canonical_chart_payloads(sync_conn: object) -> None:
    """Idempotently upgrade supported inline legacy chart payloads."""

    from cognis.rendering.rich_visuals import upgrade_legacy_chart_payload

    inspector = cast(Any, inspect(sync_conn))
    if "deliverables" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("deliverables")}
    if not {"deliverable_id", "rich_payload"}.issubset(columns):
        return

    metadata = sa.MetaData()
    deliverables = sa.Table("deliverables", metadata, autoload_with=cast(Any, sync_conn))
    cursor: str | None = None
    while True:
        query = (
            sa.select(deliverables.c.deliverable_id, deliverables.c.rich_payload)
            .where(deliverables.c.rich_payload.is_not(None))
            .order_by(deliverables.c.deliverable_id)
            .limit(100)
        )
        if cursor is not None:
            query = query.where(deliverables.c.deliverable_id > cursor)
        rows = list(sync_conn.execute(query))  # type: ignore[attr-defined]
        if not rows:
            return
        for deliverable_id, payload in rows:
            cursor = str(deliverable_id)
            if not isinstance(payload, dict):
                continue
            result = upgrade_legacy_chart_payload(payload)
            if result.upgraded_blocks:
                sync_conn.execute(  # type: ignore[attr-defined]
                    deliverables.update()
                    .where(deliverables.c.deliverable_id == deliverable_id)
                    .values(rich_payload=result.payload)
                )


def _ensure_channel_delivery_progress_columns(sync_conn: object) -> None:
    """Apply additive channel outbox migrations during normal bootstrap."""

    inspector = cast(Any, inspect(sync_conn))
    if "channel_delivery_outbox" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("channel_delivery_outbox")}
    execute = sync_conn.execute  # type: ignore[attr-defined]
    if "completed_chunk_count" not in columns:
        execute(
            text(
                "ALTER TABLE channel_delivery_outbox "
                "ADD COLUMN completed_chunk_count INTEGER NOT NULL DEFAULT 0"
            )
        )
    if "projected_chunk_count" not in columns:
        execute(
            text("ALTER TABLE channel_delivery_outbox ADD COLUMN projected_chunk_count INTEGER")
        )
    if "projection_digest" not in columns:
        execute(text("ALTER TABLE channel_delivery_outbox ADD COLUMN projection_digest VARCHAR"))
    if "inflight_chunk_index" not in columns:
        execute(text("ALTER TABLE channel_delivery_outbox ADD COLUMN inflight_chunk_index INTEGER"))
    if "inflight_idempotent" not in columns:
        execute(text("ALTER TABLE channel_delivery_outbox ADD COLUMN inflight_idempotent BOOLEAN"))
    if "attachments_json" not in columns:
        execute(text("ALTER TABLE channel_delivery_outbox ADD COLUMN attachments_json JSON"))
    if "deliverable_id" not in columns:
        execute(text("ALTER TABLE channel_delivery_outbox ADD COLUMN deliverable_id VARCHAR"))


def _ensure_task_creator_agent_column(sync_conn: object) -> None:
    """Add the optional task author-agent marker for delegated task control."""

    inspector = cast(Any, inspect(sync_conn))
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "created_by_agent_id" not in task_columns:
        sync_conn.execute(  # type: ignore[attr-defined]
            text("ALTER TABLE tasks ADD COLUMN created_by_agent_id VARCHAR")
        )


def _ensure_local_model_runtime_columns(sync_conn: object) -> None:
    """Add operation intent used by the managed Ollama runtime."""

    inspector = cast(Any, inspect(sync_conn))
    if "local_model_operations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("local_model_operations")}
    if "post_pull_provider_upsert" not in columns:
        dialect_name = str(getattr(getattr(sync_conn, "dialect", None), "name", ""))
        boolean_default = "FALSE" if dialect_name == "postgresql" else "0"
        sync_conn.execute(  # type: ignore[attr-defined]
            text(
                "ALTER TABLE local_model_operations "
                f"ADD COLUMN post_pull_provider_upsert "
                f"BOOLEAN NOT NULL DEFAULT {boolean_default}"
            )
        )


def _ensure_local_model_byte_counter_types(sync_conn: object) -> None:
    """Widen legacy PostgreSQL local-model byte counters without rebuilding SQLite."""

    dialect_name = str(getattr(getattr(sync_conn, "dialect", None), "name", ""))
    if dialect_name != "postgresql":
        # SQLite INTEGER already stores signed 64-bit values. Rebuilding these related
        # tables during every bootstrap would be less safe than retaining its affinity.
        return
    inspector = cast(Any, inspect(sync_conn))
    tables = set(inspector.get_table_names())
    execute = sync_conn.execute  # type: ignore[attr-defined]
    for table_name, column_name in (
        ("local_model_operations", "progress_bytes"),
        ("local_model_target_statuses", "observed_size_bytes"),
    ):
        if table_name not in tables:
            continue
        column = next(
            (
                candidate
                for candidate in inspector.get_columns(table_name)
                if candidate["name"] == column_name
            ),
            None,
        )
        column_type = column["type"] if column is not None else None
        if isinstance(column_type, sa.BigInteger):
            continue
        if isinstance(column_type, sa.Integer):
            execute(text(f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE BIGINT"))


def _ensure_local_model_provider_columns(sync_conn: object) -> None:
    """Add the reusable managed-provider identity used by local models."""

    inspector = cast(Any, inspect(sync_conn))
    if "llm_providers" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("llm_providers")}
    if "managed_local_key" not in columns:
        sync_conn.execute(  # type: ignore[attr-defined]
            text("ALTER TABLE llm_providers ADD COLUMN managed_local_key VARCHAR")
        )
    indexes = {index["name"] for index in inspector.get_indexes("llm_providers")}
    if "uq_llm_providers_managed_local_key" not in indexes:
        sync_conn.execute(  # type: ignore[attr-defined]
            text(
                "CREATE UNIQUE INDEX uq_llm_providers_managed_local_key "
                "ON llm_providers (managed_local_key)"
            )
        )


def _ensure_task_session_policy_column(sync_conn: object) -> None:
    """Add optional task session policy storage."""

    inspector = cast(Any, inspect(sync_conn))
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "session_policy" not in task_columns:
        sync_conn.execute(  # type: ignore[attr-defined]
            text("ALTER TABLE tasks ADD COLUMN session_policy JSON")
        )


def _ensure_knowledgebase_schema(sync_conn: object) -> None:
    """Create optional knowledgebase tables and additive artifact columns."""

    from cognis.store.models import (
        KnowledgebaseArtifactRow,
        KnowledgebaseChunkRow,
        KnowledgebaseIndexJobRow,
        KnowledgebaseRow,
    )

    KnowledgebaseRow.__table__.create(bind=sync_conn, checkfirst=True)
    KnowledgebaseArtifactRow.__table__.create(bind=sync_conn, checkfirst=True)
    KnowledgebaseChunkRow.__table__.create(bind=sync_conn, checkfirst=True)
    KnowledgebaseIndexJobRow.__table__.create(bind=sync_conn, checkfirst=True)

    inspector = cast(Any, inspect(sync_conn))
    artifact_columns = {column["name"] for column in inspector.get_columns("artifacts")}
    execute = sync_conn.execute  # type: ignore[attr-defined]
    if "content_hash" not in artifact_columns:
        execute(text("ALTER TABLE artifacts ADD COLUMN content_hash VARCHAR"))
    if "source_tool_call_id" not in artifact_columns:
        execute(text("ALTER TABLE artifacts ADD COLUMN source_tool_call_id VARCHAR"))
    if "source_anchor" not in artifact_columns:
        execute(text("ALTER TABLE artifacts ADD COLUMN source_anchor VARCHAR"))
    if "updated_at" not in artifact_columns:
        execute(
            text(
                "ALTER TABLE artifacts ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE "
                "DEFAULT CURRENT_TIMESTAMP"
            )
        )

    # Keep production startup equivalent to Alembic revision 094.  Every
    # mutation is NULL-only or guarded so repeated startup remains deterministic
    # and old controller versions can safely ignore the additive schema.
    artifacts = sa.Table("artifacts", sa.MetaData(), autoload_with=cast(Any, sync_conn))
    execute(
        artifacts.update()
        .where(
            artifacts.c.purpose == "tool_artifact",
            artifacts.c.source_tool_call_id.is_(None),
            artifacts.c.conversation_id.is_not(None),
        )
        .values(source_tool_call_id=artifacts.c.conversation_id)
    )
    execute(
        artifacts.update()
        .where(
            artifacts.c.purpose == "tool_artifact",
            artifacts.c.source_anchor.is_(None),
            artifacts.c.session_id.is_not(None),
        )
        .values(source_anchor=artifacts.c.session_id)
    )
    legacy_tool_outputs = execute(
        sa.select(artifacts.c.artifact_id, artifacts.c.filename).where(
            artifacts.c.purpose == "tool_output",
            artifacts.c.source_tool_call_id.is_(None),
            artifacts.c.filename.is_not(None),
        )
    ).all()
    for artifact_id, filename in legacy_tool_outputs:
        filename_text = str(filename or "")
        if not filename_text.endswith(".txt"):
            continue
        source_tool_call_id = filename_text[:-4].strip()
        if not source_tool_call_id:
            continue
        execute(
            artifacts.update()
            .where(
                artifacts.c.artifact_id == artifact_id,
                artifacts.c.source_tool_call_id.is_(None),
            )
            .values(source_tool_call_id=source_tool_call_id)
        )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_artifacts_tool_source "
            "ON artifacts (owner_email, source_tool_call_id, source_anchor)"
        )
    )


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


def _ensure_delegate_lineage_column(sync_conn: object) -> None:
    """Add explicit lightweight delegate lineage metadata."""

    inspector = cast(Any, inspect(sync_conn))
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "delegation_metadata" not in session_columns:
        sync_conn.execute(  # type: ignore[attr-defined]
            text("ALTER TABLE sessions ADD COLUMN delegation_metadata JSON")
        )
        sync_conn.execute(  # type: ignore[attr-defined]
            text("UPDATE sessions SET delegation_metadata = '{}' WHERE delegation_metadata IS NULL")
        )


def _ensure_todos_tables(sync_conn: object) -> None:
    """Create first-class TODO state tables."""

    from cognis.store.models import ConversationTodo, SessionTodo

    ConversationTodo.__table__.create(bind=sync_conn, checkfirst=True)
    SessionTodo.__table__.create(bind=sync_conn, checkfirst=True)
    sync_conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO conversation_todos (
                conversation_id,
                position,
                content,
                status,
                priority,
                created_at,
                updated_at
            )
            SELECT
                selected.conversation_id,
                st.position,
                st.content,
                st.status,
                st.priority,
                st.created_at,
                st.updated_at
            FROM (
                SELECT
                    s.conversation_id AS conversation_id,
                    s.session_id AS session_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.conversation_id
                        ORDER BY
                            CASE WHEN c.active_session_id = s.session_id THEN 0 ELSE 1 END,
                            s.updated_at DESC,
                            s.started_at DESC
                    ) AS rn
                FROM sessions s
                JOIN conversations c ON c.conversation_id = s.conversation_id
                WHERE (
                    c.active_session_id = s.session_id
                    OR (c.active_session_id IS NULL AND s.status IN ('active', 'idle'))
                )
            ) selected
            JOIN session_todos st ON st.session_id = selected.session_id
            WHERE selected.rn = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM conversation_todos existing
                  WHERE existing.conversation_id = selected.conversation_id
              )
            """
        )
    )


def _ensure_session_compaction_columns(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "previous_session_id" not in session_columns:
        execute(text("ALTER TABLE sessions ADD COLUMN previous_session_id VARCHAR"))
    if "completion_reason" not in session_columns:
        execute(text("ALTER TABLE sessions ADD COLUMN completion_reason VARCHAR"))
    if "result_content" not in session_columns:
        execute(text("ALTER TABLE sessions ADD COLUMN result_content TEXT"))


def _ensure_api_key_columns(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    api_key_columns = {column["name"] for column in inspector.get_columns("api_keys")}
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "last_used_at" not in api_key_columns:
        execute(text("ALTER TABLE api_keys ADD COLUMN last_used_at TIMESTAMP WITH TIME ZONE"))


def _ensure_harness_recovery_tables(sync_conn: object) -> None:
    """Create durable recovery tables introduced after the MVP bootstrap."""

    from cognis.store.models import FollowUpDedupeRow, FollowUpIntentRow, RememberQueueRow

    RememberQueueRow.__table__.create(bind=sync_conn, checkfirst=True)
    FollowUpDedupeRow.__table__.create(bind=sync_conn, checkfirst=True)
    FollowUpIntentRow.__table__.create(bind=sync_conn, checkfirst=True)

    inspector = cast(Any, inspect(sync_conn))
    execute = sync_conn.execute  # type: ignore[attr-defined]
    for table_name in ("follow_up_intents", "follow_up_dedupe"):
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "lease_owner" not in columns:
            execute(text(f"ALTER TABLE {table_name} ADD COLUMN lease_owner VARCHAR"))
        if "lease_expires_at" not in columns:
            execute(
                text(
                    f"ALTER TABLE {table_name} ADD COLUMN lease_expires_at TIMESTAMP WITH TIME ZONE"
                )
            )

    inspector = cast(Any, inspect(sync_conn))
    intent_indexes = {index["name"] for index in inspector.get_indexes("follow_up_intents")}
    if "ix_follow_up_intents_lease" not in intent_indexes:
        execute(
            text(
                "CREATE INDEX ix_follow_up_intents_lease "
                "ON follow_up_intents (status, lease_expires_at)"
            )
        )
    dedupe_indexes = {index["name"] for index in inspector.get_indexes("follow_up_dedupe")}
    if "ix_follow_up_dedupe_lease" not in dedupe_indexes:
        execute(
            text(
                "CREATE INDEX ix_follow_up_dedupe_lease "
                "ON follow_up_dedupe (status, lease_expires_at)"
            )
        )


def _ensure_tool_classification_table(sync_conn: object) -> None:
    """Create the durable tool classification table."""

    from cognis.store.models import ToolClassificationRow

    ToolClassificationRow.__table__.create(bind=sync_conn, checkfirst=True)


def _ensure_tool_classification_override_table(sync_conn: object) -> None:
    """Create the durable tool classification override table."""

    from cognis.store.models import ToolClassificationOverrideRow

    ToolClassificationOverrideRow.__table__.create(bind=sync_conn, checkfirst=True)


def _ensure_browser_sessions_table(sync_conn: object) -> None:
    """Create the durable browser session table."""

    from cognis.store.models import BrowserSession

    BrowserSession.__table__.create(bind=sync_conn, checkfirst=True)


def _ensure_push_subscriptions_table(sync_conn: object) -> None:
    """Create the browser Web Push subscriptions table."""

    from cognis.store.models import PushSubscriptionRow

    PushSubscriptionRow.__table__.create(bind=sync_conn, checkfirst=True)


def _ensure_channel_preferred_delivery_column(sync_conn: object) -> None:
    """Add preferred task delivery marker to existing channel accounts."""

    inspector = cast(Any, inspect(sync_conn))
    channel_columns = {column["name"] for column in inspector.get_columns("channel_accounts")}
    if "preferred_for_task_delivery" not in channel_columns:
        dialect_name = cast(Any, sync_conn).dialect.name
        default = "FALSE" if dialect_name == "postgresql" else "0"
        sync_conn.execute(  # type: ignore[attr-defined]
            text(
                "ALTER TABLE channel_accounts "
                f"ADD COLUMN preferred_for_task_delivery BOOLEAN NOT NULL DEFAULT {default}"
            )
        )


def _ensure_projects_tables(sync_conn: object) -> None:
    """Create durable project and project source tables."""

    from cognis.store.models import ProjectRow, ProjectSourceRow

    ProjectRow.__table__.create(bind=sync_conn, checkfirst=True)
    ProjectSourceRow.__table__.create(bind=sync_conn, checkfirst=True)


def _ensure_project_links_workflows_grants(sync_conn: object) -> None:
    """Create project link/grant tables and nullable project link columns."""

    from cognis.store.models import ProjectGrantRow, ProjectWorkflowRow

    ProjectWorkflowRow.__table__.create(bind=sync_conn, checkfirst=True)
    ProjectGrantRow.__table__.create(bind=sync_conn, checkfirst=True)
    inspector = cast(Any, inspect(sync_conn))
    execute = sync_conn.execute  # type: ignore[attr-defined]
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    schedule_columns = {column["name"] for column in inspector.get_columns("schedules")}
    conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "project_id" not in task_columns:
        execute(text("ALTER TABLE tasks ADD COLUMN project_id VARCHAR"))
    if "attempt_number" not in task_columns:
        execute(text("ALTER TABLE tasks ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1"))
    if "project_id" not in schedule_columns:
        execute(text("ALTER TABLE schedules ADD COLUMN project_id VARCHAR"))
    if "project_id" not in conversation_columns:
        execute(text("ALTER TABLE conversations ADD COLUMN project_id VARCHAR"))
    execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_project_id ON tasks (project_id)"))
    execute(text("CREATE INDEX IF NOT EXISTS ix_schedules_project_id ON schedules (project_id)"))
    execute(
        text("CREATE INDEX IF NOT EXISTS ix_conversations_project_id ON conversations (project_id)")
    )


def _ensure_task_board_indexes(sync_conn: object) -> None:
    """Create indexes used by paginated task list and kanban board queries."""

    execute = sync_conn.execute  # type: ignore[attr-defined]
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tasks_owner_updated "
            "ON tasks (created_by, updated_at, task_id)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tasks_owner_status_updated "
            "ON tasks (created_by, status, updated_at, task_id)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tasks_owner_agent_updated "
            "ON tasks (created_by, agent_id, updated_at, task_id)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tasks_owner_project_updated "
            "ON tasks (created_by, project_id, updated_at, task_id)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tasks_owner_workflow_updated "
            "ON tasks (created_by, workflow_id, updated_at, task_id)"
        )
    )


def _ensure_step_history_columns(sync_conn: object) -> None:
    """Add revision-history columns needed by later Stage 33 phases."""

    inspector = cast(Any, inspect(sync_conn))
    execute = sync_conn.execute  # type: ignore[attr-defined]
    step_columns = {column["name"] for column in inspector.get_columns("step_runs")}
    deliverable_columns = {column["name"] for column in inspector.get_columns("deliverables")}
    if "attempt_number" not in step_columns:
        execute(text("ALTER TABLE step_runs ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1"))
    if "superseded_by_step_run_id" not in step_columns:
        execute(text("ALTER TABLE step_runs ADD COLUMN superseded_by_step_run_id VARCHAR"))
    if "attempt_number" not in deliverable_columns:
        execute(
            text("ALTER TABLE deliverables ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1")
        )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_step_runs_superseded_by "
            "ON step_runs (superseded_by_step_run_id)"
        )
    )


def _ensure_task_comments_table(sync_conn: object) -> None:
    """Create durable task comments table."""

    from cognis.store.models import TaskCommentRow

    TaskCommentRow.__table__.create(bind=sync_conn, checkfirst=True)


def _ensure_tts_cache_table(sync_conn: object) -> None:
    """Create the TTS audio cache metadata table."""

    from cognis.store.models import TtsCacheRow

    TtsCacheRow.__table__.create(bind=sync_conn, checkfirst=True)


def _ensure_agent_grants_table(sync_conn: object) -> None:
    """Create the durable agent grants table."""

    from cognis.store.models import AgentGrantRow

    AgentGrantRow.__table__.create(bind=sync_conn, checkfirst=True)


def _ensure_agent_grant_overrides_column(sync_conn: object) -> None:
    """Add per-grantee override storage to existing agent grant tables."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("agent_grants")}
    except Exception:
        return
    if "grantee_overrides" not in columns:
        sync_conn.execute(text("ALTER TABLE agent_grants ADD COLUMN grantee_overrides JSON"))  # type: ignore[attr-defined]


def _ensure_agent_capabilities_column(sync_conn: object) -> None:
    """Add capabilities JSON column to agents table (idempotent)."""
    inspector = cast(Any, inspect(sync_conn))
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "capabilities" not in agent_columns:
        execute(text("ALTER TABLE agents ADD COLUMN capabilities JSON"))


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


def _ensure_llm_provider_owner_schema(sync_conn: object) -> None:
    """Add owner-scoped LLM provider/routing columns for existing databases."""

    from cognis.store.models import LLMProviderAuthSession

    inspector = cast(Any, inspect(sync_conn))
    execute = sync_conn.execute  # type: ignore[attr-defined]
    dialect = sync_conn.dialect.name  # type: ignore[attr-defined]
    system_owner = SYSTEM_USER_EMAIL.replace("'", "''")

    try:
        provider_columns = {column["name"] for column in inspector.get_columns("llm_providers")}
    except Exception:
        provider_columns = set()
    if provider_columns and "owner_email" not in provider_columns:
        execute(
            text(
                "ALTER TABLE llm_providers "
                f"ADD COLUMN owner_email VARCHAR NOT NULL DEFAULT '{system_owner}'"
            )
        )

    try:
        routing_columns = {column["name"] for column in inspector.get_columns("model_routing")}
    except Exception:
        routing_columns = set()
    if routing_columns:
        if "route_id" not in routing_columns:
            execute(text("ALTER TABLE model_routing ADD COLUMN route_id VARCHAR"))
            execute(
                text(
                    "UPDATE model_routing SET route_id = 'route_' || task_type "
                    "WHERE route_id IS NULL"
                )
            )
            routing_columns.add("route_id")
        if "owner_email" not in routing_columns:
            execute(
                text(
                    "ALTER TABLE model_routing "
                    f"ADD COLUMN owner_email VARCHAR NOT NULL DEFAULT '{system_owner}'"
                )
            )
            routing_columns.add("owner_email")
        if dialect == "postgresql":
            execute(
                text(
                    "UPDATE model_routing SET route_id = 'route_' || task_type "
                    "WHERE route_id IS NULL"
                )
            )
            execute(text("ALTER TABLE model_routing ALTER COLUMN route_id SET NOT NULL"))
            pk = inspector.get_pk_constraint("model_routing")
            if pk.get("constrained_columns") != ["route_id"]:
                preparer = sync_conn.dialect.identifier_preparer  # type: ignore[attr-defined]
                pk_name = pk.get("name")
                if pk_name:
                    execute(
                        text(
                            "ALTER TABLE model_routing DROP CONSTRAINT "
                            f"{preparer.quote(str(pk_name))}"
                        )
                    )
                execute(text("ALTER TABLE model_routing ADD PRIMARY KEY (route_id)"))
            execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_routing_owner_task "
                    "ON model_routing (owner_email, task_type)"
                )
            )

    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_llm_providers_owner_email ON llm_providers (owner_email)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_llm_providers_owner_provider "
            "ON llm_providers (owner_email, provider_id)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_llm_providers_owner_default "
            "ON llm_providers (owner_email, is_default)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_model_routing_owner_email ON model_routing (owner_email)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_model_routing_owner_task "
            "ON model_routing (owner_email, task_type)"
        )
    )
    LLMProviderAuthSession.__table__.create(bind=sync_conn, checkfirst=True)


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


def _ensure_conversation_starred_at_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    except Exception:
        return
    if "starred_at" not in conversation_columns:
        sync_conn.execute(  # type: ignore[attr-defined]
            text("ALTER TABLE conversations ADD COLUMN starred_at TIMESTAMP WITH TIME ZONE")
        )


def _ensure_conversation_sidebar_activity_index(sync_conn: object) -> None:
    """Backfill and index conversation activity for sidebar list queries."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("conversations")}
    except Exception:
        return
    if "last_message_at" not in columns or "created_at" not in columns:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]
    execute(
        text("UPDATE conversations SET last_message_at = created_at WHERE last_message_at IS NULL")
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_conversations_owner_activity "
            "ON conversations (user_email, status, last_message_at, created_at)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_conversations_owner_agent_context "
            "ON conversations (user_email, status, agent_id, context_type)"
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


def _ensure_conversation_active_executor_id(sync_conn: object) -> None:
    """Add conversations.active_executor_id (Stage 36 multi-executor agents)."""
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("conversations")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "active_executor_id" not in columns:
        execute(text("ALTER TABLE conversations ADD COLUMN active_executor_id VARCHAR"))


def _ensure_task_active_executor_id(sync_conn: object) -> None:
    """Add tasks.active_executor_id (Stage 36 task-level executor pin).

    Workflow steps each create their own conversation; without a task-level
    pin, every step would re-pick a primary executor independently. The task
    pin is the durable carrier of the agent's executor choice across all
    steps of a single task.
    """
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("tasks")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "active_executor_id" not in columns:
        execute(text("ALTER TABLE tasks ADD COLUMN active_executor_id VARCHAR"))


def _ensure_active_executor_lifecycle_columns(sync_conn: object) -> None:
    """Add active-executor assignment metadata for conversations and tasks."""

    inspector = cast(Any, inspect(sync_conn))
    execute = sync_conn.execute  # type: ignore[attr-defined]

    for table_name in ("conversations", "tasks"):
        try:
            columns = {column["name"] for column in inspector.get_columns(table_name)}
        except Exception:
            continue
        if "active_executor_assigned_at" not in columns:
            execute(
                text(
                    f"ALTER TABLE {table_name} "
                    "ADD COLUMN active_executor_assigned_at TIMESTAMP WITH TIME ZONE"
                )
            )
        if "active_executor_expires_at" not in columns:
            execute(
                text(
                    f"ALTER TABLE {table_name} "
                    "ADD COLUMN active_executor_expires_at TIMESTAMP WITH TIME ZONE"
                )
            )
        if "active_executor_source" not in columns:
            execute(text(f"ALTER TABLE {table_name} ADD COLUMN active_executor_source VARCHAR"))


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


def _ensure_executor_token_version_column(sync_conn: object) -> None:
    """Add revokable executor token version for existing databases."""
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("executors")}
    except Exception:
        return  # table doesn't exist yet (create_all will handle it)
    if "token_version" not in columns:
        sync_conn.execute(  # type: ignore[attr-defined]
            text("ALTER TABLE executors ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
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


def _ensure_skill_linked_tools_column(sync_conn: object) -> None:
    """Add linked_tool_ids column to skills when missing."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("skills")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "linked_tool_ids" not in columns:
        execute(text("ALTER TABLE skills ADD COLUMN linked_tool_ids JSON"))


def _ensure_skill_system_column(sync_conn: object) -> None:
    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("skills")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "is_system" not in columns:
        execute(text("ALTER TABLE skills ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT FALSE"))


def _ensure_skill_decomposition_columns(sync_conn: object) -> None:
    """Add skill decomposition columns to skill_versions when missing."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("skill_versions")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "steps" not in columns:
        execute(text("ALTER TABLE skill_versions ADD COLUMN steps JSON"))
    if "linked_tool_ids" not in columns:
        execute(text("ALTER TABLE skill_versions ADD COLUMN linked_tool_ids JSON"))
    if "decomposition_source_hash" not in columns:
        execute(text("ALTER TABLE skill_versions ADD COLUMN decomposition_source_hash VARCHAR"))


def _ensure_schedule_extended_columns(sync_conn: object) -> None:
    """Add schedule type, error tracking, and completion delivery columns."""
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
    if "skill_id" not in columns:
        execute(text("ALTER TABLE schedules ADD COLUMN skill_id VARCHAR"))
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
    if "completion_mode_family" not in columns:
        execute(
            text(
                "ALTER TABLE schedules ADD COLUMN completion_mode_family VARCHAR NOT NULL DEFAULT 'default'"
            )
        )
    if "allow_silent_completion" not in columns:
        execute(
            text(
                "ALTER TABLE schedules ADD COLUMN allow_silent_completion BOOLEAN NOT NULL DEFAULT false"
            )
        )
    if "interaction_mode_override" not in columns:
        execute(
            text(
                "ALTER TABLE schedules ADD COLUMN interaction_mode_override VARCHAR DEFAULT 'none'"
            )
        )
    if "suppress_empty" in columns:
        execute(
            text(
                "UPDATE schedules SET allow_silent_completion = suppress_empty "
                "WHERE allow_silent_completion = false"
            )
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


def _ensure_mcp_oauth_schema(sync_conn: object) -> None:
    """Add MCP OAuth config column and encrypted token/transaction tables."""

    from cognis.store.models import MCPOAuthTokenRow, MCPOAuthTransactionRow

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("mcp_servers")}
    except Exception:
        columns = set()
    execute = sync_conn.execute  # type: ignore[attr-defined]
    if columns and "auth_config" not in columns:
        execute(text("ALTER TABLE mcp_servers ADD COLUMN auth_config JSON"))
    MCPOAuthTokenRow.__table__.create(sync_conn, checkfirst=True)
    MCPOAuthTransactionRow.__table__.create(sync_conn, checkfirst=True)
    try:
        token_columns = {column["name"] for column in inspector.get_columns("mcp_oauth_tokens")}
    except Exception:
        token_columns = set()
    dialect_name = getattr(getattr(sync_conn, "dialect", None), "name", "")
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if dialect_name == "postgresql" else "TIMESTAMP"
    additions = {
        "refresh_failure_count": "INTEGER NOT NULL DEFAULT 0",
        "next_refresh_attempt_at": timestamp_type,
        "last_refresh_error_code": "VARCHAR",
        "last_refresh_error_description": "TEXT",
        "last_refresh_error_at": timestamp_type,
    }
    for name, sql_type in additions.items():
        if token_columns and name not in token_columns:
            execute(text(f"ALTER TABLE mcp_oauth_tokens ADD COLUMN {name} {sql_type}"))
    for index in MCPOAuthTokenRow.__table__.indexes:
        index.create(sync_conn, checkfirst=True)


def _ensure_system_override_tables(sync_conn: object) -> None:
    """Create per-user system override tables when missing."""

    from cognis.store.models import SystemAgentOverride, SystemWorkflowOverride

    SystemAgentOverride.__table__.create(sync_conn, checkfirst=True)
    SystemWorkflowOverride.__table__.create(sync_conn, checkfirst=True)


def _ensure_task_execution_paths(sync_conn: object) -> None:
    """Add working-directory columns to tasks when missing."""

    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    execute = sync_conn.execute
    if "workspace_root" not in columns:
        execute(text("ALTER TABLE tasks ADD COLUMN workspace_root TEXT"))
    if "working_directory" not in columns:
        execute(text("ALTER TABLE tasks ADD COLUMN working_directory TEXT"))


def _ensure_agent_profile_columns(sync_conn: object) -> None:
    """Add per-agent runtime profile columns where execution is selected."""

    inspector = cast(Any, inspect(sync_conn))
    execute = sync_conn.execute  # type: ignore[attr-defined]

    def columns_for(table_name: str) -> set[str]:
        try:
            return {column["name"] for column in inspector.get_columns(table_name)}
        except Exception:
            return set()

    agent_columns = columns_for("agents")
    if agent_columns and "agent_profiles" not in agent_columns:
        execute(text("ALTER TABLE agents ADD COLUMN agent_profiles JSON"))
    if agent_columns and "default_agent_profile_id" not in agent_columns:
        execute(text("ALTER TABLE agents ADD COLUMN default_agent_profile_id VARCHAR"))

    for table_name in ("conversations", "sessions", "tasks", "step_runs", "schedules"):
        columns = columns_for(table_name)
        if columns and "agent_profile_id" not in columns:
            execute(text(f"ALTER TABLE {table_name} ADD COLUMN agent_profile_id VARCHAR"))

    managed_link_columns = columns_for("managed_conversation_links")
    if managed_link_columns and "target_agent_profile_id" not in managed_link_columns:
        execute(
            text(
                "ALTER TABLE managed_conversation_links ADD COLUMN target_agent_profile_id VARCHAR"
            )
        )

    channel_account_columns = columns_for("channel_accounts")
    if channel_account_columns and "default_agent_profile_id" not in channel_account_columns:
        execute(text("ALTER TABLE channel_accounts ADD COLUMN default_agent_profile_id VARCHAR"))


def _ensure_managed_conversation_lineage(sync_conn: object) -> None:
    """Add managed-conversation lifecycle and lineage columns when missing."""

    inspector = cast(Any, inspect(sync_conn))
    if not inspector.has_table("managed_conversation_links"):
        return
    columns = {column["name"] for column in inspector.get_columns("managed_conversation_links")}

    execute = sync_conn.execute  # type: ignore[attr-defined]
    if "parent_link_id" not in columns:
        execute(text("ALTER TABLE managed_conversation_links ADD COLUMN parent_link_id VARCHAR"))
    if "root_link_id" not in columns:
        execute(text("ALTER TABLE managed_conversation_links ADD COLUMN root_link_id VARCHAR"))
    if "depth" not in columns:
        execute(
            text(
                "ALTER TABLE managed_conversation_links ADD COLUMN depth INTEGER NOT NULL DEFAULT 1"
            )
        )
    if "last_result_turn_id" not in columns:
        execute(
            text("ALTER TABLE managed_conversation_links ADD COLUMN last_result_turn_id VARCHAR")
        )

    execute(
        text(
            "UPDATE managed_conversation_links "
            "SET root_link_id = link_id, depth = 1 "
            "WHERE root_link_id IS NULL"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_managed_conversation_links_parent_link "
            "ON managed_conversation_links (parent_link_id)"
        )
    )
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_managed_conversation_links_root_depth "
            "ON managed_conversation_links (root_link_id, depth)"
        )
    )


def _ensure_task_completion_delivery_columns(sync_conn: object) -> None:
    """Add task completion delivery policy and applied mode columns."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("tasks")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "completion_mode_family" not in columns:
        execute(
            text(
                "ALTER TABLE tasks ADD COLUMN completion_mode_family VARCHAR NOT NULL DEFAULT 'default'"
            )
        )
    if "allow_silent_completion" not in columns:
        execute(
            text(
                "ALTER TABLE tasks ADD COLUMN allow_silent_completion BOOLEAN NOT NULL DEFAULT false"
            )
        )
    if "applied_completion_mode" not in columns:
        execute(text("ALTER TABLE tasks ADD COLUMN applied_completion_mode VARCHAR"))
    if "applied_completion_reason" not in columns:
        execute(text("ALTER TABLE tasks ADD COLUMN applied_completion_reason TEXT"))


def _ensure_task_interaction_override_columns(sync_conn: object) -> None:
    """Add per-task interaction override storage when missing."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("tasks")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "interaction_mode_override" not in columns:
        execute(text("ALTER TABLE tasks ADD COLUMN interaction_mode_override VARCHAR"))


def _ensure_step_run_execution_paths(sync_conn: object) -> None:
    """Add working-directory columns to step_runs when missing."""

    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    columns = {column["name"] for column in inspector.get_columns("step_runs")}
    execute = sync_conn.execute
    if "workspace_root" not in columns:
        execute(text("ALTER TABLE step_runs ADD COLUMN workspace_root TEXT"))
    if "working_directory" not in columns:
        execute(text("ALTER TABLE step_runs ADD COLUMN working_directory TEXT"))


def _ensure_deliverables_table(sync_conn: object) -> None:
    """Create/upgrade the deliverables table when missing."""

    from cognis.store.models import DeliverableRow

    DeliverableRow.__table__.create(sync_conn, checkfirst=True)
    inspector = cast(Any, inspect(sync_conn))
    column_info = {column["name"]: column for column in inspector.get_columns("deliverables")}
    columns = set(column_info)
    execute = sync_conn.execute  # type: ignore[attr-defined]
    step_run_column = column_info.get("step_run_id")
    if step_run_column is not None and step_run_column.get("nullable") is False:
        _make_deliverables_step_run_nullable(sync_conn)
    content_column = column_info.get("content")
    if content_column is not None and content_column.get("nullable") is False:
        _make_legacy_deliverables_content_nullable(sync_conn)
    for name, ddl in {
        "conversation_id": "ALTER TABLE deliverables ADD COLUMN conversation_id VARCHAR",
        "session_id": "ALTER TABLE deliverables ADD COLUMN session_id VARCHAR",
        "turn_id": "ALTER TABLE deliverables ADD COLUMN turn_id VARCHAR",
        "storage_namespace": "ALTER TABLE deliverables ADD COLUMN storage_namespace VARCHAR NOT NULL DEFAULT 'deliverables'",
        "storage_object_id": "ALTER TABLE deliverables ADD COLUMN storage_object_id VARCHAR NOT NULL DEFAULT ''",
        "content_key": "ALTER TABLE deliverables ADD COLUMN content_key VARCHAR NOT NULL DEFAULT 'content.md'",
        "content_mime": "ALTER TABLE deliverables ADD COLUMN content_mime VARCHAR NOT NULL DEFAULT 'text/markdown'",
        "content_size": "ALTER TABLE deliverables ADD COLUMN content_size INTEGER NOT NULL DEFAULT 0",
        "content_hash": "ALTER TABLE deliverables ADD COLUMN content_hash VARCHAR NOT NULL DEFAULT ''",
        "rich_key": "ALTER TABLE deliverables ADD COLUMN rich_key VARCHAR",
        "rich_size": "ALTER TABLE deliverables ADD COLUMN rich_size INTEGER",
        "rich_hash": "ALTER TABLE deliverables ADD COLUMN rich_hash VARCHAR",
        "outputs_key": "ALTER TABLE deliverables ADD COLUMN outputs_key VARCHAR",
        "outputs_mime": "ALTER TABLE deliverables ADD COLUMN outputs_mime VARCHAR",
        "outputs_size": "ALTER TABLE deliverables ADD COLUMN outputs_size INTEGER",
        "outputs_hash": "ALTER TABLE deliverables ADD COLUMN outputs_hash VARCHAR",
        "validation_warnings": "ALTER TABLE deliverables ADD COLUMN validation_warnings JSON",
        "render_metadata": "ALTER TABLE deliverables ADD COLUMN render_metadata JSON",
        "export_metadata": "ALTER TABLE deliverables ADD COLUMN export_metadata JSON",
        "html_cache_key": "ALTER TABLE deliverables ADD COLUMN html_cache_key VARCHAR",
        "pdf_cache_key": "ALTER TABLE deliverables ADD COLUMN pdf_cache_key VARCHAR",
    }.items():
        if name not in columns:
            execute(text(ddl))
    execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_deliverables_conversation_scope "
            "ON deliverables (conversation_id, session_id, turn_id)"
        )
    )


def _make_deliverables_step_run_nullable(sync_conn: object) -> None:
    """Allow direct-chat deliverables by relaxing legacy step_run_id nullability."""

    _make_deliverables_column_nullable(
        sync_conn,
        column_name="step_run_id",
        existing_type=sa.String(),
    )


def _make_legacy_deliverables_content_nullable(sync_conn: object) -> None:
    """Stop obsolete inline payload constraints from rejecting metadata-only rows."""

    _make_deliverables_column_nullable(
        sync_conn,
        column_name="content",
        existing_type=sa.Text(),
    )


def _make_deliverables_column_nullable(
    sync_conn: object,
    *,
    column_name: str,
    existing_type: Any,
) -> None:
    """Relax a known legacy deliverables column across supported database dialects."""

    dialect_name = sync_conn.dialect.name  # type: ignore[attr-defined]
    execute = sync_conn.execute  # type: ignore[attr-defined]
    alter_statement = {
        "step_run_id": text("ALTER TABLE deliverables ALTER COLUMN step_run_id DROP NOT NULL"),
        "content": text("ALTER TABLE deliverables ALTER COLUMN content DROP NOT NULL"),
    }[column_name]
    if dialect_name == "postgresql":
        execute(alter_statement)
        return
    if dialect_name == "sqlite":
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext

        context = MigrationContext.configure(sync_conn)
        with Operations(context).batch_alter_table("deliverables") as batch:
            batch.alter_column(column_name, existing_type=existing_type, nullable=True)
        return
    execute(alter_statement)


def _ensure_step_run_deliverable_columns(sync_conn: object) -> None:
    """Add deliverable tracking columns to step_runs when missing."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("step_runs")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "deliverable_id" not in columns:
        execute(text("ALTER TABLE step_runs ADD COLUMN deliverable_id VARCHAR"))
    if "require_deliverable" not in columns:
        execute(text("ALTER TABLE step_runs ADD COLUMN require_deliverable BOOLEAN"))


def _ensure_step_run_runtime_info_column(sync_conn: object) -> None:
    """Add runtime diagnostics to step_runs when missing."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("step_runs")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "runtime_info" not in columns:
        execute(text("ALTER TABLE step_runs ADD COLUMN runtime_info JSON"))


def _ensure_workflow_lifecycle_columns(sync_conn: object) -> None:
    """Add lifecycle fields to workflows when missing."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("workflows")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "lifecycle" not in columns:
        execute(
            text("ALTER TABLE workflows ADD COLUMN lifecycle VARCHAR NOT NULL DEFAULT 'persistent'")
        )
    if "archived_at" not in columns:
        execute(text("ALTER TABLE workflows ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE"))


def _ensure_system_agent_override_columns(sync_conn: object) -> None:
    """Add missing runtime override columns to system agent overrides."""

    inspector = cast(Any, inspect(sync_conn))
    try:
        columns = {column["name"] for column in inspector.get_columns("system_agent_overrides")}
    except Exception:
        return
    execute = sync_conn.execute  # type: ignore[attr-defined]

    if "skills_override" not in columns:
        execute(text("ALTER TABLE system_agent_overrides ADD COLUMN skills_override JSON"))
    if "tools_override" not in columns:
        execute(text("ALTER TABLE system_agent_overrides ADD COLUMN tools_override JSON"))
    if "permissions_override" not in columns:
        execute(text("ALTER TABLE system_agent_overrides ADD COLUMN permissions_override JSON"))
    if "agent_profiles_override" not in columns:
        execute(text("ALTER TABLE system_agent_overrides ADD COLUMN agent_profiles_override JSON"))
    if "default_agent_profile_id_override" not in columns:
        execute(
            text(
                "ALTER TABLE system_agent_overrides "
                "ADD COLUMN default_agent_profile_id_override VARCHAR"
            )
        )


async def _ensure_system_user(session: AsyncSession) -> None:
    """Create the system user if it doesn't exist (for FK integrity)."""
    from cognis.store.models import User

    existing = await session.execute(select(User).where(User.email == SYSTEM_USER_EMAIL))
    if existing.scalar_one_or_none() is not None:
        return
    session.add(
        User(
            email=SYSTEM_USER_EMAIL,
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
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(
                Agent(
                    agent_id=agent_def.agent_id,
                    owner_email=SYSTEM_USER_EMAIL,
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
            continue
        row.owner_email = SYSTEM_USER_EMAIL
        row.name = agent_def.name
        row.description = agent_def.description
        row.system_prompt = agent_def.system_prompt
        row.tools = agent_def.tools if isinstance(agent_def.tools, dict) else None
        row.agent_type = agent_def.agent_type
        row.is_system = True
        row.hidden = agent_def.hidden
        row.status = "active"
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


async def seed_builtin_management_skills(session: AsyncSession) -> None:
    """Seed first-party Cognis management skills if they do not exist."""

    for skill in _BUILTIN_MANAGEMENT_SKILLS:
        defaults = get_system_skill_default(str(skill["skill_id"]))
        assert defaults is not None
        existing = await get_skill(session, str(skill["skill_id"]))
        if existing is not None:
            if existing.owner_email is not None:
                continue
            updates: dict[str, object] = {
                "is_system": True,
                "auto_load": bool(defaults.get("auto_load", False)),
                "name": defaults["name"],
                "description": defaults["description"],
                "instructions": defaults["instructions"],
                "tools": defaults["tools"],
                "prompt_templates": defaults["prompt_templates"],
                "tags": defaults["tags"],
                "linked_tool_ids": [
                    str(tool_id) for tool_id in (defaults.get("linked_tool_ids") or [])
                ],
            }
            for key, value in updates.items():
                setattr(existing, key, value)
            content_hash = compute_content_hash(
                existing.instructions,
                existing.tools,
                existing.linked_tool_ids,
                existing.prompt_templates,
                steps=defaults.get("steps") if isinstance(defaults.get("steps"), list) else None,
            )
            current_version = (
                await get_skill_version(session, existing.current_version_id)
                if existing.current_version_id is not None
                else None
            )
            if current_version is None or current_version.content_hash != content_hash:
                version_row = await create_skill_version(
                    session,
                    skill_id=existing.skill_id,
                    version_number=await get_next_version_number(session, existing.skill_id),
                    content_hash=content_hash,
                    instructions=existing.instructions,
                    tools=existing.tools,
                    linked_tool_ids=existing.linked_tool_ids,
                    prompt_templates=existing.prompt_templates,
                    secret_placeholders=None,
                    steps=defaults.get("steps")
                    if isinstance(defaults.get("steps"), list)
                    else None,
                    decomposition_source_hash=None,
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
            linked_tool_ids=[str(tool_id) for tool_id in (defaults.get("linked_tool_ids") or [])],
            prompt_templates=defaults.get("prompt_templates"),
            tags=list(defaults["tags"]),
            auto_load=bool(defaults.get("auto_load", False)),
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
                row.linked_tool_ids,
                row.prompt_templates,
                steps=defaults.get("steps") if isinstance(defaults.get("steps"), list) else None,
            ),
            instructions=row.instructions,
            tools=row.tools,
            linked_tool_ids=row.linked_tool_ids,
            prompt_templates=row.prompt_templates,
            secret_placeholders=None,
            steps=defaults.get("steps") if isinstance(defaults.get("steps"), list) else None,
            decomposition_source_hash=None,
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

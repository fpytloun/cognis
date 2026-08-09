from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from cognis.bootstrap import (
    DEFAULT_SETTINGS,
    _make_legacy_deliverables_content_nullable,
    bootstrap_runtime,
    run_schema_bootstrap,
    seed_builtin_management_skills,
)
from cognis.config import load_config
from cognis.core.system_skills import get_system_skill_default
from cognis.security import create_password_hasher
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import (
    create_skill,
    create_skill_version,
    create_user,
    get_next_version_number,
    get_setting,
    get_skill,
    get_skill_version,
    list_settings,
    set_current_version,
    upsert_setting,
)
from cognis.tools.skill_parser import compute_content_hash


@pytest.mark.asyncio
async def test_bootstrap_creates_keys_db_and_settings(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()

    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)

    assert config.jwt_private_key_path.exists()
    assert config.jwt_public_key_path.exists()
    assert config.secrets_key_path.exists()
    assert (tmp_path / "cognis.db").exists()

    async with session_factory() as session:
        settings = await list_settings(session)
        coding_skill = await get_skill(session, "cognis-coding")
        task_skill = await get_skill(session, "cognis-task-manager")
        workflow_skill = await get_skill(session, "cognis-workflow-manager")
        pulse_skill = await get_skill(session, "cognis-pulse-deliverable")
        assert pulse_skill is not None
        assert pulse_skill.current_version_id is not None
        pulse_version = await get_skill_version(session, pulse_skill.current_version_id)
        step_timeout = await get_setting(session, "session.step_timeout_seconds")
        evaluator_timeout = await get_setting(session, "evaluator.timeout_ms")
    assert len(settings) == len(DEFAULT_SETTINGS)
    assert step_timeout is not None
    assert step_timeout.value == 14400
    assert evaluator_timeout is not None
    assert evaluator_timeout.value == 180000
    assert coding_skill is not None
    assert task_skill is not None
    assert workflow_skill is not None
    assert pulse_version is not None
    assert coding_skill.auto_load is False
    assert coding_skill.is_system is True
    assert task_skill.auto_load is False
    assert workflow_skill.auto_load is False
    assert task_skill.is_system is True
    assert workflow_skill.is_system is True
    assert pulse_skill.is_system is True
    assert pulse_skill.name == "Cognis Pulse Deliverable"
    assert pulse_version.steps == []
    assert pulse_version.prompt_templates == {}
    assert "describe_tool" in pulse_skill.instructions
    assert task_skill.instructions.startswith("# Purpose")
    assert workflow_skill.instructions.startswith("# Purpose")
    assert task_skill.current_version_id is not None
    assert workflow_skill.current_version_id is not None
    assert coding_skill.current_version_id is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_validation_mode_seeds_without_running_schema_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "validated.db"
    alembic_config = Config("cognis/store/migrations/alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    alembic_config.config_file_name = None
    command.upgrade(alembic_config, "head")

    schema_bootstrap = AsyncMock()
    monkeypatch.setattr("cognis.bootstrap.run_schema_bootstrap", schema_bootstrap)
    config = replace(
        load_config(),
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{database_path}",
        schema_mode="validate",
        jwt_private_key_path=tmp_path / "keys" / "private.pem",
        jwt_public_key_path=tmp_path / "keys" / "public.pem",
        secrets_key_path=tmp_path / "secrets.key",
    )
    _, runtime_engine, factory, _ = await bootstrap_runtime(
        config,
        create_password_hasher(),
    )
    try:
        schema_bootstrap.assert_not_awaited()
        async with factory() as session:
            assert len(await list_settings(session)) == len(DEFAULT_SETTINGS)
    finally:
        await runtime_engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_adds_task_control_conversation_link_idempotently(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task_control_bootstrap.db"
    alembic_config = Config("cognis/store/migrations/alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    alembic_config.config_file_name = None
    command.upgrade(alembic_config, "105_managed_join_handoffs")

    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.begin() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"] for column in inspect(sync_conn).get_columns("tasks")
                }
            )
            indexes = await conn.run_sync(
                lambda sync_conn: {
                    index["name"]: index for index in inspect(sync_conn).get_indexes("tasks")
                }
            )
        assert "control_conversation_id" in columns
        assert "control_conversation_claimed_at" in columns
        assert indexes["ux_tasks_control_conversation_id"]["unique"] == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_adds_work_scope_revision_tables_idempotently(tmp_path: Path) -> None:
    database_path = tmp_path / "work_scope_bootstrap.db"
    alembic_config = Config("cognis/store/migrations/alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    alembic_config.config_file_name = None
    command.upgrade(alembic_config, "110_conversation_lineage")

    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.begin() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            stream_indexes = await conn.run_sync(
                lambda sync_conn: {
                    index["name"] for index in inspect(sync_conn).get_indexes("work_scope_streams")
                }
            )
            session_indexes = await conn.run_sync(
                lambda sync_conn: {
                    index["name"] for index in inspect(sync_conn).get_indexes("sessions")
                }
            )
        assert {"work_scope_states", "work_scope_streams"} <= tables
        assert {
            "ix_work_scope_streams_event_stream",
            "ix_work_scope_streams_session",
        } <= stream_indexes
        assert {
            "ix_sessions_owner_parent_session",
            "ix_sessions_owner_previous_session",
        } <= session_indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_backfills_existing_legacy_management_skill(tmp_path: Path) -> None:
    config = load_config()
    config = replace(config, database_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy_skills.db'}")
    engine = create_engine(config.database_url)
    session_factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)

    async with session_factory() as session:
        await create_skill(
            session,
            skill_id="cognis-task-manager",
            name="Legacy",
            description="old",
            instructions="legacy",
            tags=["legacy"],
            auto_load=False,
            source="db",
            owner_email=None,
        )
        await session.commit()

    password_hasher = create_password_hasher()
    _, _, session_factory_after, _ = await bootstrap_runtime(config, password_hasher)

    async with session_factory_after() as session:
        task_skill = await get_skill(session, "cognis-task-manager")

    assert task_skill is not None
    assert task_skill.is_system is True
    assert task_skill.name == "Cognis Task Manager"
    assert task_skill.instructions.startswith("# Purpose")
    assert task_skill.tags == ["cognis", "management", "tasks"]
    assert task_skill.current_version_id is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_system_skill_seed_publishes_changed_builtin_as_current_version(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'system-skill-upgrade.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    defaults = get_system_skill_default("cognis-coding")
    assert defaults is not None

    async with factory() as session:
        await seed_builtin_management_skills(session)
        skill = await get_skill(session, "cognis-coding")
        assert skill is not None
        old_version = await create_skill_version(
            session,
            skill_id=skill.skill_id,
            version_number=await get_next_version_number(session, skill.skill_id),
            content_hash=compute_content_hash(
                "outdated built-in instructions",
                skill.tools,
                skill.linked_tool_ids,
                skill.prompt_templates,
            ),
            instructions="outdated built-in instructions",
            tools=skill.tools,
            linked_tool_ids=skill.linked_tool_ids,
            prompt_templates=skill.prompt_templates,
        )
        skill.instructions = old_version.instructions
        await set_current_version(session, skill.skill_id, old_version.version_id)
        await session.commit()

    async with factory() as session:
        await seed_builtin_management_skills(session)
        await session.commit()
        upgraded = await get_skill(session, "cognis-coding")
        assert upgraded is not None
        assert upgraded.current_version_id != old_version.version_id
        current = await get_skill_version(session, upgraded.current_version_id)

    assert upgraded.instructions == defaults["instructions"]
    assert current is not None
    assert current.instructions == defaults["instructions"]

    async with factory() as session:
        await seed_builtin_management_skills(session)
        await session.commit()
        unchanged = await get_skill(session, "cognis-coding")
    assert unchanged is not None
    assert unchanged.current_version_id == upgraded.current_version_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_system_skill_seed_does_not_overwrite_user_owned_collision(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'user-skill-collision.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)

    async with factory() as session:
        await create_user(session, "owner@example.com", "Owner", "hash")
        await create_skill(
            session,
            skill_id="cognis-coding",
            name="User Coding",
            description="User-owned",
            instructions="keep user instructions",
            tags=["user"],
            owner_email="owner@example.com",
        )
        await seed_builtin_management_skills(session)
        await session.commit()
        skill = await get_skill(session, "cognis-coding")

    assert skill is not None
    assert skill.owner_email == "owner@example.com"
    assert skill.name == "User Coding"
    assert skill.instructions == "keep user instructions"
    assert skill.is_system is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_does_not_overwrite_existing_settings(
    monkeypatch: object, tmp_path: Path
) -> None:
    """Settings seeding is non-destructive — existing values are preserved."""
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()

    # First bootstrap — seeds defaults
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)

    # Manually change a setting (simulates UI change)
    async with session_factory() as session:
        await upsert_setting(
            session, key="executors.allow_in_process", value=False, category="executors"
        )
        await session.commit()

    # Second bootstrap — should NOT overwrite
    _, engine2, session_factory2, _ = await bootstrap_runtime(config, password_hasher)

    async with session_factory2() as session:
        setting = await get_setting(session, "executors.allow_in_process")
        assert setting is not None
        assert setting.value is False  # preserved, not reset to True

    await engine.dispose()
    await engine2.dispose()


@pytest.mark.asyncio
async def test_bootstrap_does_not_seed_legacy_context_cap(
    monkeypatch: object, tmp_path: Path
) -> None:
    """Bootstrap no longer seeds the removed session.max_context_tokens setting."""

    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()

    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)

    async with session_factory() as session:
        setting = await get_setting(session, "session.max_context_tokens")
        assert setting is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_fails_fast_with_require_external_crypto(
    monkeypatch: object, tmp_path: Path
) -> None:
    """When COGNIS_REQUIRE_EXTERNAL_CRYPTO=true, missing keys cause RuntimeError."""
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_REQUIRE_EXTERNAL_CRYPTO", "true")  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()

    with pytest.raises(RuntimeError, match="COGNIS_REQUIRE_EXTERNAL_CRYPTO"):
        await bootstrap_runtime(config, password_hasher)


@pytest.mark.asyncio
async def test_run_schema_bootstrap_upgrades_legacy_sessions_table(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE sessions ("
                "session_id TEXT PRIMARY KEY, "
                "conversation_id TEXT NOT NULL, "
                "parent_session_id TEXT, "
                "user_email TEXT NOT NULL, "
                "agent_id TEXT NOT NULL, "
                "delegation_mode TEXT, "
                "delegation_task TEXT, "
                "status TEXT NOT NULL DEFAULT 'active', "
                "intaris_session_id TEXT, "
                "mnemory_session_id TEXT, "
                "started_at TIMESTAMP NOT NULL, "
                "completed_at TIMESTAMP, "
                "result_summary TEXT"
                ")"
            )
        )

    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        session_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"] for column in inspect(sync_conn).get_columns("sessions")
            }
        )

    assert {"idle_since", "updated_at", "result_content"}.issubset(session_columns)

    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_upgrades_legacy_channel_delivery_outbox(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_channel_outbox.db'}")

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE channel_delivery_outbox ("
                "delivery_id VARCHAR PRIMARY KEY, "
                "user_email VARCHAR NOT NULL, "
                "conversation_id VARCHAR NOT NULL, "
                "session_id VARCHAR, "
                "source_type VARCHAR NOT NULL, "
                "source_id VARCHAR, "
                "channel_type VARCHAR NOT NULL, "
                "account_id VARCHAR NOT NULL, "
                "chat_id VARCHAR NOT NULL, "
                "thread_id VARCHAR, "
                "status VARCHAR NOT NULL DEFAULT 'pending', "
                "fallback_text TEXT, "
                "attempt_count INTEGER NOT NULL DEFAULT 0, "
                "next_attempt_at TIMESTAMP, "
                "lease_token VARCHAR, "
                "lease_expires_at TIMESTAMP, "
                "sent_at TIMESTAMP, "
                "last_error VARCHAR, "
                "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO channel_delivery_outbox ("
                "delivery_id, user_email, conversation_id, source_type, "
                "channel_type, account_id, chat_id"
                ") VALUES ("
                "'cdel_legacy', 'user@example.com', 'conv_legacy', 'task', "
                "'matrix', 'acct_legacy', 'room_legacy'"
                ")"
            )
        )

    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("channel_delivery_outbox")
            }
        )
        row = (
            await conn.execute(
                text(
                    "SELECT completed_chunk_count, projected_chunk_count, "
                    "projection_digest, inflight_chunk_index, inflight_idempotent, "
                    "attachments_json "
                    "FROM channel_delivery_outbox WHERE delivery_id = 'cdel_legacy'"
                )
            )
        ).one()

    assert {
        "completed_chunk_count",
        "projected_chunk_count",
        "projection_digest",
        "inflight_chunk_index",
        "inflight_idempotent",
        "attachments_json",
    }.issubset(columns)
    assert row == (0, None, None, None, None, None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_keeps_legacy_sqlite_byte_counters_int64_safe(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_local_models.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE local_model_operations ("
                "operation_id VARCHAR PRIMARY KEY, progress_bytes INTEGER NOT NULL DEFAULT 0)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE local_model_target_statuses ("
                "target_id VARCHAR PRIMARY KEY, observed_size_bytes INTEGER)"
            )
        )

    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO local_model_operations (operation_id, progress_bytes) "
                "VALUES ('operation-big', 5629109111)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO local_model_target_statuses (target_id, observed_size_bytes) "
                "VALUES ('target-big', 5629109111)"
            )
        )
        assert (
            await conn.execute(
                text(
                    "SELECT progress_bytes FROM local_model_operations "
                    "WHERE operation_id = 'operation-big'"
                )
            )
        ).scalar_one() == 5_629_109_111
        assert (
            await conn.execute(
                text(
                    "SELECT observed_size_bytes FROM local_model_target_statuses "
                    "WHERE target_id = 'target-big'"
                )
            )
        ).scalar_one() == 5_629_109_111
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_upgrades_managed_conversation_lineage(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_managed.db'}")

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE managed_conversation_links ("
                "link_id TEXT PRIMARY KEY, "
                "target_agent_profile_id TEXT"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO managed_conversation_links "
                "(link_id, target_agent_profile_id) VALUES ('link_1', NULL)"
            )
        )

    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("managed_conversation_links")
            }
        )
        indexes = await conn.run_sync(
            lambda sync_conn: {
                index["name"]
                for index in inspect(sync_conn).get_indexes("managed_conversation_links")
            }
        )
        row = (
            await conn.execute(
                text(
                    "SELECT parent_link_id, root_link_id, depth "
                    "FROM managed_conversation_links WHERE link_id = 'link_1'"
                )
            )
        ).one()

    assert {
        "parent_link_id",
        "root_link_id",
        "depth",
        "last_result_turn_id",
        "handoff_state",
        "handoff_target_turn_id",
        "handoff_controller_session_id",
        "handoff_controller_turn_id",
        "handoff_tool_call_id",
        "kind",
        "completion_policy",
        "owner_epoch",
        "creation_policy_snapshot",
    }.issubset(columns)
    assert {
        "ix_managed_conversation_links_parent_link",
        "ix_managed_conversation_links_root_depth",
        "ix_managed_conversation_links_handoff_owner",
    }.issubset(indexes)
    assert row == (None, "link_1", 1)

    async with engine.begin() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
    assert {
        "managed_conversation_signals",
        "managed_channel_bindings",
        "channel_inbound_ledger",
        "channel_context_consumptions",
        "channel_observed_targets",
    }.issubset(table_names)
    async with engine.begin() as conn:
        signal_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("managed_conversation_signals")
            }
        )
    assert {
        "resume_request_id",
        "resume_turn_id",
        "resume_prepared_at",
        "resume_admitted_at",
        "resume_terminal_status",
    }.issubset(signal_columns)

    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_upgrades_group_context_columns_twice(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_group_context.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE channel_inbound_ledger ("
                "inbound_id VARCHAR PRIMARY KEY, "
                "account_id VARCHAR NOT NULL, "
                "chat_id VARCHAR NOT NULL, "
                "thread_key VARCHAR NOT NULL, "
                "message_id VARCHAR NOT NULL, "
                "binding_id VARCHAR, "
                "occurred_at TIMESTAMP NOT NULL, "
                "created_at TIMESTAMP NOT NULL"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO channel_inbound_ledger "
                "(inbound_id, account_id, chat_id, thread_key, message_id, "
                "occurred_at, created_at) "
                "VALUES ('inbound-1', 'account-1', 'chat-1', '', 'message-1', "
                "'2026-08-02 10:00:00', '2026-08-02 10:00:01')"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE channel_context_consumptions ("
                "consumption_id VARCHAR PRIMARY KEY, "
                "state VARCHAR NOT NULL, "
                "reserved_until TIMESTAMP NOT NULL"
                ")"
            )
        )

    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        ledger_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("channel_inbound_ledger")
            }
        )
        ledger_indexes = await conn.run_sync(
            lambda sync_conn: {
                index["name"] for index in inspect(sync_conn).get_indexes("channel_inbound_ledger")
            }
        )
        consumption_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("channel_context_consumptions")
            }
        )
        consumption_indexes = await conn.run_sync(
            lambda sync_conn: {
                index["name"]
                for index in inspect(sync_conn).get_indexes("channel_context_consumptions")
            }
        )
        backfilled = (
            await conn.execute(
                text(
                    "SELECT observed_at, ordering_key, ordering_source "
                    "FROM channel_inbound_ledger WHERE inbound_id = 'inbound-1'"
                )
            )
        ).one()

    assert {"observed_at", "ordering_key", "ordering_source", "retain_until"}.issubset(
        ledger_columns
    )
    assert {
        "ix_channel_inbound_ledger_context",
        "ix_channel_inbound_ledger_retention",
    }.issubset(ledger_indexes)
    assert {"usage", "trigger_inbound_id", "admitted_turn_id"}.issubset(consumption_columns)
    assert "ix_channel_context_consumptions_admission" in consumption_indexes
    assert backfilled.observed_at is not None
    assert backfilled.ordering_key.endswith(":inbound-1")
    assert backfilled.ordering_source == "observed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_upgrades_legacy_follow_up_leases(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_followups.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE follow_up_dedupe ("
                "dedupe_key VARCHAR PRIMARY KEY, "
                "conversation_id VARCHAR NOT NULL, "
                "follow_up_id VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL, "
                "expires_at TIMESTAMP NOT NULL, "
                "created_at TIMESTAMP, "
                "updated_at TIMESTAMP)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE follow_up_intents ("
                "intent_id VARCHAR PRIMARY KEY, "
                "conversation_id VARCHAR NOT NULL, "
                "follow_up_id VARCHAR NOT NULL, "
                "event_payload JSON NOT NULL, "
                "status VARCHAR NOT NULL, "
                "attempt_count INTEGER NOT NULL, "
                "last_error VARCHAR, "
                "created_at TIMESTAMP, "
                "updated_at TIMESTAMP)"
            )
        )

    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        schemas = await conn.run_sync(
            lambda sync_conn: {
                table_name: {
                    "columns": {
                        column["name"] for column in inspect(sync_conn).get_columns(table_name)
                    },
                    "indexes": {
                        index["name"] for index in inspect(sync_conn).get_indexes(table_name)
                    },
                }
                for table_name in ("follow_up_intents", "follow_up_dedupe")
            }
        )
    for table_name, index_name in (
        ("follow_up_intents", "ix_follow_up_intents_lease"),
        ("follow_up_dedupe", "ix_follow_up_dedupe_lease"),
    ):
        assert {"lease_owner", "lease_expires_at"} <= schemas[table_name]["columns"]
        assert index_name in schemas[table_name]["indexes"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_upgrades_legacy_llm_provider_owner_schema(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_llm.db'}")

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE llm_providers ("
                "provider_id TEXT PRIMARY KEY, "
                "display_name TEXT NOT NULL, "
                "location TEXT NOT NULL, "
                "backend TEXT NOT NULL, "
                "config TEXT NOT NULL, "
                "is_default BOOLEAN NOT NULL DEFAULT 0, "
                "status TEXT NOT NULL, "
                "created_at TIMESTAMP NOT NULL, "
                "updated_at TIMESTAMP NOT NULL"
                ")"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE model_routing ("
                "task_type TEXT PRIMARY KEY, "
                "provider_id TEXT, "
                "model TEXT NOT NULL, "
                "config TEXT, "
                "updated_at TIMESTAMP NOT NULL"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO llm_providers ("
                "provider_id, display_name, location, backend, config, status, created_at, updated_at"
                ") VALUES ('openai', 'OpenAI', 'controller', 'litellm', '{}', 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO model_routing (task_type, provider_id, model, config, updated_at) "
                "VALUES ('default', 'openai', 'gpt-4o-mini', NULL, CURRENT_TIMESTAMP)"
            )
        )

    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        provider_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"] for column in inspect(sync_conn).get_columns("llm_providers")
            }
        )
        routing_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"] for column in inspect(sync_conn).get_columns("model_routing")
            }
        )
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        owner_email = (
            await conn.execute(
                text("SELECT owner_email FROM llm_providers WHERE provider_id = 'openai'")
            )
        ).scalar_one()
        route_id = (
            await conn.execute(
                text("SELECT route_id FROM model_routing WHERE task_type = 'default'")
            )
        ).scalar_one()

    assert "owner_email" in provider_columns
    assert {"route_id", "owner_email"}.issubset(routing_columns)
    assert "llm_provider_auth_sessions" in table_names
    assert owner_email == "system@cognis.local"
    assert route_id == "route_default"

    await engine.dispose()


def test_legacy_deliverables_content_repair_emits_postgresql_ddl() -> None:
    statements: list[str] = []

    class PostgresConnection:
        class dialect:
            name = "postgresql"

        @staticmethod
        def execute(statement: object) -> None:
            statements.append(str(statement))

    _make_legacy_deliverables_content_nullable(PostgresConnection())

    assert statements == ["ALTER TABLE deliverables ALTER COLUMN content DROP NOT NULL"]


@pytest.mark.asyncio
async def test_run_schema_bootstrap_repairs_legacy_deliverable_constraints(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_deliverables.db'}")

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE deliverables ("
                "deliverable_id TEXT PRIMARY KEY, "
                "step_run_id TEXT NOT NULL, "
                "version INTEGER NOT NULL DEFAULT 1, "
                "content TEXT NOT NULL, "
                "format TEXT NOT NULL, "
                "status TEXT NOT NULL"
                ")"
            )
        )

    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        deliverable_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]: column for column in inspect(sync_conn).get_columns("deliverables")
            }
        )
        await conn.execute(
            text(
                "INSERT INTO deliverables ("
                "deliverable_id, step_run_id, conversation_id, session_id, turn_id, "
                "version, format, status"
                ") VALUES ("
                "'dlv_direct', NULL, 'conv', 'sess', 'turn', "
                "1, 'rich', 'buffered'"
                ")"
            )
        )

    assert deliverable_columns["step_run_id"]["nullable"] is True
    assert deliverable_columns["content"]["nullable"] is True
    assert {
        "conversation_id",
        "session_id",
        "turn_id",
        "storage_namespace",
        "storage_object_id",
        "content_key",
        "content_mime",
        "content_size",
        "content_hash",
        "validation_warnings",
        "render_metadata",
        "export_metadata",
    }.issubset(deliverable_columns)

    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_creates_system_override_tables(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'overrides.db'}")

    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:

        def _table_names(sync_conn: object) -> list[str]:
            return inspect(sync_conn).get_table_names()

        table_names = await conn.run_sync(_table_names)

    assert "system_agent_overrides" in table_names
    assert "system_workflow_overrides" in table_names

    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_upgrades_system_agent_profile_overrides_twice(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_overrides.db'}")

    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE system_agent_overrides (
                    override_id VARCHAR PRIMARY KEY,
                    owner_email VARCHAR NOT NULL,
                    agent_id VARCHAR NOT NULL,
                    disabled BOOLEAN NOT NULL DEFAULT 0,
                    llm_config_override JSON,
                    execution_override JSON,
                    skills_override JSON,
                    tools_override JSON,
                    permissions_override JSON,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
        )

    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)

    async with engine.begin() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("system_agent_overrides")
            }
        )

    assert "agent_profiles_override" in columns
    assert "default_agent_profile_id_override" in columns
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_upgrades_legacy_chart_payload_twice(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_charts.db'}")
    legacy_payload = {
        "blocks": [
            {
                "type": "chart",
                "title": "Requests",
                "chart_type": "comparison",
                "rows": [
                    {"day": "Mon", "series": "API", "value": 4},
                    {"day": "Tue", "series": "API", "value": 7},
                ],
                "series_key": "series",
                "x_key": "day",
                "y_key": "value",
                "source_ids": ["source-1"],
            }
        ]
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE deliverables (deliverable_id VARCHAR PRIMARY KEY, rich_payload JSON)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO deliverables (deliverable_id, rich_payload) "
                "VALUES (:deliverable_id, :rich_payload)"
            ).bindparams(sa.bindparam("rich_payload", type_=sa.JSON())),
            {"deliverable_id": "dlv_legacy_chart", "rich_payload": legacy_payload},
        )

    await run_schema_bootstrap(engine)
    async with engine.connect() as conn:
        first = (
            await conn.execute(
                text(
                    "SELECT rich_payload FROM deliverables "
                    "WHERE deliverable_id = 'dlv_legacy_chart'"
                ).columns(rich_payload=sa.JSON())
            )
        ).scalar_one()

    await run_schema_bootstrap(engine)
    async with engine.connect() as conn:
        second = (
            await conn.execute(
                text(
                    "SELECT rich_payload FROM deliverables "
                    "WHERE deliverable_id = 'dlv_legacy_chart'"
                ).columns(rich_payload=sa.JSON())
            )
        ).scalar_one()

    assert first == second
    chart = first["blocks"][0]
    assert chart == {
        "type": "chart",
        "title": "Requests",
        "source_ids": ["source-1"],
        "spec_version": "cognis.chart.v1",
        "chart_type": "grouped_bar",
        "series": [
            {
                "id": "api",
                "label": "API",
                "points": [{"x": "Mon", "y": 4.0}, {"x": "Tue", "y": 7.0}],
            }
        ],
        "x_axis": {"type": "category"},
        "y_axis": {"type": "linear"},
        "stack": False,
    }
    await engine.dispose()


async def _artifact_source_identity_state(engine: object) -> tuple[dict[str, tuple], set[str]]:
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        rows = (
            await conn.execute(
                text(
                    "SELECT artifact_id, source_tool_call_id, source_anchor "
                    "FROM artifacts ORDER BY artifact_id"
                )
            )
        ).all()
        indexes = await conn.run_sync(
            lambda sync_conn: {
                index["name"] for index in inspect(sync_conn).get_indexes("artifacts")
            }
        )
    return {str(row[0]): (row[1], row[2]) for row in rows}, indexes


@pytest.mark.asyncio
async def test_run_schema_bootstrap_applies_artifact_source_identity_094_twice(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'artifact_093.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE artifacts (
                    artifact_id VARCHAR PRIMARY KEY,
                    owner_email VARCHAR NOT NULL,
                    filename VARCHAR,
                    purpose VARCHAR NOT NULL,
                    conversation_id VARCHAR,
                    session_id VARCHAR,
                    updated_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO artifacts (
                    artifact_id, owner_email, filename, purpose,
                    conversation_id, session_id
                ) VALUES
                    ('legacy_artifact', 'user@example.com', 'image.jpg',
                     'tool_artifact', 'call-artifact', 'media:1'),
                    ('legacy_output', 'user@example.com', 'call-output.txt',
                     'tool_output', 'conversation-1', 'session-1'),
                    ('empty_output', 'user@example.com', '.txt',
                     'tool_output', 'conversation-1', 'session-1'),
                    ('wrong_extension', 'user@example.com', 'call-output.json',
                     'tool_output', 'conversation-1', 'session-1')
                """
            )
        )

    await run_schema_bootstrap(engine)
    first_state = await _artifact_source_identity_state(engine)
    await run_schema_bootstrap(engine)
    second_state = await _artifact_source_identity_state(engine)

    assert first_state == second_state
    rows, indexes = first_state
    assert rows["legacy_artifact"] == ("call-artifact", "media:1")
    assert rows["legacy_output"] == ("call-output", None)
    assert rows["empty_output"] == (None, None)
    assert rows["wrong_extension"] == (None, None)
    assert "ix_artifacts_tool_source" in indexes
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_completes_partial_artifact_094_without_overwrite(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'artifact_partial.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE artifacts (
                    artifact_id VARCHAR PRIMARY KEY,
                    owner_email VARCHAR NOT NULL,
                    filename VARCHAR,
                    purpose VARCHAR NOT NULL,
                    conversation_id VARCHAR,
                    session_id VARCHAR,
                    source_tool_call_id VARCHAR,
                    source_anchor VARCHAR,
                    updated_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO artifacts (
                    artifact_id, owner_email, filename, purpose,
                    conversation_id, session_id,
                    source_tool_call_id, source_anchor
                ) VALUES
                    ('preserved', 'user@example.com', 'image.jpg',
                     'tool_artifact', 'legacy-call', 'legacy-anchor',
                     'current-call', 'current-anchor'),
                    ('missing_call', 'user@example.com', 'image.jpg',
                     'tool_artifact', 'legacy-call-2', 'legacy-anchor-2',
                     NULL, 'current-anchor-2'),
                    ('missing_anchor', 'user@example.com', 'image.jpg',
                     'tool_artifact', 'legacy-call-3', 'legacy-anchor-3',
                     'current-call-3', NULL),
                    ('preserved_output', 'user@example.com', 'legacy-name.txt',
                     'tool_output', 'conversation-1', 'session-1',
                     'current-output-call', NULL)
                """
            )
        )

    await run_schema_bootstrap(engine)
    rows, indexes = await _artifact_source_identity_state(engine)

    assert rows["preserved"] == ("current-call", "current-anchor")
    assert rows["missing_call"] == ("legacy-call-2", "current-anchor-2")
    assert rows["missing_anchor"] == ("current-call-3", "legacy-anchor-3")
    assert rows["preserved_output"] == ("current-output-call", None)
    assert "ix_artifacts_tool_source" in indexes
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_schema_bootstrap_fresh_artifacts_has_094_schema(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'artifact_fresh.db'}")

    await run_schema_bootstrap(engine)
    async with engine.connect() as conn:
        columns, indexes = await conn.run_sync(
            lambda sync_conn: (
                {column["name"] for column in inspect(sync_conn).get_columns("artifacts")},
                {index["name"] for index in inspect(sync_conn).get_indexes("artifacts")},
            )
        )

    assert {"source_tool_call_id", "source_anchor"}.issubset(columns)
    assert "ix_artifacts_tool_source" in indexes
    await engine.dispose()

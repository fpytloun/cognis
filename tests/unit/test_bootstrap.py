from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from cognis.bootstrap import DEFAULT_SETTINGS, bootstrap_runtime, run_schema_bootstrap
from cognis.config import load_config
from cognis.security import create_password_hasher
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import create_skill, get_setting, get_skill, list_settings, upsert_setting


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
        task_skill = await get_skill(session, "cognis-task-manager")
        workflow_skill = await get_skill(session, "cognis-workflow-manager")
    assert len(settings) == len(DEFAULT_SETTINGS)
    assert task_skill is not None
    assert workflow_skill is not None
    assert task_skill.auto_load is False
    assert workflow_skill.auto_load is False
    assert task_skill.is_system is True
    assert workflow_skill.is_system is True
    assert task_skill.instructions.startswith("# Purpose")
    assert workflow_skill.instructions.startswith("# Purpose")
    assert task_skill.current_version_id is not None
    assert workflow_skill.current_version_id is not None

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
async def test_bootstrap_upgrades_legacy_default_context_cap(
    monkeypatch: object, tmp_path: Path
) -> None:
    """Legacy default 128k context cap is upgraded to the new 250k default."""
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()

    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)

    async with session_factory() as session:
        await upsert_setting(
            session,
            key="session.max_context_tokens",
            value=128000,
            category="session",
        )
        await session.commit()

    _, engine2, session_factory2, _ = await bootstrap_runtime(config, password_hasher)

    async with session_factory2() as session:
        setting = await get_setting(session, "session.max_context_tokens")
        assert setting is not None
        assert setting.value == 250000

    await engine.dispose()
    await engine2.dispose()


@pytest.mark.asyncio
async def test_bootstrap_preserves_custom_context_cap(monkeypatch: object, tmp_path: Path) -> None:
    """Non-default custom context caps are preserved across bootstrap."""
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()

    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)

    async with session_factory() as session:
        await upsert_setting(
            session,
            key="session.max_context_tokens",
            value=180000,
            category="session",
        )
        await session.commit()

    _, engine2, session_factory2, _ = await bootstrap_runtime(config, password_hasher)

    async with session_factory2() as session:
        setting = await get_setting(session, "session.max_context_tokens")
        assert setting is not None
        assert setting.value == 180000

    await engine.dispose()
    await engine2.dispose()


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

    assert {"idle_since", "updated_at"}.issubset(session_columns)

    await engine.dispose()

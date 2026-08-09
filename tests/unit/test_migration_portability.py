from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.store.database import create_session_factory
from cognis.store.migrations.versioning import ALEMBIC_VERSION_NUM_LENGTH
from cognis.store.queries import (
    create_agent,
    create_user,
    list_agents,
    list_secondary_bindings,
    set_secondary_bindings,
)
from tests.schema_parity import assert_schema_matches_metadata

MIGRATIONS = Path("cognis/store/migrations/versions")
MODELS = Path("cognis/store/models.py")
POSTGRES_INTEGRATION_TESTS = Path("tests/integration")
NUMERIC_BOOLEAN_LITERALS = {"0", "1"}


def _is_sa_attribute(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (node.value.id == "sa" and node.attr == name)
    )


def _is_boolean_type(node: ast.AST) -> bool:
    return (
        _is_sa_attribute(node, "Boolean")
        or isinstance(node, ast.Name)
        and node.id == "Boolean"
        or isinstance(node, ast.Call)
        and (
            _is_sa_attribute(node.func, "Boolean")
            or isinstance(node.func, ast.Name)
            and node.func.id == "Boolean"
        )
    )


def _numeric_boolean_default(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return str(node.value) in NUMERIC_BOOLEAN_LITERALS
    return (
        isinstance(node, ast.Call)
        and _is_sa_attribute(node.func, "text")
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and str(node.args[0].value) in NUMERIC_BOOLEAN_LITERALS
    )


def test_boolean_server_defaults_are_dialect_portable() -> None:
    invalid: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and _is_sa_attribute(node.func, "Column")
                and len(node.args) >= 2
                and _is_boolean_type(node.args[1])
            ):
                continue
            server_default = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "server_default"),
                None,
            )
            if server_default is not None and _numeric_boolean_default(server_default):
                invalid.append(f"{path}:{node.lineno}")

    assert invalid == []


def test_orm_boolean_server_defaults_are_dialect_portable() -> None:
    tree = ast.parse(MODELS.read_text(encoding="utf-8"), filename=str(MODELS))
    invalid = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            _is_sa_attribute(node.func, "mapped_column")
            or isinstance(node.func, ast.Name)
            and node.func.id == "mapped_column"
        )
        and any(_is_boolean_type(argument) for argument in node.args)
        and any(
            keyword.arg == "server_default" and _numeric_boolean_default(keyword.value)
            for keyword in node.keywords
        )
    ]

    assert invalid == []


def test_alembic_version_capacity_exceeds_longest_revision() -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    revisions = ScriptDirectory.from_config(config).walk_revisions("base", "heads")
    longest_revision = max(len(revision.revision) for revision in revisions)

    assert longest_revision < ALEMBIC_VERSION_NUM_LENGTH


def test_task_delivery_migration_expands_baseline_version_capacity() -> None:
    migration = Path(
        "cognis/store/migrations/versions/126_task_delivery_preferred_channel_default.py"
    ).read_text()

    capacity_call = "ensure_alembic_version_capacity(op.get_bind())"
    task_migration = 'with op.batch_alter_table("tasks") as batch_op:'

    assert capacity_call in migration
    assert migration.index(capacity_call) < migration.index(task_migration)


def test_task_delivery_default_migrates_to_preferred_channel(tmp_path: Path) -> None:
    database_path = tmp_path / "task-delivery-default.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    command.upgrade(config, "125_task_source_session")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            delivery_column = next(
                column
                for column in sa.inspect(connection).get_columns("tasks")
                if column["name"] == "delivery_mode"
            )
            assert delivery_column["default"] == "'same_conversation'"

        command.upgrade(config, "126_task_delivery_preferred_channel_default")

        with engine.connect() as connection:
            delivery_column = next(
                column
                for column in sa.inspect(connection).get_columns("tasks")
                if column["name"] == "delivery_mode"
            )
            assert delivery_column["default"] == "'preferred_channel'"
    finally:
        engine.dispose()


def test_postgres_integration_schemas_never_fall_back_to_public() -> None:
    offenders = []
    for path in sorted(POSTGRES_INTEGRATION_TESTS.glob("*postgres.py")):
        source = path.read_text(encoding="utf-8")
        if "CreateSchema(" not in source:
            continue
        if "search_path" in source and ",public" in source:
            offenders.append(str(path))
        assert "DropSchema(" in source, path
        assert "Base.metadata.create_all" in source or "command.upgrade" in source, path

    assert offenders == []


@pytest.mark.asyncio
async def test_fresh_sqlite_upgrade_to_head_remains_supported(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    command.upgrade(config, "head")

    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        with sync_engine.connect() as connection:
            assert_schema_matches_metadata(connection)
    finally:
        sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await create_user(session, "owner@example.com", "Owner", "hash")
            await create_agent(
                session,
                agent_id="primary",
                owner_email="owner@example.com",
                name="Primary",
            )
            await create_agent(
                session,
                agent_id="secondary",
                owner_email="owner@example.com",
                name="Secondary",
                agent_type="secondary",
            )
            await set_secondary_bindings(session, "primary", ["secondary"])
            await session.commit()

            agents = await list_agents(session, "owner@example.com")
            assert {agent.agent_id for agent in agents} == {"primary", "secondary"}
            assert all(agent.is_system is False and agent.hidden is False for agent in agents)
            assert await list_secondary_bindings(session, "primary") == ["secondary"]
    finally:
        await engine.dispose()


def test_revision_103_backfills_existing_rows_and_adds_agent_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "upgrade.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    command.upgrade(config, "102_executor_pin_ha_stage3")

    engine = create_engine(f"sqlite:///{database_path}")
    created_at = datetime(2026, 7, 26, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users "
                "(email, name, password_hash, role, created_at, updated_at) "
                "VALUES (:email, :name, :password_hash, :role, :created_at, NULL)"
            ),
            {
                "email": "legacy@example.com",
                "name": "Legacy",
                "password_hash": "hash",
                "role": "user",
                "created_at": created_at,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO agents "
                "(agent_id, owner_email, name, status, created_at, updated_at) "
                "VALUES ('legacy-agent', 'legacy@example.com', 'Legacy', "
                "'active', :created_at, :created_at)"
            ),
            {"created_at": created_at},
        )
        connection.execute(
            sa.text(
                "INSERT INTO artifacts "
                "(artifact_id, namespace, object_id, filename, purpose, kind, "
                "mime_type, size_bytes, status, created_at, updated_at) "
                "VALUES ('artifact-1', 'owner', 'object-1', 'file.txt', "
                "'chat_input', 'file', 'text/plain', 1, 'temporary', "
                ":created_at, NULL)"
            ),
            {"created_at": created_at},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            agent = connection.execute(
                sa.text(
                    "SELECT agent_type, is_system, hidden "
                    "FROM agents WHERE agent_id = 'legacy-agent'"
                )
            ).one()
            assert tuple(agent) == ("primary", False, False)
            assert connection.scalar(sa.text("SELECT COUNT(*) FROM agent_secondary_bindings")) == 0
            assert connection.scalar(
                sa.text(
                    "SELECT updated_at IS NOT NULL FROM users WHERE email = 'legacy@example.com'"
                )
            )
            assert connection.scalar(
                sa.text(
                    "SELECT updated_at IS NOT NULL FROM artifacts WHERE artifact_id = 'artifact-1'"
                )
            )
            assert_schema_matches_metadata(connection)
    finally:
        engine.dispose()

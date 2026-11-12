from __future__ import annotations

import ast
import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_session_factory
from cognis.store.migrations.versioning import ALEMBIC_VERSION_NUM_LENGTH
from cognis.store.queries import (
    create_agent,
    create_user,
    list_agents,
    list_secondary_bindings,
    set_secondary_bindings,
)
from tests.bootstrap_preservation import read_bootstrap_data, seed_bootstrap_data
from tests.schema_parity import assert_schema_matches_metadata

MIGRATIONS = Path("cognis/store/migrations/versions")
MODELS = Path("cognis/store/models.py")
POSTGRES_INTEGRATION_TESTS = Path("tests/integration")
NUMERIC_BOOLEAN_LITERALS = {"0", "1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_tables", [False, True])
async def test_bootstrap_at_129_then_upgrade_preserves_release_schema(
    tmp_path: Path, partial_tables: bool
) -> None:
    database_path = tmp_path / "bootstrapped-upgrade.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    await asyncio.to_thread(command.upgrade, config, "129_work_record_file_metadata")
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        await run_schema_bootstrap(engine)
        async with engine.begin() as connection:
            preserved = await connection.run_sync(seed_bootstrap_data)
        if partial_tables:
            async with engine.begin() as connection:
                await connection.execute(sa.text("DROP TABLE inference_bindings"))
                await connection.execute(sa.text("DROP TABLE agent_hook_turns"))
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as connection:
            await connection.run_sync(assert_schema_matches_metadata)
            assert await connection.run_sync(read_bootstrap_data) == preserved
    finally:
        await engine.dispose()


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


def test_durable_work_projection_repairs_partial_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "durable-work-partial.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    command.upgrade(config, "122_durable_work_projection")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("DROP TABLE work_session_projections"))
            connection.execute(sa.text("DROP INDEX ix_work_records_owner_version_newest"))
            connection.execute(sa.text("DROP INDEX ix_work_records_pairing"))
        command.stamp(config, "121_channel_recipient_intents")
        command.upgrade(config, "122_durable_work_projection")

        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert inspector.has_table("work_records")
            assert inspector.has_table("work_session_projections")
            assert {
                str(index["name"])
                for index in inspector.get_indexes("work_records")
                if index.get("name")
            } >= {
                "ix_work_records_owner_session_version_order",
                "ix_work_records_owner_version_newest",
                "ix_work_records_pairing",
            }
            assert {
                str(index["name"])
                for index in inspector.get_indexes("work_session_projections")
                if index.get("name")
            } >= {
                "ix_work_session_projections_queue",
                "ix_work_session_projections_owner_state",
                "ix_work_session_projections_lease",
            }
    finally:
        engine.dispose()


SHARED_USAGE_SETUP = {
    "tests.usage_query_schema_fixtures": {"exercise_query_upgrade", "explain_query_indexes"},
    "tests.unit.test_usage_schema": {"exercise_usage_schema"},
}


def _has_shared_usage_setup(source: str, root: Path = Path(".")) -> bool:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module not in SHARED_USAGE_SETUP:
            continue
        helper_path = root.joinpath(*node.module.split(".")).with_suffix(".py")
        if not helper_path.is_file():
            continue
        helper_tree = ast.parse(helper_path.read_text(encoding="utf-8"))
        canonical_names = {
            alias.asname or alias.name
            for item in helper_tree.body
            if isinstance(item, ast.ImportFrom) and item.module == "cognis.store.usage.schema"
            for alias in item.names
            if alias.name == "ensure_usage_schema"
        }
        for alias in node.names:
            if alias.name not in SHARED_USAGE_SETUP[node.module]:
                continue
            local_name = alias.asname or alias.name
            referenced = any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "run_sync"
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == local_name
                for call in ast.walk(tree)
            )
            initialized = any(
                isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                and function.name == alias.name
                and function.args.args
                and any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in canonical_names
                    and call.args
                    and isinstance(call.args[0], ast.Name)
                    and call.args[0].id == function.args.args[0].arg
                    for call in ast.walk(function)
                )
                for function in helper_tree.body
            )
            if referenced and initialized:
                return True
    return False


def _assert_postgres_schema_isolation(source: str, path: Path, root: Path = Path(".")) -> None:
    if "CreateSchema(" not in source:
        return
    assert not ("search_path" in source and re.search(r",\s*public\b", source, re.I)), path
    assert "DropSchema(" in source, path
    assert (
        "Base.metadata.create_all" in source
        or "command.upgrade" in source
        or _has_shared_usage_setup(source, root)
    ), path


def test_postgres_integration_schemas_never_fall_back_to_public() -> None:
    for path in sorted(POSTGRES_INTEGRATION_TESTS.glob("*postgres.py")):
        _assert_postgres_schema_isolation(path.read_text(encoding="utf-8"), path)


@pytest.mark.parametrize("setup", ["Base.metadata.create_all", "command.upgrade"])
def test_postgres_schema_guard_accepts_direct_setup(setup: str) -> None:
    _assert_postgres_schema_isolation(f"CreateSchema()\nDropSchema()\n{setup}", Path("direct"))


@pytest.mark.parametrize(
    ("module", "helper"),
    [
        ("tests.usage_query_schema_fixtures", "exercise_query_upgrade"),
        ("tests.usage_query_schema_fixtures", "explain_query_indexes"),
        ("tests.unit.test_usage_schema", "exercise_usage_schema"),
    ],
)
def test_postgres_schema_guard_accepts_referenced_shared_setup(module: str, helper: str) -> None:
    source = (
        f"from {module} import {helper}\n"
        f"CreateSchema()\nDropSchema()\nconnection.run_sync({helper})"
    )
    _assert_postgres_schema_isolation(source, Path("shared"))


@pytest.mark.parametrize(
    "setup",
    [
        "connection.run_sync(unknown_setup)",
        "from tests.usage_query_schema_fixtures import exercise_query_upgrade",
        "from tests.usage_query_schema_fixtures import previous_180_schema\n"
        "connection.run_sync(previous_180_schema)",
        "from tests.unrelated import exercise_query_upgrade\n"
        "connection.run_sync(exercise_query_upgrade)",
    ],
)
def test_postgres_schema_guard_rejects_unsupported_setup(setup: str) -> None:
    with pytest.raises(AssertionError):
        _assert_postgres_schema_isolation(f"CreateSchema()\nDropSchema()\n{setup}", Path("unknown"))


def test_postgres_schema_guard_rejects_missing_or_changed_shared_owner(tmp_path: Path) -> None:
    source = (
        "from tests.usage_query_schema_fixtures import exercise_query_upgrade\n"
        "CreateSchema()\nDropSchema()\nconnection.run_sync(exercise_query_upgrade)"
    )
    with pytest.raises(AssertionError):
        _assert_postgres_schema_isolation(source, Path("missing"), tmp_path)
    helper = tmp_path / "tests" / "usage_query_schema_fixtures.py"
    helper.parent.mkdir()
    helper.write_text("def exercise_query_upgrade(connection):\n    pass\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_postgres_schema_isolation(source, Path("changed"), tmp_path)
    helper.write_text(
        "from another_module import ensure_usage_schema\n"
        "def exercise_query_upgrade(connection):\n    ensure_usage_schema(connection)\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _assert_postgres_schema_isolation(source, Path("wrong-owner"), tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        'CreateSchema()\nDropSchema()\nsearch_path = "private, public"\ncommand.upgrade()',
        "CreateSchema()\ncommand.upgrade()",
    ],
)
def test_postgres_schema_guard_rejects_public_fallback_and_missing_cleanup(source: str) -> None:
    with pytest.raises(AssertionError):
        _assert_postgres_schema_isolation(source, Path("unsafe"))


@pytest.mark.parametrize("unsafe", ['search_path = "private, public"\nDropSchema()', ""])
def test_postgres_schema_guard_rejects_unsafe_shared_setup(unsafe: str) -> None:
    source = (
        "from tests.usage_query_schema_fixtures import explain_query_indexes\n"
        f"CreateSchema()\n{unsafe}\nconnection.run_sync(explain_query_indexes)"
    )
    with pytest.raises(AssertionError):
        _assert_postgres_schema_isolation(source, Path("unsafe-shared"))


@pytest.mark.parametrize("corruption", ["unknown-table", "ordinary-search-tables"])
def test_schema_parity_rejects_noncanonical_native_tables(tmp_path: Path, corruption: str) -> None:
    database_path = tmp_path / "native_parity.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            assert_schema_matches_metadata(connection)
            if corruption == "unknown-table":
                connection.execute(sa.text("CREATE TABLE unregistered_metadata (id INTEGER)"))
                expected = "unregistered_metadata"
            else:
                connection.execute(sa.text("DROP TABLE palette_deliverables"))
                for suffix in ("", "_data", "_idx", "_docsize", "_config"):
                    connection.execute(
                        sa.text(f"CREATE TABLE palette_deliverables{suffix} (id INTEGER)")
                    )
                expected = "deliverables"
            with pytest.raises(AssertionError, match=expected):
                assert_schema_matches_metadata(connection)
    finally:
        engine.dispose()


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

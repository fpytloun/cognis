from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine
from cognis.store.models import Base, ManagedConversationSignal


def _revision_module(revision: str) -> Any:
    config = Config("cognis/store/migrations/alembic.ini")
    return ScriptDirectory.from_config(config).get_revision(revision).module


@pytest.mark.parametrize(
    ("revision", "name", "columns"),
    [
        (
            "114_managed_channel_fences",
            "uq_managed_signal_source_turn",
            ("link_id", "owner_epoch", "source_turn_id", "kind"),
        ),
        (
            "115_managed_channel_resume",
            "uq_managed_signal_resume_request",
            ("resume_request_id",),
        ),
    ],
)
@pytest.mark.parametrize("representation", ["constraint", "index"])
def test_managed_signal_migration_accepts_equivalent_named_unique(
    revision: str,
    name: str,
    columns: tuple[str, ...],
    representation: str,
) -> None:
    constraints = (
        [{"name": name, "column_names": list(reversed(columns))}]
        if representation == "constraint"
        else []
    )
    indexes = (
        [{"name": name, "column_names": list(reversed(columns)), "unique": True}]
        if representation == "index"
        else []
    )
    inspector = SimpleNamespace(
        get_unique_constraints=lambda _table: constraints,
        get_indexes=lambda _table: indexes,
    )

    assert (
        _revision_module(revision)._named_unique_representation(
            inspector,
            table_name="managed_conversation_signals",
            name=name,
            expected_columns=columns,
        )
        == representation
    )


@pytest.mark.parametrize(
    ("revision", "name", "columns"),
    [
        (
            "114_managed_channel_fences",
            "uq_managed_signal_source_turn",
            ("link_id", "owner_epoch", "source_turn_id", "kind"),
        ),
        (
            "115_managed_channel_resume",
            "uq_managed_signal_resume_request",
            ("resume_request_id",),
        ),
    ],
)
@pytest.mark.parametrize(
    ("representation", "actual_columns", "unique"),
    [
        ("index", ("resume_request_id",), False),
        ("constraint", ("wrong_column",), True),
    ],
)
def test_managed_signal_migration_rejects_incompatible_same_name(
    revision: str,
    name: str,
    columns: tuple[str, ...],
    representation: str,
    actual_columns: tuple[str, ...],
    unique: bool,
) -> None:
    constraints = (
        [{"name": name, "column_names": list(actual_columns)}]
        if representation == "constraint"
        else []
    )
    indexes = (
        [{"name": name, "column_names": list(actual_columns), "unique": unique}]
        if representation == "index"
        else []
    )
    inspector = SimpleNamespace(
        get_unique_constraints=lambda _table: constraints,
        get_indexes=lambda _table: indexes,
    )

    with pytest.raises(RuntimeError, match="incompatible unique definition"):
        _revision_module(revision)._named_unique_representation(
            inspector,
            table_name="managed_conversation_signals",
            name=name,
            expected_columns=columns,
        )


def test_managed_signal_orm_declares_resume_request_uniqueness() -> None:
    constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in ManagedConversationSignal.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }

    assert constraints["uq_managed_signal_resume_request"] == ("resume_request_id",)


def test_create_all_resume_request_unique_is_accepted_by_revision_115(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "managed-create-all-parity.db"
    sync_url = f"sqlite:///{database_path}"
    engine = sa.create_engine(sync_url)
    try:
        Base.metadata.create_all(engine)
        inspector = sa.inspect(engine)
        revision = _revision_module("115_managed_channel_resume")
        assert (
            revision._named_unique_representation(
                inspector,
                table_name="managed_conversation_signals",
                name="uq_managed_signal_resume_request",
                expected_columns=("resume_request_id",),
            )
            == "constraint"
        )
    finally:
        engine.dispose()


def test_revision_115_accepts_create_all_constraint_and_drops_it_on_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "managed-create-all-upgrade.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "114_managed_channel_fences")

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with sync_engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            with operations.batch_alter_table("managed_conversation_signals") as batch:
                batch.add_column(sa.Column("resume_request_id", sa.String(), nullable=True))
                batch.add_column(sa.Column("resume_turn_id", sa.String(), nullable=True))
                batch.add_column(
                    sa.Column("resume_admitted_at", sa.TIMESTAMP(timezone=True), nullable=True)
                )
                batch.add_column(sa.Column("resume_terminal_status", sa.String(), nullable=True))
                batch.create_unique_constraint(
                    "uq_managed_signal_resume_request",
                    ["resume_request_id"],
                )
    finally:
        sync_engine.dispose()

    command.upgrade(config, "115_managed_channel_resume")
    command.downgrade(config, "114_managed_channel_fences")

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        inspector = sa.inspect(sync_engine)
        columns = {
            column["name"] for column in inspector.get_columns("managed_conversation_signals")
        }
        unique_names = {
            str(item["name"])
            for item in (
                inspector.get_unique_constraints("managed_conversation_signals")
                + inspector.get_indexes("managed_conversation_signals")
            )
        }
        assert "resume_request_id" not in columns
        assert "uq_managed_signal_resume_request" not in unique_names
    finally:
        sync_engine.dispose()


def test_sqlite_bootstrap_unique_indexes_upgrade_and_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "managed-bootstrap-indexes.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "113_managed_channel_lifecycle")

    engine = create_engine(database_url)
    asyncio.run(run_schema_bootstrap(engine))
    asyncio.run(engine.dispose())

    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        indexes = {
            index["name"]: index
            for index in sa.inspect(sync_engine).get_indexes("managed_conversation_signals")
        }
        assert bool(indexes["uq_managed_signal_source_turn"]["unique"]) is True
        assert bool(indexes["uq_managed_signal_resume_request"]["unique"]) is True
    finally:
        sync_engine.dispose()

    command.upgrade(config, "117_group_context")
    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with sync_engine.connect() as connection:
            revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert revision == "117_group_context"
    finally:
        sync_engine.dispose()

    command.downgrade(config, "113_managed_channel_lifecycle")
    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        inspector = sa.inspect(sync_engine)
        columns = {
            column["name"] for column in inspector.get_columns("managed_conversation_signals")
        }
        unique_names = {
            str(item["name"])
            for item in (
                inspector.get_unique_constraints("managed_conversation_signals")
                + inspector.get_indexes("managed_conversation_signals")
            )
        }
        with sync_engine.connect() as connection:
            revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert revision == "113_managed_channel_lifecycle"
        assert "source_turn_id" not in columns
        assert "resume_request_id" not in columns
        assert "uq_managed_signal_source_turn" not in unique_names
        assert "uq_managed_signal_resume_request" not in unique_names
    finally:
        sync_engine.dispose()

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine
from cognis.store.models import WorkRecordRow

INDEX_NAME = "ix_work_records_overview_evidence"
EXPECTED_COLUMNS = [
    "owner_email",
    "session_id",
    "materializer_version",
    "category",
    "work_record_id",
]


@pytest.mark.asyncio
async def test_migration_141_matches_bootstrap_index(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    migration = importlib.import_module(
        "cognis.store.migrations.versions.141_work_overview_evidence_index"
    )
    try:
        await run_schema_bootstrap(engine)
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP INDEX {INDEX_NAME}"))

            def upgrade(sync_connection: object) -> None:
                context = MigrationContext.configure(sync_connection)
                migration.op = Operations(context)
                migration.upgrade()

            await connection.run_sync(upgrade)
            indexes = await connection.run_sync(
                lambda sync: inspect(sync).get_indexes("work_records")
            )
    finally:
        await engine.dispose()

    index = next(index for index in indexes if index.get("name") == INDEX_NAME)
    assert index["column_names"] == EXPECTED_COLUMNS
    assert "is_evidence IS TRUE" in str(
        dict(index.get("dialect_options") or {}).get("sqlite_where")
    )
    assert migration.down_revision == "140_work_retention_indexes"


def test_work_overview_evidence_index_postgresql_shape() -> None:
    index = next(index for index in WorkRecordRow.__table__.indexes if index.name == INDEX_NAME)
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    assert (
        "ON work_records "
        "(owner_email, session_id, materializer_version, category, work_record_id)" in ddl
    )
    assert "INCLUDE (entity_id)" in ddl
    assert "WHERE is_evidence IS TRUE" in ddl


@pytest.mark.parametrize(
    ("definition", "is_valid", "expected"),
    [
        (
            "CREATE INDEX ix_work_records_overview_evidence ON public.work_records "
            "USING btree (owner_email, session_id, materializer_version, category, "
            "work_record_id) INCLUDE (entity_id) WHERE (is_evidence IS TRUE)",
            True,
            True,
        ),
        (
            "CREATE INDEX ix_work_records_overview_evidence ON public.work_records "
            "USING btree (owner_email, session_id) WHERE (is_evidence IS TRUE)",
            True,
            False,
        ),
        (
            "CREATE INDEX ix_work_records_overview_evidence ON public.work_records "
            "USING btree (owner_email, session_id, materializer_version, category, "
            "work_record_id) INCLUDE (entity_id) WHERE (is_evidence IS TRUE)",
            False,
            False,
        ),
    ],
)
def test_postgresql_retry_guard_requires_valid_exact_shape(
    definition: str,
    is_valid: bool,
    expected: bool,
) -> None:
    migration = importlib.import_module(
        "cognis.store.migrations.versions.141_work_overview_evidence_index"
    )

    class Result:
        def first(self) -> object:
            return SimpleNamespace(index_definition=definition, is_valid=is_valid)

    class Bind:
        def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    assert migration._postgresql_index_matches(Bind()) is expected

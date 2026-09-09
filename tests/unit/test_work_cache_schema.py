from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine


@pytest.mark.asyncio
async def test_bootstrap_current_file_schema_matches_derived_cache_contract(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}")
    await run_schema_bootstrap(engine)
    async with engine.connect() as connection:
        (
            columns,
            checks,
            record_columns,
            revision_columns,
            revision_checks,
        ) = await connection.run_sync(
            lambda sync: (
                {
                    column["name"]: column
                    for column in inspect(sync).get_columns("work_current_files")
                },
                {
                    check["name"]: str(check["sqltext"])
                    for check in inspect(sync).get_check_constraints("work_current_files")
                },
                {
                    column["name"]: column
                    for column in inspect(sync).get_columns("work_record_files")
                },
                {
                    column["name"]: column
                    for column in inspect(sync).get_columns("work_live_revisions")
                },
                {
                    check["name"]: str(check["sqltext"])
                    for check in inspect(sync).get_check_constraints("work_live_revisions")
                },
            )
        )
    assert columns["additions"]["nullable"] is False
    assert columns["deletions"]["nullable"] is False
    nonnegative = checks["ck_work_current_files_nonnegative"]
    assert "additions >= 0" in nonnegative
    assert "deletions >= 0" in nonnegative
    for name in ("owner_email", "session_id", "materializer_version"):
        assert record_columns[name]["nullable"] is False
    assert revision_columns["owner_email"]["primary_key"] == 1
    assert revision_columns["revision"]["nullable"] is False
    assert "revision >= 0" in revision_checks["ck_work_live_revisions_nonnegative"]
    await engine.dispose()

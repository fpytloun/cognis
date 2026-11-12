"""Actual accepted session-query SQL; no planner coercion, mutable proxy code or raw fallback."""

import json
from datetime import timedelta
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, func, select, text

from cognis.core.usage.query_models import UsageScope, UsageSessionQuery
from cognis.core.usage.query_service import create_usage_query_service
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.usage.models import UsageProjectionStateRow, UsageSessionTotalRow
from cognis.store.usage.schema import ensure_usage_schema
from tests.integration.test_usage_query_api import ACTOR, END, START
from tests.integration.test_usage_query_api import db as db


@pytest.mark.asyncio
async def test_missing_global_session_index_still_returns_503_until_service_reconstruction(db):
    migration = import_module("cognis.store.migrations.versions.184_usage_session_day_index")
    async with db.engine.begin() as connection:

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()

        await connection.run_sync(downgrade)
    stale = await create_usage_query_service(
        db.factory, cursor_secret=b"usage-test-cursor-signing-key-32-bytes"
    )
    query = UsageSessionQuery(start=START, end=END, scope=UsageScope(mode="admin"))
    db.calls.clear()
    with pytest.raises(UsageQueryError) as failure:
        await stale.sessions(ACTOR, query)
    assert failure.value.code == "usage_index_unavailable" and failure.value.status == 503
    assert not any("FROM usage_" in statement for statement in db.calls)
    async with db.engine.begin() as connection:
        await connection.run_sync(ensure_usage_schema)
        await connection.run_sync(ensure_usage_schema)
    with pytest.raises(UsageQueryError, match="time-leading"):
        await stale.sessions(ACTOR, query)  # Capability snapshots are deliberately immutable.
    installed = await create_usage_query_service(
        db.factory, cursor_secret=b"usage-test-cursor-signing-key-32-bytes"
    )
    response = await installed.sessions(ACTOR, query)
    assert {item.session_id for item in response.items} == {
        f"session_usage_{number}" for number in range(4)
    }


@pytest.mark.asyncio
async def test_ordinary_global_session_query_plan_uses_real_day_index(db, tmp_path):
    # Retained-detail distribution: many unrelated days, both Cognis and proxy
    # rows, and the same owner/day/generation fence used by the actual service.
    async with db.engine.begin() as connection:
        source = dict(
            (await connection.execute(select(UsageSessionTotalRow.__table__).limit(1)))
            .mappings()
            .one()
        )
        state = dict(
            (await connection.execute(select(UsageProjectionStateRow.__table__).limit(1)))
            .mappings()
            .one()
        )
        for offset in range(0, 2048, 128):
            totals, states = [], []
            for number in range(offset, offset + 128):
                day = START.date() - timedelta(days=number + 1)
                owner = f"global_session_plan_{number}"
                proxy = bool(number % 2)
                totals.append(
                    {
                        **source,
                        "owner_key": owner,
                        "bucket_day": day,
                        "session_kind": "proxy" if proxy else "cognis",
                        "session_id": None if proxy else source["session_id"],
                        "client_session_id": f"plan_client_{number}" if proxy else None,
                    }
                )
                states.append({**state, "owner_key": owner, "bucket_day": day})
            await connection.execute(UsageSessionTotalRow.__table__.insert(), totals)
            await connection.execute(UsageProjectionStateRow.__table__.insert(), states)
        await connection.execute(text("ANALYZE usage_session_totals"))
        await connection.execute(text("ANALYZE usage_projection_state"))
    service = await create_usage_query_service(
        db.factory, cursor_secret=b"usage-test-cursor-signing-key-32-bytes"
    )
    recorded = []

    def record(_connection, _cursor, statement, parameters, _context, _many):
        recorded.append((statement, parameters))

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await service.sessions(
            ACTOR, UsageSessionQuery(start=START, end=END, scope=UsageScope(mode="admin"))
        )
        assert len(response.items) == 4
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)
    statement, parameters = next(
        item for item in recorded if item[0].startswith("WITH usage_session_page")
    )
    async with db.engine.begin() as connection:
        if db.engine.dialect.name == "postgresql":
            plan = (
                await connection.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}", parameters
                )
            ).scalar_one()
            rendered = plan if isinstance(plan, str) else json.dumps(plan)
        else:
            plan = (
                await connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)
            ).all()
            rendered = "\n".join(str(row) for row in plan)
    # Both ordinary planner choices are bounded: source day-range first, or the
    # projection day-range followed by exact owner/day/generation source probes.
    assert "ix_usage_session_day" in rendered or (
        "ix_usage_projection_day" in rendered
        and (
            "sqlite_autoindex_usage_session_totals_1" in rendered
            or "usage_session_totals_pkey" in rendered
        )
    ), rendered
    assert "usage_attempts" not in statement and "context_payload" not in statement
    row = UsageSessionTotalRow
    mixed_query = (
        select(row.session_kind, func.count())
        .where(row.bucket_day >= START.date() - timedelta(days=2), row.bucket_day < END.date())
        .group_by(row.session_kind)
    )
    async with db.engine.begin() as connection:
        assert dict((await connection.execute(mixed_query)).all()) == {"cognis": 5, "proxy": 1}
        mixed_sql = str(
            mixed_query.compile(dialect=db.engine.dialect, compile_kwargs={"literal_binds": True})
        )
        if db.engine.dialect.name == "postgresql":
            mixed_plan = (
                await connection.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {mixed_sql}"
                )
            ).scalar_one()
            mixed_rendered = mixed_plan if isinstance(mixed_plan, str) else json.dumps(mixed_plan)
        else:
            mixed_plan = (await connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {mixed_sql}")).all()
            mixed_rendered = "\n".join(str(item) for item in mixed_plan)
    assert "ix_usage_session_day" in mixed_rendered, mixed_rendered
    evidence = tmp_path / f"usage-session-plan-{db.engine.dialect.name}.json"
    evidence.write_text(
        json.dumps(
            {
                "actual_service_sql": statement,
                "actual_service_plan": rendered,
                "mixed_session_sql": mixed_sql,
                "mixed_session_plan": mixed_rendered,
            },
            indent=2,
        )
        + "\n"
    )

"""Actual service SQL plans against an isolated PostgreSQL index distribution."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event

from cognis.core.usage.query_models import UsageAttemptQuery, UsageQuery, UsageScope
from tests.integration.test_usage_query_api import ACTOR, END, START
from tests.integration.test_usage_query_api import db as db
from tests.usage_query_schema_fixtures import explain_query_indexes


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["postgres"], indirect=True)
async def test_actual_query_plans_and_statement_budgets(db, tmp_path):
    """Do not force planner settings or substitute simplified hand-written SQL."""
    recorded = []

    def record(connection, cursor, statement, parameters, context, executemany):
        recorded.append((statement, parameters))

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    scope = UsageScope(mode="admin")
    try:
        before = len(recorded)
        await db.service.query(ACTOR, UsageQuery(start=START, end=END, scope=scope))
        assert len(recorded) - before <= 8
        summary = next(item for item in recorded[before:] if " AS group_key" in item[0])
        before = len(recorded)
        await db.service.attempts(ACTOR, UsageAttemptQuery(start=START, end=END, scope=scope))
        assert len(recorded) - before <= 8
        attempts = next(
            item
            for item in recorded[before:]
            if "usage_attempts.row_id" in item[0] and "usage_valuations" in item[0]
        )
        before = len(recorded)
        await db.service.requests(ACTOR, UsageAttemptQuery(start=START, end=END, scope=scope))
        assert len(recorded) - before <= 8
        requests = next(
            item
            for item in recorded[before:]
            if "usage_attempts.row_id" in item[0] and "usage_request_stats" in item[0]
        )
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)

    # This helper supplies plan-only raw payloads and canonical dimension rows.
    # Functional envelope/authorization evidence belongs to test_usage_query_api.
    async with db.engine.begin() as connection:
        baseline = await connection.run_sync(explain_query_indexes)
        plans = {}
        for name, (statement, parameters) in (
            ("summary", summary),
            ("attempts", attempts),
            ("requests", requests),
        ):
            result = await connection.exec_driver_sql(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}", parameters
            )
            plan = result.scalar_one()
            plan = json.loads(plan) if isinstance(plan, str) else plan
            rendered = json.dumps(plan)
            if name == "summary":
                assert "ix_usage_daily_day" in rendered or "ix_usage_projection_day" in rendered
            else:
                assert "ix_usage_attempt_time" in rendered
            assert plan[0]["Execution Time"] < 1000, plan
            plans[name] = plan
    evidence = tmp_path / "usage-query-plans.json"
    evidence.write_text(
        json.dumps({"service_sql": plans, "storage_baseline": baseline}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Usage query plan evidence: {evidence}")

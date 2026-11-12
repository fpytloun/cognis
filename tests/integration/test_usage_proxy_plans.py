"""Actual proxy service SQL with a selective, same-subject namespace distribution."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event

from cognis.core.usage.query_models import UsageAttemptQuery, UsageFilters, UsageSessionQuery
from cognis.store.usage.models import UsageAttemptRow
from tests.integration.test_usage_proxy_api import proxy
from tests.integration.test_usage_query_api import ACTOR, END, START
from tests.integration.test_usage_query_api import db as db


@pytest.mark.asyncio
async def test_actual_proxy_query_plans_and_budgets(db, tmp_path):
    async with db.factory() as session:
        fact = await proxy(session, 4)
        busy = await proxy(session, 5, label="busy-run")
        await session.commit()
        template = {
            column.name: getattr(fact, column.name)
            for column in UsageAttemptRow.__table__.columns
            if column.name != "row_id"
        }
    # Plan-only facts include a busy selected namespace and many other namespaces.
    # Populate before minting refs so their allocator fence covers this distribution.
    async with db.engine.begin() as connection:
        await connection.execute(
            UsageAttemptRow.__table__.insert(),
            [
                template
                | {
                    "attempt_id": f"plan_attempt_{index}",
                    "request_id": f"plan_request_{index}",
                    "client_session_id": "busy-run" if index < 2560 else f"plan_label_{index}",
                }
                for index in range(5120)
            ],
        )
        await connection.exec_driver_sql("ANALYZE usage_attempts")
    recorded = []

    def record(connection, cursor, statement, parameters, context, executemany):
        recorded.append((statement, parameters))

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    try:
        page = await db.service.sessions(
            ACTOR, UsageSessionQuery(start=START, end=END, session_type="proxy")
        )
        assert len(recorded) <= 12
        assert len(page.items) == 2
        sparse = next(item for item in page.items if item.client_session_id == "shared-run")
        probes = next(item for item in recorded if " AS first_0" in item[0])
        summaries = next(item for item in recorded if "WITH proxy_page" in item[0])
        before = len(recorded)
        requests = await db.service.requests(
            ACTOR,
            UsageAttemptQuery(start=START, end=END, selection_ref=sparse.selection_ref),
        )
        assert len(recorded) - before <= 10
        assert len(requests.items) == 1
        namespace = next(
            item
            for item in recorded[before:]
            if "usage_attempts.row_id" in item[0] and "usage_request_stats" in item[0]
        )
        busy_request = await db.service.requests(
            ACTOR,
            UsageAttemptQuery(
                start=START,
                end=END,
                filters=UsageFilters(logical_request_ids=[busy.request_id]),
            ),
        )
        before = len(recorded)
        await db.service.attempts(
            ACTOR,
            UsageAttemptQuery(
                start=START, end=END, selection_ref=busy_request.items[0].selection_ref
            ),
        )
        assert len(recorded) - before <= 10
        trail = next(
            item
            for item in recorded[before:]
            if "usage_attempts.row_id" in item[0] and "usage_valuations" in item[0]
        )
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)

    # Functional envelopes and authority are covered by test_usage_proxy_api.
    async with db.engine.begin() as connection:
        plans = {}
        for name, (statement, parameters) in (
            ("summaries", summaries),
            ("retained_edges", probes),
            ("namespace_requests", namespace),
            ("request_trail", trail),
        ):
            if db.dialect == "postgres":
                result = await connection.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}", parameters
                )
                plan = result.scalar_one()
                plan = json.loads(plan) if isinstance(plan, str) else plan
                assert plan[0]["Execution Time"] < 1000, plan
            else:
                result = await connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                )
                plan = [list(row) for row in result]
            rendered = json.dumps(plan)
            if name in {"retained_edges", "namespace_requests"}:
                assert "ix_usage_client_session_time" in rendered, plan
            if name == "request_trail":
                assert "ix_usage_request_attempt" in rendered, plan
            assert "json_extract(usage_attempts.context_payload" not in statement.lower()
            plans[name] = plan
    evidence = tmp_path / f"usage-proxy-plans-{db.dialect}.json"
    evidence.write_text(json.dumps(plans, indent=2) + "\n", encoding="utf-8")
    print(f"Proxy query plan evidence: {evidence}")

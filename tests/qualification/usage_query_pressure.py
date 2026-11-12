"""Bounded authorized usage-service query workload against a private PG schema."""

from __future__ import annotations

import json
import time

import pytest

from cognis.core.usage.query_models import UsageAttemptQuery, UsageQuery, UsageScope
from tests.integration.test_usage_query_api import ACTOR, END, START, _seed
from tests.integration.test_usage_query_api import db as db
from tests.usage_query_schema_fixtures import explain_query_indexes


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
async def test_authorized_query_pressure(db):
    """Count actual service queries over the existing synthetic index population."""
    async with db.factory.begin() as session:
        for index in range(4, 68):
            await _seed(session, index, ACTOR)
    scope = UsageScope(mode="admin")
    query = UsageQuery(start=START, end=END, scope=scope)
    details = UsageAttemptQuery(start=START, end=END, scope=scope)
    operations = (
        ("summary", lambda: db.service.query(ACTOR, query)),
        ("attempts", lambda: db.service.attempts(ACTOR, details)),
        ("requests", lambda: db.service.requests(ACTOR, details)),
    )
    results = {}
    for name, call in operations:
        await call()
        samples = []
        counts = []
        for _ in range(5):
            before = len(db.calls)
            started = time.perf_counter()
            await call()
            samples.append((time.perf_counter() - started) * 1000)
            counts.append(len(db.calls) - before)
        assert max(counts) <= 8
        results[name] = {"milliseconds": samples, "sql_counts": counts}
    # The plan fixture intentionally uses plan-only payloads, so never expose
    # those rows to the authorized service calls above.
    async with db.engine.begin() as connection:
        plans = await connection.run_sync(explain_query_indexes)
    print(
        "query_pressure "
        + json.dumps(
            {
                "service_synthetic_attempts": 68,
                "plan_only_synthetic_attempts": 2560,
                "samples_per_operation": 5,
                "indexed_plan_names": sorted(plans),
                "operations": results,
            }
        )
    )

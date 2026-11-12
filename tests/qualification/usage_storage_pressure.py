"""Explicit, bounded PostgreSQL storage qualification; not a provider benchmark.

Run only with a task-owned PostgreSQL endpoint and the repository validation lock.
The shared storage fixture creates and removes its own unique schema.
"""

from __future__ import annotations

import asyncio
import json
import resource
import time

import pytest
from sqlalchemy import event, func, select

from cognis.core.usage.capture_models import UsageWriterLimits
from cognis.store.usage.models import UsageValuationRow
from tests.usage_query_schema_fixtures import explain_query_indexes

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.parametrize("usage_storage", ["postgres"], indirect=True)
async def test_storage_pressure_and_idle(usage_storage):
    """Measure a synchronous burst separately from active and idle storage work."""
    h = usage_storage
    limits = UsageWriterLimits()
    sql_count = 0

    def count_sql(connection, cursor, statement, parameters, context, executemany):
        nonlocal sql_count
        sql_count += 1

    event.listen(h.engine.sync_engine, "before_cursor_execute", count_sql)
    try:
        await h.writer.drain()
        await h.maintenance.publish_price(h.price())
        for i in range(8):
            assert h.writer.try_emit(h.event(i, pin="price_1"))
        before_active = sql_count
        active_started = time.perf_counter()
        await h.pump()
        active_seconds = time.perf_counter() - active_started
        async with h.sessions() as session:
            valuations = await session.scalar(select(func.count()).select_from(UsageValuationRow))
        assert valuations == 8
        print(
            "storage_active "
            + json.dumps(
                {
                    "facts": 8,
                    "valuation_receipts": valuations,
                    "seconds": active_seconds,
                    "sql_statements_including_count": sql_count - before_active,
                }
            )
        )
        await h.projector.start()
        assert await h.projector.drain(timeout_seconds=5)
        before_idle = sql_count
        await asyncio.sleep(0.25)
        idle_sql = sql_count - before_idle
        assert idle_sql == 0

        # No await between offers: the writer cannot consume capacity in this burst.
        observations = [h.event(i + 100) for i in range(limits.queue_items + 64)]
        before_burst = sql_count
        started = time.perf_counter()
        accepted = sum(h.writer.try_emit(item) for item in observations)
        burst_seconds = time.perf_counter() - started
        burst = h.writer.diagnostics()
        assert sql_count == before_burst
        assert accepted == limits.queue_items
        assert burst.pending_items == limits.queue_items
        assert burst.pending_bytes <= limits.queue_bytes
        assert burst.dropped_finalized == 64
        # This burst is deliberately not a producer or inference latency claim.
        print(
            "storage_burst "
            + json.dumps(
                {
                    "offers": len(observations),
                    "accepted": accepted,
                    "rejected": burst.dropped_finalized,
                    "pending_bytes": burst.pending_bytes,
                    "queue_items_limit": limits.queue_items,
                    "queue_bytes_limit": limits.queue_bytes,
                    "seconds": burst_seconds,
                    "maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    "idle_seconds": 0.25,
                    "idle_sql": idle_sql,
                }
            )
        )
        before_active = sql_count
        started = time.perf_counter()
        await h.writer.drain(timeout_seconds=5)
        # The full burst is intentionally too large for a fast qualification
        # projection; record writer work without claiming projection completion.
        writer_seconds = time.perf_counter() - started
        after = h.writer.diagnostics()
        print(
            "storage_writer "
            + json.dumps(
                {
                    "seconds": writer_seconds,
                    "sql_statements": sql_count - before_active,
                    "pending_items": after.pending_items,
                    "batches": after.committed_batches,
                    "dropped_finalized": after.dropped_finalized,
                }
            )
        )
    finally:
        event.remove(h.engine.sync_engine, "before_cursor_execute", count_sql)


@pytest.mark.parametrize("usage_storage", ["postgres"], indirect=True)
async def test_postgres_index_population(usage_storage):
    """Run the existing bounded synthetic indexed-plan fixture on its own schema."""
    async with usage_storage.engine.begin() as connection:
        started = time.perf_counter()
        plans = await connection.run_sync(explain_query_indexes)
        seconds = time.perf_counter() - started
    print(
        "storage_plans "
        + json.dumps(
            {
                "synthetic_attempts": 2560,
                "indexes": sorted(plans),
                "elapsed_seconds": seconds,
                "execution_ms": {index: plan[0]["Execution Time"] for index, plan in plans.items()},
            }
        )
    )

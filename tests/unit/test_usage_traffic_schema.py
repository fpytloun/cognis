"""Prior-schema traffic upgrades preserve canonical facts and fence old generations."""

import importlib
from copy import deepcopy
from datetime import timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select

from cognis.core.usage.contributions import digest_json
from cognis.core.usage.maintenance import UsageMaintenanceScope
from cognis.core.usage.projector import lock_scope
from cognis.store.usage.models import (
    UsageArchivedTotalRow,
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageHourlyTotalRow,
    UsageMaintenanceJobRow,
    UsageProjectionStateRow,
    UsageRequestLogRow,
    UsageRequestStatsRow,
    UsageSessionTotalRow,
)
from cognis.store.usage.schema import ensure_usage_schema
from tests.unit.test_usage_request_logs import seed
from tests.usage_storage_harness import usage_storage as usage_storage


def exercise_traffic_upgrade(connection):
    migration = importlib.import_module("cognis.store.migrations.versions.187_usage_api_clients")
    migration.op = Operations(MigrationContext.configure(connection))
    migration.downgrade()
    assert "traffic_origin" not in {
        column["name"] for column in inspect(connection).get_columns("usage_attempts")
    }
    ensure_usage_schema(connection)
    ensure_usage_schema(connection)
    columns = {column["name"] for column in inspect(connection).get_columns("usage_attempts")}
    assert {"traffic_origin", "traffic_auth_type", "traffic_client_id", "traffic_key_id"} <= columns
    rows = connection.execute(select(UsageProjectionStateRow.__table__)).mappings().all()
    assert rows
    for row in rows:
        assert row["traffic_version"] == 1
        assert row["ready"] is False
        assert row["retention_paused"] is True


@pytest.mark.asyncio
async def test_traffic_bootstrap_is_idempotent_from_previous_schema(usage_storage):
    harness = usage_storage
    now = harness.clock()
    async with harness.sessions.begin() as session:
        await lock_scope(session, "upgrade@tests.local", now.date(), now)
    async with harness.engine.begin() as connection:
        await connection.run_sync(exercise_traffic_upgrade)
    await harness.pump()
    async with harness.sessions() as session:
        row = await session.scalar(select(UsageProjectionStateRow))
        assert row.traffic_version == 2
        assert row.ready is True


@pytest.mark.asyncio
async def test_traffic_backfill_rearms_completed_job_and_skips_failed_scope(usage_storage):
    """A rollout can leave a scope pre-traffic after its job completed; it must not stall."""
    harness = usage_storage
    now = harness.clock()
    day = now.date()
    async with harness.sessions.begin() as session:
        for owner in ("a-completed@tests.local", "b-failed@tests.local", "c-pending@tests.local"):
            await lock_scope(session, owner, day, now)
    async with harness.engine.begin() as connection:
        await connection.run_sync(exercise_traffic_upgrade)
    async with harness.sessions.begin() as session:
        for owner, status in (
            ("a-completed@tests.local", "complete"),
            ("b-failed@tests.local", "failed"),
        ):
            payload = {"owner_key": owner, "bucket_day": day.isoformat()}
            session.add(
                UsageMaintenanceJobRow(
                    job_id=f"utraffic_{digest_json(payload)}",
                    kind="rebuild",
                    status=status,
                    scope=payload,
                    cursor={"phase": "cleanup"},
                    processed_count=3,
                    owner_epoch=1,
                    created_at=now,
                    updated_at=now,
                    completed_at=now if status == "complete" else None,
                )
            )
    await harness.pump()
    async with harness.sessions() as session:
        states = {
            row.owner_key: row
            for row in (await session.scalars(select(UsageProjectionStateRow))).all()
        }
        assert states["a-completed@tests.local"].traffic_version == 2
        assert states["c-pending@tests.local"].traffic_version == 2
        # Failed jobs keep their diagnosis gate; the scope stays visibly pre-traffic.
        assert states["b-failed@tests.local"].traffic_version == 1


async def scope_for(harness):
    async with harness.sessions() as session:
        row = await session.scalar(
            select(UsageRequestLogRow).order_by(UsageRequestLogRow.started_at.desc()).limit(1)
        )
        return UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day)


async def canonical_snapshot(harness):
    async with harness.sessions() as session:
        attempts = (
            await session.scalars(select(UsageAttemptRow).order_by(UsageAttemptRow.row_id))
        ).all()
        requests = (await session.scalars(select(UsageRequestStatsRow))).all()
        return [
            (
                row.attempt_id,
                row.context_digest,
                row.context_payload,
                row.terminal_digest,
                row.terminal_payload,
            )
            for row in attempts
        ], [
            (row.request_id, row.attribution, row.server_digest, row.server_payload)
            for row in requests
        ]


@pytest.mark.asyncio
async def test_populated_traffic_upgrade_rebuild_and_privacy(usage_storage):
    harness = usage_storage
    harness.clock.now -= timedelta(days=62)
    await seed(harness, legacy=True)
    await harness.maintenance.request_archive(await scope_for(harness))
    await harness.pump()
    harness.clock.now += timedelta(days=31)
    await seed(harness, legacy=True, sequence=11)
    retained_scope = await scope_for(harness)
    async with harness.sessions.begin() as session:
        # Reconstruct the previous JSON shape: default origin was not serialized.
        for row in (await session.scalars(select(UsageAttemptRow))).all():
            payload = deepcopy(row.context_payload)
            payload["attribution"]["identity"].pop("traffic_origin", None)
            row.context_payload = payload
        for row in (await session.scalars(select(UsageRequestStatsRow))).all():
            attribution = deepcopy(row.attribution)
            attribution["identity"].pop("traffic_origin", None)
            row.attribution = attribution
    before = await canonical_snapshot(harness)
    async with harness.engine.begin() as connection:
        await connection.run_sync(exercise_traffic_upgrade)
    await harness.pump()
    assert await canonical_snapshot(harness) == before
    async with harness.sessions() as session:
        states = (await session.scalars(select(UsageProjectionStateRow))).all()
        assert all(row.traffic_version == 2 and row.ready for row in states)
        assert (await session.scalar(select(func.sum(UsageDailyTotalRow.finalized_count)))) == 4
        headers = (await session.scalars(select(UsageRequestLogRow))).all()
        assert len(headers) == 1
        assert headers[0].traffic_origin == "api"
        assert headers[0].traffic_key_id == "key-client-123"
        assert headers[0].http_metadata is None
        assert all(
            row.traffic_key_id == "key-client-123"
            for row in (await session.scalars(select(UsageAttemptRow))).all()
        )
        for row in (await session.scalars(select(UsageArchivedTotalRow))).all():
            assert "key-client-123" not in str(row.dimensions)
            assert "upgrade-client-session" not in str(row.dimensions)
    await harness.maintenance.request_archive(retained_scope)
    await harness.pump()
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(UsageRequestLogRow)) == 0
        for table in (UsageDailyTotalRow, UsageHourlyTotalRow, UsageSessionTotalRow):
            for row in (await session.scalars(select(table))).all():
                assert row.traffic_client_id is None
                assert row.traffic_key_id is None
        assert (await session.scalar(select(func.sum(UsageDailyTotalRow.finalized_count)))) == 4

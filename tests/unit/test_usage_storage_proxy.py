"""Proxy correlation is one retained session contribution, never canonical grouping."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from cognis.core.usage.maintenance import UsageMaintenanceScope
from cognis.core.usage.sessions import session_correlation
from cognis.models.usage import UsageAttemptStarted
from cognis.store.usage.models import (
    UsageArchivedTotalRow,
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageSessionTotalRow,
    UsageValuationRow,
)
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
async def test_proxy_namespace_and_verified_precedence(usage_storage: UsageStorageHarness) -> None:
    storage = usage_storage
    first = storage.event(1, proxy="unverified_session")
    second_data = storage.event(2, proxy="unverified_session").model_dump()
    second_data["context"]["attribution"]["identity"]["client_id"] = "second_client"
    second = type(first).model_validate(second_data)
    assert session_correlation(first.context.attribution).session_kind == "proxy"
    assert storage.writer.try_emit(first)
    assert storage.writer.try_emit(second)
    await storage.pump()
    async with storage.sessions() as session:
        sessions = (await session.scalars(select(UsageSessionTotalRow))).all()
        assert len(sessions) == 2
        assert {row.client_id for row in sessions} == {None, "second_client"}
        assert all(row.session_id is None and row.finalized_count == 1 for row in sessions)
        daily = (await session.scalars(select(UsageDailyTotalRow))).all()
        assert len(daily) == 1 and daily[0].finalized_count == 2
        assert "unverified_session" not in str(daily[0].dimensions)
        rows = (await session.scalars(select(UsageAttemptRow))).all()
        assert all(row.client_session_id == "unverified_session" for row in rows)
    data = first.context.attribution.model_dump()
    data["resources"] = {"session_id": "verified_session", "provenance": "controller"}
    bound = type(first.context.attribution).model_validate(data)
    correlation = session_correlation(bound)
    assert correlation.session_kind == "cognis" and correlation.client_session_id is None
    data["resources"] = None
    data["identity"]["service_id"] = "intaris"
    assert session_correlation(type(bound).model_validate(data)) is None


@pytest.mark.asyncio
async def test_archive_retires_selected_proxy_not_late_final_bucket(
    usage_storage: UsageStorageHarness,
) -> None:
    storage = usage_storage
    storage.clock.now -= timedelta(days=40)
    old = storage.event(1, proxy="shared_old_start_day")
    pending = storage.event(2, proxy="shared_old_start_day")
    assert storage.writer.try_emit(old)
    assert storage.writer.try_emit(
        UsageAttemptStarted(observation_id="start_pending", context=pending.context)
    )
    await storage.pump()
    storage.clock.now += timedelta(days=40)
    late = pending.model_copy(update={"finalized_at": storage.clock.now})
    assert storage.writer.try_emit(late)
    await storage.pump()
    async with storage.sessions() as session:
        source = await session.scalar(
            select(UsageAttemptRow).where(UsageAttemptRow.attempt_id == "ua_1")
        )
        owner, day = source.owner_key, source.bucket_day
    scope = UsageMaintenanceScope(owner_key=owner, bucket_day=day)
    await storage.maintenance.request_archive(scope)
    await storage.pump()
    async with storage.sessions() as session:
        retained = (await session.scalars(select(UsageAttemptRow))).all()
        assert [row.attempt_id for row in retained] == ["ua_2"]
        sessions = (await session.scalars(select(UsageSessionTotalRow))).all()
        assert len(sessions) == 1 and sessions[0].finalized_count == 1
        daily = (await session.scalars(select(UsageDailyTotalRow))).one()
        assert daily.finalized_count == 2
        archives = (await session.scalars(select(UsageArchivedTotalRow))).all()
        assert len(archives) == 1 and archives[0].finalized_count == 1
        assert "shared_old_start_day" not in str(archives[0].dimensions)
        receipts = (
            await session.scalars(
                select(UsageValuationRow).where(UsageValuationRow.source_kind == "archive")
            )
        ).all()
        assert "shared_old_start_day" not in str([row.contribution for row in receipts])
    await storage.maintenance.request_rebuild(scope)
    await storage.pump()
    async with storage.sessions() as session:
        sessions = (
            await session.scalars(
                select(UsageSessionTotalRow).where(UsageSessionTotalRow.generation == 2)
            )
        ).all()
        assert len(sessions) == 1 and sessions[0].finalized_count == 1
        daily = (
            await session.scalars(
                select(UsageDailyTotalRow).where(UsageDailyTotalRow.generation == 2)
            )
        ).one()
        assert daily.finalized_count == 2

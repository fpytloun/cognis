"""Real runtime composition and settings-fenced maintenance on SQLite and PostgreSQL."""

import asyncio
from dataclasses import asdict
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from cognis.api.usage_lifecycle import UsageDeliveryLifecycleMiddleware
from cognis.core.settings_management import SettingsManagement
from cognis.core.usage.capture_models import UsageWriterLimits
from cognis.core.usage.maintenance import UsageMaintenanceScope, UsageMaintenanceService
from cognis.core.usage.runtime import UsageRuntime
from cognis.core.usage.service import UsageAccountingService
from cognis.core.usage.settings import RETENTION_KEYS, load_usage_retention_policy
from cognis.models.usage import UsageAttemptStarted, UsageRequestStats, UsageServerSpan
from cognis.providers.llm.usage_delivery import bind_usage_request
from cognis.settings_schema import get_setting_spec
from cognis.store.models import Setting, User
from cognis.store.usage.models import (
    UsageAttemptRow,
    UsageCoverageEpochRow,
    UsageRequestStatsRow,
    UsageSessionTotalRow,
)
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


async def prepare_settings(h: UsageStorageHarness) -> None:
    async with h.engine.begin() as connection:
        await connection.run_sync(User.__table__.create, checkfirst=True)
        await connection.run_sync(Setting.__table__.create, checkfirst=True)
    async with h.sessions.begin() as session:
        session.add(User(email="usage+test@cognis.test", is_active=False))
        await session.flush()
        session.add_all(
            [
                Setting(key=key, value=get_setting_spec(key).default, category="usage")
                for key in RETENTION_KEYS
            ]
        )


@pytest.mark.asyncio
async def test_runtime_records_real_delivery_and_closes_coverage(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    await prepare_settings(h)
    runtime = UsageRuntime(h.sessions, controller_owner_id="runtime_delivery_test")
    await runtime.start()
    request = runtime.capture.request(attribution=h.event().context.attribution)

    async def app(scope, _receive, send):
        bind_usage_request(scope, request)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    try:
        await UsageDeliveryLifecycleMiddleware(app, capture=lambda: runtime.capture)(
            {"type": "http", "state": {}}, receive, send
        )
        await runtime.service.drain()
        assert await runtime.projector.drain()
        async with h.sessions() as session:
            stats = await session.get(UsageRequestStatsRow, request.request_id)
            assert stats is not None and stats.logical_outcome == "success"
        assert (b"x-cognis-request-id", request.request_id.encode()) in messages[0]["headers"]
        assert asdict(runtime.diagnostics)["capture_rejected"] == 0
    finally:
        await runtime.aclose()
    async with h.sessions() as session:
        epoch = await session.get(UsageCoverageEpochRow, runtime.service.coverage_epoch_id)
        assert epoch.coverage_status == "complete" and epoch.ended_at is not None


@pytest.mark.asyncio
async def test_disable_commits_before_queued_archive_can_delete(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    await prepare_settings(h)
    h.clock.now -= timedelta(days=40)
    finalized = h.event(proxy="settings_guarded")
    assert h.writer.try_emit(finalized)
    assert h.writer.try_emit(
        UsageAttemptStarted(
            observation_id="pending_guarded",
            context=h.event(2).context,
        )
    )
    await h.pump()
    async with h.sessions.begin() as session:
        await session.execute(
            update(Setting).where(Setting.key == "usage.retention_enabled").values(value=True)
        )
        row = await session.scalar(select(UsageAttemptRow).where(UsageAttemptRow.fact_version > 0))
        scope = UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day)
    maintenance = UsageMaintenanceService(
        h.sessions,
        projector=h.projector,
        controller_owner_id="settings_guard_test",
        retention_loader=load_usage_retention_policy,
    )
    preview = await maintenance.preview_retention(scope)
    assert preview.enabled and preview.attempt_candidates == 1
    assert preview.pending_or_unprojected_preserved
    archive = await maintenance.request_archive(scope)
    async with h.sessions.begin() as session:
        await session.execute(
            update(Setting).where(Setting.key == "usage.retention_enabled").values(value=False)
        )
    for _ in range(20):
        if not await maintenance.process_page():
            break
    assert (await maintenance.status(archive.job_id)).status == "superseded"
    async with h.sessions() as session:
        assert len((await session.scalars(select(UsageAttemptRow))).all()) == 2


@pytest.mark.asyncio
async def test_settings_preview_does_not_activate_or_wake(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    await prepare_settings(h)
    runtime = UsageRuntime(h.sessions, controller_owner_id="settings_preview_test")
    signals = []
    management = SettingsManagement(
        session_factory=h.sessions,
        update_lock=asyncio.Lock(),
        apply=AsyncMock(),
        notify_usage_settings=runtime.notify_settings,
        usage_settings_committed=lambda: signals.append(True),
    )
    result = await management.put(
        key="usage.retention_enabled", value=True, updated_by="usage+test@cognis.test", preview=True
    )
    assert result["application_scope"] == "next_operation"
    assert not signals
    async with h.sessions() as session:
        assert not (await load_usage_retention_policy(session)).enabled
    await management.put(
        key="usage.detail_retention_days", value=60, updated_by="usage+test@cognis.test"
    )
    assert signals == [True]
    management.apply.assert_not_awaited()
    with pytest.raises(ValueError):
        await management.put(
            key="usage.detail_retention_days", value=29, updated_by="usage+test@cognis.test"
        )


@pytest.mark.asyncio
async def test_writer_shutdown_is_bounded_when_final_coverage_is_unavailable(
    usage_storage: UsageStorageHarness,
    monkeypatch,
) -> None:
    h = usage_storage
    writer = UsageAccountingService(
        h.sessions,
        controller_owner_id="shutdown_test",
        projection_waker=h.projector.wake,
        limits=UsageWriterLimits(shutdown_seconds=0.05),
    )
    await writer.start()
    await writer.drain()
    blocked = asyncio.Event()
    monkeypatch.setattr(writer, "_close_epoch", blocked.wait)
    await asyncio.wait_for(writer.aclose(), 1)
    assert writer.diagnostics().writer_failed
    assert not writer.try_emit(h.event())


@pytest.mark.asyncio
async def test_failed_writer_stops_admission_and_persists_gap(
    usage_storage: UsageStorageHarness,
    monkeypatch,
) -> None:
    h = usage_storage
    await h.writer.drain()

    async def broken_batch(*_args):
        raise RuntimeError("must_not_log_raw_details")

    monkeypatch.setattr(h.writer, "_commit_batch", broken_batch)
    assert h.writer.try_emit(h.event())
    await h.writer.drain()
    assert h.writer.diagnostics().writer_failed
    assert not h.writer.try_emit(h.event(2))
    await h.writer.aclose()
    async with h.sessions() as session:
        epoch = await session.get(UsageCoverageEpochRow, h.writer.coverage_epoch_id)
        assert epoch.coverage_status == "gapped"


@pytest.mark.asyncio
async def test_capture_failure_outside_writer_persists_gap(
    usage_storage: UsageStorageHarness,
    monkeypatch,
) -> None:
    h = usage_storage
    await prepare_settings(h)
    runtime = UsageRuntime(h.sessions, controller_owner_id="capture_gap_test")
    await runtime.start()

    def broken_sink(_observation):
        raise RuntimeError("must_not_log_raw_capture_details")

    monkeypatch.setattr(runtime.service, "try_emit", broken_sink)
    assert not runtime.capture.emit(h.event())
    assert runtime.diagnostics.capture_failed == 1
    await runtime.aclose()
    async with h.sessions() as session:
        epoch = await session.get(UsageCoverageEpochRow, runtime.service.coverage_epoch_id)
        assert epoch.coverage_status == "gapped"


@pytest.mark.asyncio
async def test_server_only_span_is_not_an_empty_proxy_summary(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    event = h.event(proxy="server_only_span")
    assert h.writer.try_emit(
        UsageRequestStats(
            observation_id=event.observation_id,
            request_id=event.context.trace.request_id,
            request_started_at=h.clock.now,
            observed_at=h.clock.now,
            attribution=event.context.attribution,
            controller_owner_id="span_test",
            coverage_epoch_id=h.writer.coverage_epoch_id,
            stats_scope="server",
            server=UsageServerSpan(received_at=h.clock.now, elapsed_ms=10),
        )
    )
    await h.pump()
    async with h.sessions() as session:
        summary = (await session.scalars(select(UsageSessionTotalRow))).one()
        assert summary.session_kind == "proxy"
        assert summary.server_elapsed_count == 1
        assert summary.finalized_count == summary.logical_completed_count == 0

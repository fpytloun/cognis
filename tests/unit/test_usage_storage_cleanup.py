"""History expiry cannot erase recoverable work or turn into an idle retry loop."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from cognis.api.models import ProviderUsageResponse
from cognis.core.usage.maintenance import UsageMaintenanceScope, UsageRetentionPolicy
from cognis.core.usage.quota import UsageQuotaRecorder
from cognis.store.usage.models import ProviderUsageSampleRow, UsageMaintenanceJobRow
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
async def test_cleanup_preserves_recoverable_jobs_and_fresh_gauges(
    usage_storage: UsageStorageHarness,
    monkeypatch,
) -> None:
    h = usage_storage
    failed = await h.maintenance.request_reprice(UsageMaintenanceScope())
    complete = await h.maintenance.request_reprice(UsageMaintenanceScope())
    await h.pump()
    old = h.clock.now - timedelta(days=400)
    async with h.sessions.begin() as session:
        await session.execute(
            update(UsageMaintenanceJobRow)
            .where(UsageMaintenanceJobRow.job_id.in_([failed.job_id, complete.job_id]))
            .values(completed_at=old)
        )
        await session.execute(
            update(UsageMaintenanceJobRow)
            .where(UsageMaintenanceJobRow.job_id == failed.job_id)
            .values(status="failed", error_code="repair_required")
        )
    recorder = UsageQuotaRecorder(h.writer.try_emit_provider_sample)
    for observation, at in (("expired_monitor", old), ("fresh_monitor", h.clock.now)):
        report = ProviderUsageResponse(
            provider_id="provider",
            source="chatgpt_codex_usage",
            ok=True,
            fetched_at=at.isoformat(),
            primary={"used_percent": 20, "window_minutes": 300},
        )
        recorder.observe(
            "provider", "usage+test@cognis.test", "generation_1", observation, at, report
        )
    await h.maintenance.configure_retention(UsageRetentionPolicy(enabled=True))
    await h.pump()
    async with h.sessions() as session:
        assert (await session.get(UsageMaintenanceJobRow, failed.job_id)).status == "failed"
        assert await session.get(UsageMaintenanceJobRow, complete.job_id) is None
        samples = (await session.scalars(select(ProviderUsageSampleRow))).all()
        assert len(samples) == 1 and samples[0].observation_id == "fresh_monitor"
    expiries = []
    monkeypatch.setattr(h.projector, "schedule_expiry", expiries.append)
    await h.maintenance.on_idle()
    assert expiries and expiries[-1] > h.clock.now
    await asyncio.sleep(0)

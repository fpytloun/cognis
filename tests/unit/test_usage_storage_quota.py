"""Quota points are gauges, deduplicated source observations, never spend counters."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from cognis.api.models import ProviderUsageResponse
from cognis.core.usage.quota import UsageQuotaRecorder
from cognis.store.usage.models import ProviderUsageSampleRow
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
async def test_header_redelivery_keeps_original_time_and_gauge(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    at = h.clock.now
    recorder = UsageQuotaRecorder(h.writer.try_emit_provider_sample)
    report = ProviderUsageResponse(
        provider_id="provider",
        source="anthropic_rate_limit_headers",
        ok=True,
        observed_at=at.isoformat(),
        primary={"used_percent": 25, "window_minutes": 1, "limit": 100, "remaining": 75},
        rate_limit_headers={"authorization": "must_not_be_stored"},
    )
    recorder.observe("provider", "usage+test@cognis.test", "generation_1", "refresh_1", at, report)
    await h.writer.drain()
    recorder.observe(
        "provider",
        "usage+test@cognis.test",
        "generation_1",
        "refresh_2",
        at + timedelta(minutes=1),
        report,
    )
    await h.writer.drain()
    async with h.sessions() as session:
        row = (await session.scalars(select(ProviderUsageSampleRow))).one()
        assert row.latest_remaining == 75 and row.maximum_utilization == 25
        assert row.sample_count == 1
        assert row.first_observed_at == row.last_observed_at
        assert "must_not_be_stored" not in str(
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
        )
    recorder.observe("provider", "usage+test@cognis.test", "generation_2", "refresh_2", at, report)
    await h.writer.drain()
    async with h.sessions() as session:
        assert len((await session.scalars(select(ProviderUsageSampleRow))).all()) == 2


@pytest.mark.asyncio
async def test_refresh_windows_are_independent_gauges_and_credits_are_not_cost(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    recorder = UsageQuotaRecorder(h.writer.try_emit_provider_sample)
    report = ProviderUsageResponse(
        provider_id="provider",
        source="chatgpt_codex_usage",
        ok=True,
        primary={"used_percent": 40, "window_minutes": 300},
        secondary={"used_percent": 10, "window_minutes": 10080},
        credits={"has_credits": True, "unlimited": False, "balance": "12.5"},
    )
    recorder.observe(
        "provider", "usage+test@cognis.test", "generation_1", "monitor_1", h.clock.now, report
    )
    await h.writer.drain()
    async with h.sessions() as session:
        rows = (await session.scalars(select(ProviderUsageSampleRow))).all()
        assert {row.window_key for row in rows} == {"primary", "secondary", "credits"}
        credit = next(row for row in rows if row.window_key == "credits")
        assert credit.latest_remaining == Decimal("12.5") and credit.maximum_utilization is None
    assert recorder.diagnostics.failed_samples == 0


@pytest.mark.asyncio
async def test_elevenlabs_credit_windows_reach_durable_quota_history(
    usage_storage: UsageStorageHarness,
) -> None:
    from cognis.providers.llm.elevenlabs_usage import normalize_subscription_usage

    h = usage_storage
    payload = normalize_subscription_usage(
        {
            "character_count": 11000,
            "character_limit": 10000,
            "can_extend_character_limit": True,
            "max_credit_limit_extension": 5000,
            "next_character_count_reset_unix": int((h.clock.now + timedelta(days=30)).timestamp()),
        }
    )
    payload["fetched_at"] = h.clock.now.isoformat()
    report = ProviderUsageResponse.model_validate({**payload, "provider_id": "provider"})
    recorder = UsageQuotaRecorder(h.writer.try_emit_provider_sample)
    recorder.observe(
        "provider", "usage+test@cognis.test", "generation_1", "monitor_1", h.clock.now, report
    )
    await h.writer.drain()
    async with h.sessions() as session:
        rows = (await session.scalars(select(ProviderUsageSampleRow))).all()
        assert len(rows) == 2
        assert {(row.latest_remaining, row.maximum_utilization) for row in rows} == {
            (Decimal(0), Decimal(100)),
            (Decimal(4000), Decimal(20)),
        }
    assert recorder.diagnostics.failed_samples == 0


def test_bad_gauge_and_failed_sink_remain_visible() -> None:
    def failing_sink(_sample):
        raise RuntimeError("must_not_log_the_raw_exception")

    recorder = UsageQuotaRecorder(failing_sink)
    from datetime import UTC, datetime

    report = ProviderUsageResponse(
        provider_id="provider",
        source="chatgpt_codex_usage",
        ok=True,
        primary={"used_percent": 40, "window_minutes": 300},
    )
    recorder.observe(
        "provider", "usage+test@cognis.test", "generation_1", "monitor_1", datetime.now(UTC), report
    )
    assert recorder.diagnostics.failed_samples == 1

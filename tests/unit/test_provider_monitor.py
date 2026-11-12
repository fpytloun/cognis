from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.api.dashboard_issues import collect_dashboard_issues
from cognis.api.models import LLMProviderRequest, LLMProviderUpdateRequest
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.provider_monitor import ProviderMonitor
from cognis.providers.llm.errors import ProviderReauthorizationRequired
from cognis.providers.llm.monitoring import MonitoringOptions, probe_connectivity, usage_severity
from cognis.store.database import create_session_factory
from cognis.store.models import (
    Base,
    InboxNotificationRow,
    LLMProvider,
    ProviderMonitorSnapshot,
    User,
)


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def factory(request, tmp_path):
    schema = None
    admin = None
    if request.param == "postgres":
        url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
        url = url.replace("postgresql://", "postgresql+asyncpg://")
        schema = f"provider_monitor_{uuid.uuid4().hex}"
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(sa_schema.CreateSchema(schema))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'monitor.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    try:
        yield sessions, engine
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as conn:
                await conn.execute(sa_schema.DropSchema(schema, cascade=True))
            await admin.dispose()


async def seed(
    sessions,
    *,
    enabled=True,
    status="active",
    provider_id="claude",
    owner="owner@cognis.test",
    preset="anthropic",
):
    async with sessions() as session:
        if owner != "system@cognis.local" and await session.get(User, owner) is None:
            session.add(User(email=owner, role="user", is_active=True))
        session.add(
            LLMProvider(
                provider_id=provider_id,
                display_name=provider_id,
                backend="litellm",
                location="controller",
                owner_email=owner,
                status=status,
                config={"preset": preset, "monitoring": {"enabled": enabled}},
            )
        )
        await session.commit()


class Source:
    def __init__(self):
        self.calls = 0
        self.failure = False
        self.used = 85
        self.started = asyncio.Event()
        self.release = None

    async def get_provider_usage(self, provider_id):
        self.calls += 1
        self.started.set()
        if self.release:
            await self.release.wait()
        if self.failure:
            raise RuntimeError("secret upstream body must never appear in API")
        return {
            "ok": True,
            "source": "anthropic_subscription_usage",
            "fetched_at": datetime.now(UTC).isoformat(),
            "primary": {
                "used_percent": self.used,
                "resets_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            },
        }

    async def check_provider_connectivity(self, provider):
        pytest.fail("Subscription usage check must not make a second connectivity request")


@pytest.mark.asyncio
async def test_monitor_elevenlabs_quota_persists_then_clears_revoked_access(factory):
    from unittest.mock import AsyncMock

    from cognis.providers.llm.elevenlabs_usage import (
        normalize_subscription_usage,
        unavailable_usage,
    )

    sessions, _ = factory
    source = Source()
    reset_at = datetime.now(UTC) + timedelta(days=1)
    source.get_provider_usage = AsyncMock(
        return_value=normalize_subscription_usage(
            {
                "tier": "free",
                "character_count": 8000,
                "character_limit": 10000,
                "next_character_count_reset_unix": int(reset_at.timestamp()),
            }
        )
    )
    source.check_provider_connectivity = AsyncMock(
        return_value={"status": "healthy", "detail": "Available"}
    )
    await seed(sessions, provider_id="elevenlabs", preset="elevenlabs")
    monitor = ProviderMonitor(sessions, source)
    first = await monitor.get("elevenlabs", force=True)
    assert first.health_status == "healthy" and first.usage.ok
    source.check_provider_connectivity.assert_not_awaited()
    async with sessions() as session:
        saved = await session.get(ProviderMonitorSnapshot, "elevenlabs")
        window = saved.usage["additional_rate_limits"][0]["primary"]
        assert (window["limit"], window["remaining"]) == (10000, 2000)
    source.get_provider_usage.return_value = unavailable_usage(
        "Subscription quota access is denied for this API key"
    )
    refreshed = await monitor.get("elevenlabs", force=True)
    assert refreshed.health_status == "healthy"
    assert not refreshed.usage.ok
    assert refreshed.usage.additional_rate_limits == []
    source.check_provider_connectivity.assert_awaited_once()


@pytest.mark.asyncio
async def test_cache_deadline_disabled_manual_and_config_options(factory):
    sessions, _ = factory
    await seed(sessions)
    source = Source()
    monitor = ProviderMonitor(sessions, source)
    first = await monitor.get("claude")
    assert first.alert_severity == "warning"
    assert (first.next_refresh_at - first.checked_at).total_seconds() == 900
    await monitor.check_due()
    await ProviderMonitor(sessions, source).get("claude")
    assert source.calls == 1
    async with sessions() as db:
        provider = await db.get(LLMProvider, "claude")
        provider.config = {
            **provider.config,
            "monitoring": {
                "enabled": False,
                "interval_minutes": 60,
                "warning_percent": 10,
                "critical_percent": 0,
            },
        }
        await db.commit()
    # Changing options preserves observations but recomputes thresholds/deadlines.
    cached = await monitor.get("claude")
    assert cached.usage.primary.used_percent == 85
    assert cached.alert_severity is None
    assert (cached.next_refresh_at - cached.checked_at).total_seconds() == 3600
    await monitor.check_due()
    assert source.calls == 1
    source.used = 100
    manual = await monitor.get("claude", force=True)
    assert source.calls == 2
    assert manual.alert_severity == "critical"
    await seed(sessions, enabled=False, provider_id="manual-only")
    assert (await monitor.get("manual-only")).checked_at is None
    assert source.calls == 2


@pytest.mark.asyncio
async def test_two_replicas_coalesce_forced_and_scheduled_checks(factory):
    sessions, _ = factory
    await seed(sessions)
    source = Source()
    source.release = asyncio.Event()
    first, second = ProviderMonitor(sessions, source), ProviderMonitor(sessions, source)
    task = asyncio.create_task(first.get("claude", force=True))
    await source.started.wait()
    other = asyncio.create_task(second.get("claude", force=True))
    scheduled = asyncio.create_task(second.check_due())
    await asyncio.sleep(0.2)
    assert source.calls == 1
    source.release.set()
    one, two, _ = await asyncio.gather(task, other, scheduled)
    assert source.calls == 1
    assert one.checked_at == two.checked_at


@pytest.mark.asyncio
async def test_refresh_timeout_includes_concurrency_queue(factory, monkeypatch):
    sessions, _ = factory
    await seed(sessions)
    source = Source()
    monitor = ProviderMonitor(sessions, source)
    monkeypatch.setattr("cognis.core.provider_monitor.REFRESH_TIMEOUT_SECONDS", 0.1)
    await monitor._concurrency.acquire()
    await monitor._concurrency.acquire()
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(TimeoutError):
            await monitor.get("claude", force=True)
        assert asyncio.get_running_loop().time() - started < 1
        assert source.calls == 0
    finally:
        monitor._concurrency.release()
        monitor._concurrency.release()


@pytest.mark.asyncio
async def test_failures_preserve_last_success_backoff_and_recover(factory):
    sessions, _ = factory
    await seed(sessions)
    source = Source()
    monitor = ProviderMonitor(sessions, source)
    good = await monitor.get("claude")
    source.failure = True
    failed = await monitor.get("claude", force=True)
    assert failed.health_checked_at == good.health_checked_at
    assert failed.usage.fetched_at == good.usage.fetched_at
    assert failed.error == "Provider check failed"
    assert failed.stale
    assert (failed.next_refresh_at - failed.checked_at).total_seconds() == 1800
    source.failure = False
    source.used = 10
    recovered = await monitor.get("claude", force=True)
    assert recovered.error is None
    assert recovered.alert_severity is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", [True, False])
async def test_inflight_obsolete_results_are_discarded(factory, invalidate):
    sessions, _ = factory
    await seed(sessions)
    source = Source()
    source.release = asyncio.Event()
    monitor = ProviderMonitor(sessions, source)
    task = asyncio.create_task(monitor.get("claude", force=True))
    await source.started.wait()
    if invalidate:
        await ProviderMonitor(sessions, source).invalidate("claude")
    else:
        async with sessions() as db:
            provider = await db.get(LLMProvider, "claude")
            provider.config = {**provider.config, "base_url": "https://changed.cognis.test"}
            await db.commit()
    source.release.set()
    result = await task
    assert result.checked_at is None
    async with sessions() as db:
        assert await db.get(ProviderMonitorSnapshot, "claude") is None


@pytest.mark.asyncio
async def test_visibility_and_dashboard_issue_recovery(factory):
    sessions, _ = factory
    await seed(sessions)
    await seed(sessions, provider_id="other", owner="other@cognis.test")
    await seed(sessions, provider_id="system", owner="system@cognis.local")
    await seed(sessions, provider_id="inactive", status="inactive")
    source = Source()
    monitor = ProviderMonitor(sessions, source)
    await monitor.get("claude")
    visible = await monitor.list_visible("owner@cognis.test")
    assert {item.provider_id for item in visible} == {"claude", "system"}
    async with sessions() as db:
        issues = await collect_dashboard_issues(db, user_email="owner@cognis.test")
    assert [(item.kind, item.severity) for item in issues.issues] == [("provider_quota", "warning")]
    source.used = 5
    await monitor.get("claude", force=True)
    async with sessions() as db:
        assert not (await collect_dashboard_issues(db, user_email="owner@cognis.test")).issues


class RecordingInbox:
    def __init__(self):
        self.published = []

    async def publish(self, row):
        self.published.append(row.notification_id)


async def inbox_rows(sessions):
    async with sessions() as db:
        return list(
            (
                await db.scalars(
                    select(InboxNotificationRow).order_by(InboxNotificationRow.user_email)
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_rejected_credentials_alert_once_until_reauthorized(factory):
    sessions, _ = factory
    await seed(sessions)
    source = Source()
    inbox = RecordingInbox()
    monitor = ProviderMonitor(sessions, source, inbox=inbox)
    await monitor.get("claude")

    async def rejected(provider_id):
        source.calls += 1
        raise ProviderReauthorizationRequired(provider_id=provider_id, provider_label="Claude")

    healthy_usage = source.get_provider_usage
    source.get_provider_usage = rejected
    failed = await monitor.get("claude", force=True)
    assert failed.error == "Reauthorization required"
    assert failed.health_status == "error"
    assert failed.health_detail == (
        "Claude sign-in expired or was revoked. Reauthorize the provider in Settings > Providers."
    )
    async with sessions() as db:
        issues = (await collect_dashboard_issues(db, user_email="owner@cognis.test")).issues
    assert [(item.kind, item.severity, item.title, item.action_label) for item in issues] == [
        ("provider_auth", "critical", "claude needs reauthorization", "Reauthorize")
    ]
    assert issues[0].action_url == "/settings?tab=providers&provider=claude"
    rows = await inbox_rows(sessions)
    assert [(row.user_email, row.kind, row.href) for row in rows] == [
        (
            "owner@cognis.test",
            "provider.reauthorization_required",
            "/settings?tab=providers&provider=claude",
        )
    ]
    assert inbox.published == [rows[0].notification_id]

    # Repeated failures keep the alert without notifying again.
    await monitor.get("claude", force=True)
    assert len(await inbox_rows(sessions)) == 1

    source.get_provider_usage = healthy_usage
    source.used = 5
    recovered = await monitor.get("claude", force=True)
    assert recovered.error is None and recovered.health_status == "healthy"
    async with sessions() as db:
        assert not (await collect_dashboard_issues(db, user_email="owner@cognis.test")).issues


@pytest.mark.asyncio
async def test_shared_provider_reauthorization_notifies_active_admins(factory):
    sessions, _ = factory
    await seed(sessions, owner="system@cognis.local")
    async with sessions() as db:
        db.add_all(
            [
                User(email="admin@cognis.test", role="admin", is_active=True),
                User(email="former-admin@cognis.test", role="admin", is_active=False),
                User(email="user@cognis.test", role="user", is_active=True),
            ]
        )
        await db.commit()
    source = Source()

    async def rejected(provider_id):
        raise ProviderReauthorizationRequired(provider_id=provider_id)

    source.get_provider_usage = rejected
    await ProviderMonitor(sessions, source).get("claude", force=True)
    assert [row.user_email for row in await inbox_rows(sessions)] == ["admin@cognis.test"]


@pytest.mark.asyncio
async def test_inference_rejection_report_publishes_without_waiting_for_schedule(factory):
    sessions, _ = factory
    await seed(sessions)
    source = Source()
    monitor = ProviderMonitor(sessions, source)
    await monitor.get("claude")

    async def rejected(provider_id):
        source.calls += 1
        raise ProviderReauthorizationRequired(provider_id=provider_id)

    source.get_provider_usage = rejected
    monitor.report_reauthorization_required("claude")
    monitor.report_reauthorization_required("claude")
    await asyncio.gather(*monitor._reauthorization_checks.values())
    await asyncio.sleep(0)  # let done callbacks release the per-provider slot
    assert not monitor._reauthorization_checks
    assert source.calls == 2  # one initial success, one coalesced check
    async with sessions() as db:
        snapshot = await db.get(ProviderMonitorSnapshot, "claude")
        assert snapshot.error == "Reauthorization required"
    # An already published rejection needs no further upstream check.
    monitor.report_reauthorization_required("claude")
    await asyncio.gather(*monitor._reauthorization_checks.values())
    assert source.calls == 2
    await monitor.stop()


@pytest.mark.asyncio
async def test_migration_and_bootstrap_idempotence(factory):
    _, engine = factory
    migration = importlib.import_module("cognis.store.migrations.versions.152_provider_monitor")

    def upgrade(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()

    def downgrade(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()

    async with engine.begin() as conn:
        await conn.run_sync(downgrade)
        await conn.run_sync(upgrade)
        columns = await conn.run_sync(
            lambda sync: {
                c["name"] for c in inspect(sync).get_columns("provider_monitor_snapshots")
            }
        )
        assert columns == {c.name for c in ProviderMonitorSnapshot.__table__.columns}
        await conn.run_sync(downgrade)
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    async with engine.connect() as conn:
        assert "provider_monitor_snapshots" in await conn.run_sync(
            lambda sync: inspect(sync).get_table_names()
        )


def test_threshold_contract_and_expired_evidence():
    options = MonitoringOptions()
    assert options.interval_minutes == 15
    now = datetime.now(UTC)
    usage = {
        "ok": True,
        "source": "anthropic_subscription_usage",
        "fetched_at": now.isoformat(),
        "primary": {"used_percent": 80},
        "additional_rate_limits": [{"primary": {"used_percent": 100}}],
    }
    assert usage_severity(usage, options, now=now) == "critical"
    usage["additional_rate_limits"] = []
    assert usage_severity(usage, options, now=now) == "warning"
    usage["primary"]["resets_at"] = (now - timedelta(seconds=1)).isoformat()
    usage["limit_reached"] = True
    assert usage_severity(usage, options, now=now) is None
    for payload in [
        {"warning_percent": 10, "critical_percent": 20},
        {"interval_minutes": 0},
        {"warning_percent": float("nan")},
    ]:
        with pytest.raises(ValidationError):
            LLMProviderRequest(display_name="test", config={"monitoring": payload})
        with pytest.raises(ValidationError):
            LLMProviderUpdateRequest(config={"monitoring": payload})
    usage = {
        "ok": True,
        "source": "anthropic_rate_limit_headers",
        "observed_at": (now - timedelta(minutes=2)).isoformat(),
        "limit_reached": True,
    }
    assert usage_severity(usage, options, now=now) is None


def test_expired_header_windows_do_not_alert_from_aggregate_limit_flag():
    now = datetime.now(UTC)
    usage = {
        "ok": True,
        "source": "anthropic_rate_limit_headers",
        "observed_at": now.isoformat(),
        "limit_reached": True,
        "additional_rate_limits": [
            {
                "limit_reached": True,
                "primary": {
                    "used_percent": 100,
                    "resets_at": (now - timedelta(seconds=1)).isoformat(),
                },
            }
        ],
    }
    assert usage_severity(usage, MonitoringOptions(), now=now) is None
    usage["additional_rate_limits"][0]["primary"]["resets_at"] = (
        now + timedelta(seconds=10)
    ).isoformat()
    assert usage_severity(usage, MonitoringOptions(), now=now) == "critical"


@pytest.mark.asyncio
async def test_probe_only_reads_metadata_and_does_not_follow_redirects(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    provider = LLMProvider(provider_id="openai", location="controller", config={"preset": "openai"})
    result = await probe_connectivity(provider, {"api_key": "test-key"})
    assert result["status"] == "healthy"
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", "https://api.openai.com/v1/models")
    ]
    provider.location = "executor"
    assert (await probe_connectivity(provider, {}))["status"] == "unknown"
    assert len(requests) == 1

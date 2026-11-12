"""Real canonical-store boundaries; the inference sink never awaits the store."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.core.usage.capture_models import UsageProviderSample, UsageWriterLimits
from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import (
    UsageAttemptFinalized,
    UsageAttemptStarted,
    UsageClientLabels,
    UsageClientSpan,
    UsageHookOrigin,
    UsageRequestStats,
    UsageServerSpan,
)
from cognis.store.database import create_engine
from cognis.store.usage.codec import terminal_event
from cognis.store.usage.models import (
    ProviderUsageSampleRow,
    UsageAttemptRow,
    UsageCoverageEpochRow,
    UsageDailyTotalRow,
    UsageProjectionOutboxRow,
    UsageRequestStatsRow,
)
from cognis.store.usage.schema import ensure_usage_schema
from tests.usage_fixtures import finalized_attempt


def observed_event(**changes: Any) -> UsageAttemptFinalized:
    data = finalized_attempt().model_dump()
    data["context"]["started_at"] = datetime.now(UTC)
    data["finalized_at"] = datetime.now(UTC)
    data.update(changes)
    return UsageAttemptFinalized.model_validate(data)


@pytest.fixture
async def storage(tmp_path: Any) -> Any:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'capture.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(ensure_usage_schema)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, sessions
    await engine.dispose()


@pytest.mark.asyncio
async def test_start_final_coalesce_without_projecting_or_duplicating_context(storage: Any) -> None:
    _engine, sessions = storage
    wakes = []
    service = UsageAccountingService(
        sessions, controller_owner_id="writer", projection_waker=lambda: wakes.append(True)
    )
    await service.start()
    final = observed_event()
    start = UsageAttemptStarted(observation_id="obs_start", context=final.context)
    assert service.try_emit(start)
    assert service.try_emit(final)
    assert (await service.drain()).pending_items == 0
    async with sessions() as session:
        row = await session.scalar(select(UsageAttemptRow))
        assert row.start_observed
        assert row.fact_version == 1
        assert row.projected_version == 0
        assert "context" not in row.terminal_payload
        assert row.requested_parameters == {}
        assert terminal_event(row) == final
        registered = row.registered_at
        terminal_recorded = row.finalized_at
        assert await session.scalar(select(func.count()).select_from(UsageProjectionOutboxRow)) == 1
        assert await session.scalar(select(func.count()).select_from(UsageDailyTotalRow)) == 0
    assert service.try_emit(final)
    await service.drain()
    async with sessions() as session:
        row = await session.scalar(select(UsageAttemptRow))
        assert row.registered_at == registered
        assert row.finalized_at == terminal_recorded
        assert await session.scalar(select(func.count()).select_from(UsageProjectionOutboxRow)) == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_final_without_start_and_conflict_content_verification(storage: Any) -> None:
    _engine, sessions = storage
    service = UsageAccountingService(
        sessions, controller_owner_id="writer", projection_waker=lambda: None
    )
    await service.start()
    final = observed_event()
    assert service.try_emit(final)
    await service.drain()
    conflicting = UsageAttemptFinalized.model_validate({**final.model_dump(), "outcome": "error"})
    assert service.try_emit(conflicting)
    await service.drain()
    async with sessions() as session:
        row = await session.scalar(select(UsageAttemptRow))
        assert not row.start_observed
        assert row.outcome == "success"
        epoch = await session.scalar(select(UsageCoverageEpochRow))
        assert epoch.dropped_finalized == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_queue_overload_is_synchronous_and_records_gap(storage: Any) -> None:
    _engine, sessions = storage
    service = UsageAccountingService(
        sessions,
        controller_owner_id="writer",
        projection_waker=lambda: None,
        limits=UsageWriterLimits(queue_items=1),
    )
    await service.start()
    final = observed_event()
    assert service.try_emit(final)
    assert not service.try_emit(final)
    assert service.diagnostics().dropped_finalized == 1
    await service.drain()
    await service.aclose()
    async with sessions() as session:
        epoch = await session.scalar(select(UsageCoverageEpochRow))
        assert epoch.coverage_status == "gapped"


@pytest.mark.asyncio
async def test_no_idle_sql_and_no_sql_in_try_emit(storage: Any) -> None:
    engine, sessions = storage
    statements = []
    event.listen(
        engine.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2])
    )
    service = UsageAccountingService(
        sessions, controller_owner_id="writer", projection_waker=lambda: None
    )
    await service.start()
    for _ in range(5):
        await asyncio.sleep(0)
    assert statements == []
    assert service.try_emit(observed_event())
    assert statements == []
    await service.drain()
    count = len(statements)
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(statements) == count
    await service.aclose()


@pytest.mark.asyncio
async def test_expired_missing_final_rejected_but_existing_pending_allowed(storage: Any) -> None:
    _engine, sessions = storage
    service = UsageAccountingService(
        sessions, controller_owner_id="writer", projection_waker=lambda: None
    )
    await service.start()
    final = observed_event()
    assert service.try_emit(UsageAttemptStarted(context=final.context, observation_id="obs_start"))
    await service.drain()
    # Existing pending lookup must not depend on a new ingestion timestamp.
    service._limits = UsageWriterLimits(replay_days=1)
    assert service.try_emit(final)
    old = final.model_dump()
    old["context"]["started_at"] = datetime.now(UTC) - timedelta(days=90)
    old["context"]["trace"]["attempt_id"] = "attempt_expired"
    assert service.try_emit(UsageAttemptFinalized.model_validate(old))
    await service.drain()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(UsageAttemptRow)) == 1
        row = await session.scalar(select(UsageAttemptRow))
        assert row.fact_version == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_persistence_failure_has_bounded_retry_and_no_payload_log(
    storage: Any, caplog: Any
) -> None:
    _engine, sessions = storage
    service = UsageAccountingService(
        sessions, controller_owner_id="writer", projection_waker=lambda: None
    )
    calls = []

    async def unavailable(*args: Any) -> None:
        calls.append(True)
        raise OperationalError(
            "private prompt in driver message", {}, RuntimeError("private secret")
        )

    service._commit_batch = unavailable
    await service.start()
    assert service.try_emit(observed_event())
    result = await service.drain()
    assert len(calls) == 3
    assert result.dropped_finalized == 1
    assert "private" not in caplog.text
    await service.aclose()


@pytest.mark.asyncio
async def test_server_client_stats_remain_separate_and_idempotent(storage: Any) -> None:
    _engine, sessions = storage
    service = UsageAccountingService(
        sessions, controller_owner_id="writer", projection_waker=lambda: None
    )
    await service.start()
    final = observed_event()
    base = {
        "request_id": final.context.trace.request_id,
        "request_started_at": final.context.started_at,
        "observed_at": datetime.now(UTC),
        "attribution": final.context.attribution,
        "controller_owner_id": "writer",
        "coverage_epoch_id": service.coverage_epoch_id,
    }
    server = UsageRequestStats(
        **base,
        stats_scope="server",
        observation_id="server_stats",
        logical_outcome="success",
        server=UsageServerSpan(elapsed_ms=123),
    )
    client = UsageRequestStats(
        **base,
        stats_scope="client",
        observation_id="client_stats",
        client=UsageClientSpan(source="authenticated_report", elapsed_ms=150),
    )
    assert service.try_emit(server)
    assert service.try_emit(client)
    await service.drain()
    assert service.try_emit(server)
    await service.drain()
    async with sessions() as session:
        row = await session.scalar(select(UsageRequestStatsRow))
        assert row.fact_version == 2
        assert row.server_elapsed_ms == 123
        assert row.client_elapsed_ms == 150
        assert row.logical_outcome == "success"
    await service.aclose()


@pytest.mark.asyncio
async def test_provider_sample_hook_reuses_same_queue_without_provider_io(storage: Any) -> None:
    _engine, sessions = storage
    service = UsageAccountingService(
        sessions, controller_owner_id="writer", projection_waker=lambda: None
    )
    await service.start()
    sample = UsageProviderSample(
        sample_id="sample_usage",
        provider_id="provider_usage",
        provider_owner="usage@cognis.test",
        account_scope_id="scope_usage",
        configuration_generation="configuration_1",
        source="refresh",
        observation_id="refresh_usage",
        window_key="requests_1h",
        observed_at=datetime.now(UTC),
        remaining=42,
    )
    assert service.try_emit_provider_sample(sample)
    await service.drain()
    assert service.try_emit_provider_sample(sample)
    await service.drain()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProviderUsageSampleRow)) == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_optional_reduction_is_visible_in_epoch_diagnostics(storage: Any) -> None:
    _, sessions = storage
    service = UsageAccountingService(
        sessions,
        controller_owner_id="controller_a",
        projection_waker=lambda: None,
    )
    await service.start()
    data = observed_event().model_dump()
    data["context"]["metadata_reduction"] = {
        "fields": ["client_labels"],
        "reason": "envelope_budget",
    }
    observation = UsageAttemptFinalized.model_validate(data)
    assert service.try_emit(observation)
    result = await service.drain()
    assert result.metadata_reduced_events == 1
    async with sessions() as session:
        epoch = await session.scalar(select(UsageCoverageEpochRow))
        assert epoch.metadata_reduced_events == 1
        assert epoch.coverage_status == "gapped"
    await service.aclose()


def test_hook_display_unicode_and_unknown_external_operation() -> None:
    assert UsageHookOrigin(hook_id="hook_usage", hook_name="Denní rekapitulace 🛠️").hook_name
    assert UsageClientLabels(operation="external_unknown").operation is None
    assert UsageClientLabels(operation="search_embedding").operation == "search_embedding"


def test_dense_proxy_hook_parameters_terminal_fits_budget() -> None:
    data = observed_event().model_dump()
    context = data["context"]
    context["attribution"]["identity"].update(
        resource_owner="usage+test@cognis.test",
        provider_payer="payer@cognis.test",
        routing_owner="payer@cognis.test",
        auth_type="service_jwt",
        service_id="intaris",
        service_operation="policy_compile",
        client_id="client_0000000000000001",
        key_record_id="key_0000000000000002",
    )
    context["attribution"]["resources"] = {
        "provenance": "controller",
        "project_id": "proj_0000000000000002",
        "session_id": "sess_0000000000000004",
        "conversation_id": "conv_0000000000000005",
        "activity_scope_id": "activity_01",
        "agent_id": "agent_01",
        "agent_profile_id": "deep",
        "task_id": "task_0000000000000003",
        "step_run_id": "step_0000000000000006",
        "turn_id": "turn_01",
    }
    context["attribution"]["hook"] = {
        "hook_id": "hook_0000000000000007",
        "hook_revision": 3,
        "hook_name": "Večerní rekapitulace 🛠️",
        "hook_type": "after_turn",
        "invocation_id": "hrun_0000000000000008",
    }
    for scope in ("requested_parameters", "effective_parameters"):
        context["selection"][scope] = {
            "temperature": 0.7,
            "top_p": 0.95,
            "max_output_tokens": 8192,
            "seed": 42,
            "reasoning_effort": "high",
            "service_tier": "priority",
            "provenance": [
                {"parameter": name, "source": "request"}
                for name in (
                    "temperature",
                    "top_p",
                    "max_output_tokens",
                    "seed",
                    "reasoning_effort",
                    "service_tier",
                )
            ],
        }
    data["performance"].update(
        total_elapsed_ms=1200,
        provider_elapsed_ms=1100,
        dispatch_elapsed_ms=1150,
        ttft_ms=100,
        generation_elapsed_ms=1000,
        first_generated_at=datetime.now(UTC),
        last_generated_at=datetime.now(UTC),
    )
    for key, value in (("input_tokens", 1000), ("output_tokens", 150), ("cache_read_tokens", 500)):
        data["usage"][key] = {"value": value, "status": "reported", "source": "provider"}
    envelope = UsageAttemptFinalized.model_validate(data)
    assert len(envelope.model_dump_json(exclude_none=True, exclude_defaults=True).encode()) <= 4096

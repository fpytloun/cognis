"""Late video evidence settles one durable Usage fact without billing its reservation."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from cognis.core.video import VideoService
from cognis.models.usage import NormalizedUsage
from cognis.models.video import VideoRemoteResult
from cognis.store.models import VideoAttemptRow, VideoJobRow
from cognis.store.usage.codec import terminal_event
from cognis.store.usage.models import UsageAttemptRow, UsageValuationRow
from tests.usage_storage_harness import usage_storage as usage_storage


@pytest.mark.parametrize("reported_cost", [None, Decimal("0"), Decimal("0.123456")])
async def test_video_settlement_waits_for_capture_and_is_idempotent(usage_storage, reported_cost):
    storage = usage_storage
    async with storage.engine.begin() as connection:
        await connection.run_sync(VideoJobRow.__table__.create, checkfirst=True)
        await connection.run_sync(VideoAttemptRow.__table__.create, checkfirst=True)
    event = storage.event()
    event = event.model_copy(
        update={
            "context": event.context.model_copy(
                update={
                    "selection": event.context.selection.model_copy(
                        update={"operation": "video_generate"}
                    ),
                }
            ),
            "usage": NormalizedUsage(
                semantic_profile="unknown",
                semantic_version=1,
                cache_read_semantics="unknown",
                cache_write_semantics="unknown",
                reasoning_semantics="unknown",
                extras=(
                    {
                        "name": "output_video_duration_ms",
                        "value": 8000,
                        "unit": "milliseconds",
                        "source": "provider",
                    },
                ),
            ),
        }
    )
    async with storage.sessions() as session:
        session.add(
            VideoJobRow(
                job_id="video-settlement",
                owner_email="owner@test",
                agent_id="agent",
                conversation_id="conversation",
                idempotency_key="settlement",
                request_hash="request-hash",
                request_artifact_id="request-artifact",
                operation="generate",
                provider_id=event.context.selection.provider_id,
                model=event.context.selection.effective_model,
                binding={
                    "usage_origin": {"provenance": "controller", "conversation_id": "conversation"}
                },
                status="succeeded",
                attempt_id=event.context.trace.request_id,
                estimated_microusd=2_000_000,
                budget_day="2026-01-01",
            )
        )
        await session.flush()
        session.add(
            VideoAttemptRow(
                attempt_id=event.context.trace.request_id,
                job_id="video-settlement",
                dispatched=True,
                result=VideoRemoteResult(
                    state="completed",
                    handle="remote-handle",
                    cost_usd=reported_cost,
                ).model_dump(mode="json"),
            )
        )
        await session.commit()
    if reported_cost == Decimal("0.123456"):
        async with storage.sessions.begin() as session:
            for index in range(32):
                missing = f"missing-capture-{index}"
                session.add(
                    VideoJobRow(
                        job_id=missing,
                        owner_email="owner@test",
                        agent_id="agent",
                        conversation_id="conversation",
                        idempotency_key=missing,
                        request_hash="request-hash",
                        request_artifact_id="request-artifact",
                        operation="generate",
                        provider_id=event.context.selection.provider_id,
                        model=event.context.selection.effective_model,
                        binding={"usage_origin": {"provenance": "controller"}},
                        status="succeeded",
                        attempt_id=missing,
                        estimated_microusd=2_000_000,
                        budget_day="2026-01-01",
                        updated_at=storage.clock.now - timedelta(days=1),
                    )
                )
            await session.flush()
            session.add_all(
                [
                    VideoAttemptRow(
                        attempt_id=f"missing-capture-{index}",
                        job_id=f"missing-capture-{index}",
                        dispatched=True,
                        result=VideoRemoteResult(
                            state="completed",
                            handle=f"missing-handle-{index}",
                            cost_usd=Decimal("0.1"),
                        ).model_dump(mode="json"),
                    )
                    for index in range(32)
                ]
            )
    service = VideoService(
        session_factory=storage.sessions,
        artifacts=None,
        llm=None,
        usage_maintenance=storage.maintenance,
    )
    await service.reconcile_usage()
    async with storage.sessions() as session:
        job = await session.get(VideoJobRow, "video-settlement")
        assert job.binding.get("usage_settled", False) == (reported_cost is None)
    storage.writer.try_emit(event)
    await storage.pump()
    await service.reconcile_usage()
    await storage.pump()
    await service.reconcile_usage()
    async with storage.sessions() as session:
        fact = await session.scalar(
            select(UsageAttemptRow).where(
                UsageAttemptRow.attempt_id == event.context.trace.attempt_id
            )
        )
        values = list(await session.scalars(select(UsageValuationRow)))
        assert len(values) == 1
        value = values[0]
        assert value.historical_cost == reported_cost
        assert value.current_cost == reported_cost
        assert value.historical_status == ("unpriced" if reported_cost is None else "complete")
        assert value.basis == (None if reported_cost is None else "provider_reported")
        assert fact.fact_version == (1 if reported_cost is None else 2)
        assert terminal_event(fact).usage.extras[0].value == 8000
    await storage.maintenance.publish_price(storage.price("new-tariff", multiplier=100))
    await storage.pump()
    async with storage.sessions() as session:
        value = (await session.scalars(select(UsageValuationRow))).one()
        assert value.historical_cost == reported_cost and value.current_cost == reported_cost
    await service.store.record_result(
        event.context.trace.request_id,
        VideoRemoteResult(
            handle="remote-handle",
            state="completed",
            cost_usd=Decimal("0.25"),
            output_duration_ms=8000,
        ),
    )
    async with storage.sessions() as session:
        job = await session.get(VideoJobRow, "video-settlement")
        assert job.binding["usage_settled"] is False
    await service.reconcile_usage()
    await storage.pump()
    await service.reconcile_usage()
    async with storage.sessions() as session:
        value = (await session.scalars(select(UsageValuationRow))).one()
        assert value.historical_cost == Decimal("0.25") and value.current_cost == Decimal("0.25")
        fact = await session.scalar(
            select(UsageAttemptRow).where(
                UsageAttemptRow.attempt_id == event.context.trace.attempt_id
            )
        )
        assert fact.fact_version == (2 if reported_cost is None else 3)
    for partial, expected_cost, expected_duration in [
        ({"output_duration_ms": 10000}, Decimal("0.25"), 10000),
        ({"cost_usd": Decimal("0.5")}, Decimal("0.5"), 10000),
    ]:
        await service.store.record_result(
            event.context.trace.request_id,
            VideoRemoteResult(handle="remote-handle", state="completed", **partial),
        )
        await service.reconcile_usage()
        await storage.pump()
        async with storage.sessions() as session:
            value = (await session.scalars(select(UsageValuationRow))).one()
            assert value.historical_cost == expected_cost
            fact = await session.scalar(
                select(UsageAttemptRow).where(
                    UsageAttemptRow.attempt_id == event.context.trace.attempt_id
                )
            )
            assert terminal_event(fact).usage.extras[0].value == expected_duration

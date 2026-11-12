"""Projection is independently recoverable and never part of raw capture commit."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.core.usage.projector import UsageProjector
from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import UsageAttemptFinalized
from cognis.store.database import create_engine
from cognis.store.usage.models import (
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageProjectionOutboxRow,
    UsageValuationRow,
)
from cognis.store.usage.schema import ensure_usage_schema
from tests.usage_fixtures import finalized_attempt


@pytest.fixture
async def projected_storage(tmp_path: Any) -> Any:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'projection.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(ensure_usage_schema)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    projector = UsageProjector(sessions, controller_owner_id="projector")
    writer = UsageAccountingService(
        sessions, controller_owner_id="writer", projection_waker=projector.wake
    )
    await writer.start()
    yield sessions, writer, projector
    await writer.aclose()
    await projector.aclose()
    await engine.dispose()


def final_event() -> UsageAttemptFinalized:
    data = finalized_attempt().model_dump()
    data["context"]["started_at"] = datetime.now(UTC)
    data["usage"]["input_tokens"] = {"value": 1000, "status": "reported", "source": "provider"}
    data["usage"]["output_tokens"] = {"value": 100, "status": "reported", "source": "provider"}
    return UsageAttemptFinalized.model_validate(data)


@pytest.mark.asyncio
async def test_raw_commit_then_projector_exactly_once(projected_storage: Any) -> None:
    sessions, writer, projector = projected_storage
    observation = final_event()
    assert writer.try_emit(observation)
    await writer.drain()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(UsageDailyTotalRow)) == 0
    assert await projector.process_page()
    assert not await projector.process_page()
    async with sessions() as session:
        attempt = await session.scalar(select(UsageAttemptRow))
        assert attempt.projected_version == 1
        total = await session.scalar(select(UsageDailyTotalRow))
        assert total.finalized_count == 1
        assert total.input_tokens == 1000
        assert total.output_tokens == 100
        assert total.historical_priced_count == 0
        receipt = await session.scalar(select(UsageValuationRow))
        assert receipt.historical_status == "unpriced"
        outbox = await session.scalar(select(UsageProjectionOutboxRow))
        assert outbox.completed_at is not None
    assert writer.try_emit(observation)
    await writer.drain()
    assert not await projector.process_page()


@pytest.mark.asyncio
async def test_startup_catchup_without_recurring_work(projected_storage: Any) -> None:
    sessions, writer, projector = projected_storage
    assert writer.try_emit(final_event())
    await writer.drain()
    await projector.start()
    assert await projector.drain()
    async with sessions() as session:
        total = await session.scalar(select(UsageDailyTotalRow))
        assert total.finalized_count == 1

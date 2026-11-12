"""Task-owned SQLite/PostgreSQL accounting harness, no application/provider I/O."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from cognis.core.usage.maintenance import UsageMaintenanceService, UsageRetentionPolicy
from cognis.core.usage.projector import (
    TIMEOUT_BACKOFF_SECONDS,
    UsagePendingTransactionTimeout,
    UsageProjector,
)
from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import UsageAttemptFinalized, UsagePriceDefinition, UsagePriceRate
from cognis.store.database import create_engine
from cognis.store.usage.schema import ensure_usage_schema
from tests.usage_fixtures import finalized_attempt


@dataclass
class StorageClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class UsageStorageHarness:
    engine: AsyncEngine
    sessions: async_sessionmaker
    writer: UsageAccountingService
    projector: UsageProjector
    maintenance: UsageMaintenanceService
    clock: StorageClock

    async def pump(self, *, pages: int = 128) -> None:
        await self.writer.drain()
        for _ in range(pages):
            maintained = await _with_page_retries(self.maintenance.process_page)
            projected = await _with_page_retries(self.projector.process_page)
            if not maintained and not projected:
                return
        raise AssertionError("Accounting work exceeded the bounded test page allowance")

    def event(
        self, number: int = 1, *, pin: str | None = None, proxy: str | None = None
    ) -> UsageAttemptFinalized:
        data = finalized_attempt().model_dump()
        data["context"]["started_at"] = self.clock.now
        data["context"]["trace"].update(attempt_id=f"ua_{number}", request_id=f"ur_{number}")
        data["context"]["selection"]["captured_price_revision_id"] = pin
        data["context"]["selection"]["service_tier"] = "default"
        data["context"]["selection"]["effective_parameters"] = {"service_tier": "default"}
        if proxy is not None:
            data["context"]["attribution"]["client_labels"] = {"session_id": proxy}
        data["observation_id"] = f"uo_{number}"
        data["finalized_at"] = self.clock.now
        data["usage"].update(
            completeness="reported",
            cache_read_semantics="included_in_input",
            cache_write_semantics="included_in_input",
            reasoning_semantics="included_in_output",
        )
        for name, value in (
            ("input_tokens", 1000),
            ("output_tokens", 100),
            ("cache_read_tokens", 0),
            ("cache_write_tokens", 0),
            ("reasoning_tokens", 0),
        ):
            data["usage"][name] = {"value": value, "status": "reported", "source": "provider"}
        return UsageAttemptFinalized.model_validate(data)

    def price(self, revision: str = "price_1", *, multiplier: int = 1) -> UsagePriceDefinition:
        event = self.event()
        return UsagePriceDefinition(
            revision_id=revision,
            provider_id=event.context.selection.provider_id,
            model=event.context.selection.effective_model,
            effective_at=self.clock.now - timedelta(days=100),
            currency="USD",
            basis="configured_estimate",
            provenance="configured",
            recorded_at=self.clock.now,
            rates=(
                UsagePriceRate(
                    dimension="input", per_million=Decimal(2 * multiplier), service_tier="standard"
                ),
                UsagePriceRate(
                    dimension="output", per_million=Decimal(4 * multiplier), service_tier="standard"
                ),
            ),
        )


async def _with_page_retries(process_page: Any) -> bool:
    """Retry a page deadline the same bounded number of times as the projector loop."""
    for delay in TIMEOUT_BACKOFF_SECONDS:
        try:
            return await process_page()
        except UsagePendingTransactionTimeout:
            await asyncio.sleep(delay)
    return await process_page()


@pytest.fixture(params=["sqlite", "postgres"])
async def usage_storage(request: Any, tmp_path: Any) -> Any:
    admin = None
    schema = None
    if request.param == "postgres":
        dsn = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not dsn:
            pytest.skip("COGNIS_TEST_POSTGRES_URL is required for real PostgreSQL qualification")
        schema = f"cognis_storage_{uuid.uuid4().hex}"
        admin = create_async_engine(dsn)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(dsn, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage-storage.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(ensure_usage_schema)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = StorageClock(datetime.now(UTC))
    projector = UsageProjector(sessions, controller_owner_id="storage_test")
    maintenance = UsageMaintenanceService(
        sessions,
        projector=projector,
        controller_owner_id="storage_test",
        retention=UsageRetentionPolicy(enabled=True),
    )
    projector.attach_maintenance(maintenance)
    writer = UsageAccountingService(
        sessions,
        controller_owner_id="storage_test",
        projection_waker=projector.wake,
        clock=clock,
    )
    await writer.start()
    harness = UsageStorageHarness(engine, sessions, writer, projector, maintenance, clock)
    try:
        yield harness
    finally:
        await writer.aclose()
        await projector.aclose()
        await engine.dispose()
        if admin is not None and schema is not None:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()

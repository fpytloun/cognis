from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.core.events import EventBus
from cognis.core.scheduler import Scheduler
from cognis.store.coordination import DatabaseLeaseStore
from cognis.store.database import create_session_factory
from cognis.store.models import Base, ScheduleFireRow, Task
from cognis.store.queries import create_agent, create_schedule, create_task, create_user

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


def _url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://")
        else url
    )


class _Queue:
    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def submit(self, **kwargs: Any) -> Task:
        async with self.factory() as session:
            task = await create_task(
                session,
                task_id=kwargs["task_id"],
                created_by=kwargs["created_by"],
                agent_id=kwargs["agent_id"],
                title=kwargs["title"],
                source_type=kwargs["source_type"],
                source_ref=kwargs["source_ref"],
                status="ready",
                scheduled_for=kwargs["scheduled_for"],
            )
            await session.commit()
            return task


@pytest.mark.asyncio
async def test_postgres_schedule_contention_and_channel_lease_takeover() -> None:
    url = _url()
    schema_name = f"cognis_stage2_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        fire_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        async with factory() as session:
            await create_user(
                session,
                email="owner@example.com",
                name="Owner",
                password_hash="x",
            )
            await create_agent(
                session,
                agent_id="agent-1",
                owner_email="owner@example.com",
                name="Agent",
                status="active",
            )
            await create_schedule(
                session,
                schedule_id="schedule-1",
                name="HA schedule",
                schedule_type="interval",
                interval_seconds=60,
                agent_id="agent-1",
                task_template={"title": "Scheduled"},
                next_fire_at=fire_at,
                created_by="owner@example.com",
            )
            await session.commit()

        queue = _Queue(factory)
        schedulers = [
            Scheduler(
                factory,
                queue,
                EventBus(),
                controller_owner_id=f"controller-{index}",
            )
            for index in (1, 2)
        ]
        results = await asyncio.gather(
            *(scheduler._fire_schedule("schedule-1") for scheduler in schedulers)  # noqa: SLF001
        )
        assert len({item for item in results if item is not None}) == 1
        async with factory() as session:
            assert len((await session.execute(Task.__table__.select())).all()) == 1
            assert len((await session.execute(ScheduleFireRow.__table__.select())).all()) == 1

        leases = DatabaseLeaseStore(factory)
        old = await leases.acquire("channel-account:account-1", "controller-1", ttl_seconds=0.1)
        assert old is not None
        assert (
            await leases.acquire("channel-account:account-1", "controller-2", ttl_seconds=60)
            is None
        )
        await asyncio.sleep(0.2)
        newer = await leases.acquire("channel-account:account-1", "controller-2", ttl_seconds=60)
        assert newer is not None
        assert newer.fencing_token > old.fencing_token
        assert await leases.release(old) is False
        assert await leases.renew(newer, ttl_seconds=60) is not None
        assert await leases.revoke("channel-account:account-1")
        assert not await leases.is_current(newer)
        newest = await leases.acquire("channel-account:account-1", "controller-3", ttl_seconds=60)
        assert newest is not None and newest.fencing_token > newer.fencing_token
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()

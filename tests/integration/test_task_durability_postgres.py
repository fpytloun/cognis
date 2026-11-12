from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.core.events import EventBus
from cognis.core.scheduler import Scheduler
from cognis.core.task_execution import TaskExecutionFence, TaskExecutionStore
from cognis.core.task_queue import TaskQueue
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.models.workflow import Workflow
from cognis.store.database import create_session_factory
from cognis.store.models import Base, Schedule, ScheduleFireRow, Task
from cognis.store.queries import (
    create_agent,
    create_schedule,
    create_task,
    create_user,
    create_workflow,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


@pytest_asyncio.fixture
async def database():
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    schema = f"durability_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as db:
        await db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": f'"{schema}"'}}
    )
    try:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        async with factory() as db:
            await create_user(
                db, email="durability@example.org", name="Durability", password_hash="test-hash"
            )
            await create_agent(
                db,
                agent_id="durability-agent",
                name="Durability",
                owner_email="durability@example.org",
            )
            await create_workflow(
                db,
                workflow_id="durability-workflow",
                name="Durability",
                definition={"steps": [{"name": "run", "type": "run", "prompt": "Test"}]},
                owner_email="durability@example.org",
            )
            await db.commit()
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


class _Queue:
    def __init__(self, factory):
        self.factory = factory
        self._workflow_registry = SimpleNamespace(
            get=lambda _: Workflow(workflow_id="durability-workflow", name="Durability", steps=[])
        )

    async def submit(self, **arguments):
        async with self.factory() as db:
            await create_task(
                db,
                task_id=arguments["task_id"],
                title=arguments["title"],
                agent_id=arguments["agent_id"],
                status=arguments["status"],
                workflow_id=arguments["workflow_id"],
                created_by="durability@example.org",
                source_type=arguments["source_type"],
                source_ref=arguments["source_ref"],
                scheduled_for=arguments["scheduled_for"],
            )
            await db.commit()
        return SimpleNamespace(task_id=arguments["task_id"])

    async def is_ready(self, task_id):
        async with self.factory() as db:
            return (await db.get(Task, task_id)) is not None


@pytest.mark.asyncio
async def test_postgres_stale_ha_selection_cannot_dispatch_or_skip_tomorrow(database):
    fire_at = datetime.now(UTC) - timedelta(seconds=2)
    async with database() as db:
        await create_schedule(
            db,
            schedule_id="durability-schedule",
            name="Durability",
            agent_id="durability-agent",
            workflow_id="durability-workflow",
            schedule_type="interval",
            interval_seconds=86400,
            next_fire_at=fire_at,
            created_by="durability@example.org",
            task_template={"title": "Durability"},
        )
        await db.commit()
    workers = [
        Scheduler(database, _Queue(database), EventBus(), controller_owner_id=uuid4().hex)
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(
            worker._fire_schedule("durability-schedule", expected_fire_at=fire_at)
            for worker in workers
        )
    )
    assert sum(result is not None for result in results) == 1
    assert await workers[1]._fire_schedule("durability-schedule", expected_fire_at=fire_at) is None
    assert await workers[1]._fire_schedule("durability-schedule") is None
    async with database() as db:
        assert len((await db.scalars(select(Task))).all()) == 1
        assert len((await db.scalars(select(ScheduleFireRow))).all()) == 1
        schedule = await db.get(Schedule, "durability-schedule")
        assert schedule.next_fire_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_postgres_takeover_claim_does_not_require_workflow_capacity(database):
    async with database() as db:
        for task_id, status in (("occupied", "ready"), ("interrupted", "running")):
            await create_task(
                db,
                task_id=task_id,
                title=task_id,
                agent_id="durability-agent",
                status=status,
                created_by="durability@example.org",
            )
        await db.commit()
    store = TaskExecutionStore(
        database, owner_id=uuid4().hex, max_active_global=1, max_active_per_agent=1
    )
    occupied = await store.claim_ready()
    assert occupied is not None and occupied.has_capacity
    takeover = await store.claim_existing("interrupted", statuses={"running"}, capacity=False)
    assert takeover is not None and not takeover.has_capacity
    assert takeover.attempt_number == 1
    await TaskExecutionFence(store, occupied, asyncio.Event()).assert_current()
    await TaskExecutionFence(store, takeover, asyncio.Event()).assert_current()
    await store.release(takeover)
    await store.release(occupied)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "classification",
    ["safe_before_tool_dispatch", "infrastructure_wait", "recoverable_pending_tool_calls"],
)
async def test_postgres_recovery_requeues_same_attempt_using_real_database(
    database, monkeypatch, classification
):
    async with database() as db:
        await create_task(
            db,
            task_id="recoverable",
            title="Recoverable",
            agent_id="durability-agent",
            status="running",
            created_by="durability@example.org",
        )
        await db.commit()
    queue = TaskQueue(
        session_factory=database,
        workflow_engine=AsyncMock(),
        workflow_registry=WorkflowRegistry(database),
        event_bus=EventBus(),
    )
    monkeypatch.setattr(
        queue, "_classify_crash_recovery", AsyncMock(return_value=(classification, {}))
    )
    await queue.recover_stale_tasks()
    async with database() as db:
        task = await db.get(Task, "recoverable")
        assert task.status == "ready"
        assert task.attempt_number == 1
        assert task.completed_at is None
        if classification == "infrastructure_wait":
            assert (
                datetime.now(UTC) < task.scheduled_for < datetime.now(UTC) + timedelta(seconds=40)
            )
        else:
            assert task.scheduled_for <= datetime.now(UTC)

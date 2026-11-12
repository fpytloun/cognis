"""Expired resource ranking fails explicitly on both supported databases."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from cognis.core.usage.maintenance import UsageMaintenanceScope
from cognis.core.usage.query_models import UsageQuery
from cognis.core.usage.query_service import create_usage_query_service
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.models.usage import UsageAttemptFinalized
from cognis.store.models import Base, Schedule, ScheduleFireRow
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_task,
    create_user,
)
from cognis.store.usage.models import UsageAttemptRow

pytest_plugins = ("tests.usage_storage_harness",)
pytestmark = pytest.mark.asyncio


async def test_expired_task_schedule_and_conversation_rankings_are_rejected(usage_storage):
    h = usage_storage
    actor = "usage+test@cognis.test"
    h.clock.now -= timedelta(days=40)
    start = h.clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with h.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with h.sessions.begin() as session:
        await create_user(session, actor, "Usage", "")
        await create_user(session, "other@usage.test", "Other", "")
        await create_agent(
            session,
            agent_id="agent_archive",
            owner_email=actor,
            name="Archive",
            status="active",
        )
        await create_conversation(
            session,
            user_email=actor,
            agent_id="agent_archive",
            context_type="web",
            conversation_id="conv_archive",
            title="Archive conversation",
        )
        await create_session(
            session,
            user_email=actor,
            agent_id="agent_archive",
            conversation_id="conv_archive",
            session_id="session_archive",
        )
        await create_task(
            session,
            created_by=actor,
            agent_id="agent_archive",
            task_id="task_archive",
            title="Archived task",
            source_type="scheduler",
        )
        session.add(
            Schedule(
                schedule_id="schedule_archive",
                created_by=actor,
                agent_id="agent_archive",
                name="Archived schedule",
                task_template={},
            )
        )
        await session.flush()
        session.add(
            ScheduleFireRow(
                fire_id="fire_archive",
                schedule_id="schedule_archive",
                scheduled_fire_at=start,
                task_id="task_archive",
            )
        )
    await h.maintenance.publish_price(h.price())
    event = h.event(pin="price_1").model_dump()
    event["context"]["attribution"]["resources"] = {
        "provenance": "controller",
        "agent_id": "agent_archive",
        "conversation_id": "conv_archive",
        "session_id": "session_archive",
        "task_id": "task_archive",
    }
    assert h.writer.try_emit(UsageAttemptFinalized.model_validate(event))
    await h.pump()
    service = await create_usage_query_service(
        h.sessions,
        cursor_secret=b"usage-archive-test-signing-key-32",
    )
    queries = [
        UsageQuery(
            start=start,
            end=start + timedelta(days=180),
            view="ranking",
            group_by=dimension,
            bucket="day",
            metric="cost",
            filters={"schedule_ids": ["schedule_archive"], "task_origin": "scheduled"},
        )
        for dimension in ["task", "schedule", "conversation"]
    ]
    before = [await service.query(actor, query) for query in queries]
    assert all(len(result.groups) == 1 for result in before)
    async with h.sessions() as session:
        attempt = (await session.scalars(select(UsageAttemptRow))).one()
        scope = UsageMaintenanceScope(owner_key=attempt.owner_key, bucket_day=attempt.bucket_day)
    h.clock.now += timedelta(days=40)
    job = await h.maintenance.request_archive(scope)
    await h.pump()
    assert (await h.maintenance.status(job.job_id)).status == "complete"
    async with h.sessions() as session:
        assert await session.scalar(select(UsageAttemptRow)) is None
    for query in queries:
        with pytest.raises(UsageQueryError, match="not available before") as error:
            await service.query(actor, query)
        assert error.value.code == "resource_detail_expired"
        assert error.value.status == 422
        # Another actor's archive must not reveal its cutoff or fabricate readiness.
        other = await service.query("other@usage.test", query)
        assert other.total is None and other.groups == []
        assert other.meta.availability.detail_retention_cutoff is None
        assert other.meta.projection.state == "unavailable"
        assert other.ranking_measure == "known_priced_subtotal"

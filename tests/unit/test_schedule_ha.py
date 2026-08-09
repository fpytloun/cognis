from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cognis.bootstrap import run_schema_bootstrap
from cognis.core.events import Event, EventBus, EventType
from cognis.core.scheduler import Scheduler
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Schedule, ScheduleFireRow, Task
from cognis.store.queries import create_agent, create_schedule, create_task, create_user
from cognis.tools.builtin.schedule import _handle_trigger


class _DatabaseTaskQueue:
    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory
        self.before_return: asyncio.Event | None = None
        self.create_status = "ready"

    async def submit(self, **kwargs: Any) -> Task:
        status = kwargs.get("status", self.create_status)
        async with self._session_factory() as session:
            row = await create_task(
                session,
                task_id=kwargs["task_id"],
                created_by=kwargs["created_by"],
                agent_id=kwargs["agent_id"],
                title=kwargs["title"],
                description=kwargs["description"],
                expected_output=kwargs["expected_output"],
                priority=kwargs["priority"],
                source_type=kwargs["source_type"],
                source_ref=kwargs["source_ref"],
                status=status,
                scheduled_for=kwargs["scheduled_for"],
            )
            await session.commit()
        if self.before_return is not None:
            await self.before_return.wait()
        return row

    async def submit_existing(self, task_id: str) -> Task:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            assert task is not None
            if task.status == "draft":
                task.status = "ready"
                await session.commit()
            return task

    async def reconcile_submitted(self, task_id: str) -> Task | None:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is not None and task.status == "queued":
                task.status = "ready"
                await session.commit()
            return task


async def _setup(tmp_path: Any) -> tuple[Any, Any, datetime]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'schedule-ha.db'}")
    await run_schema_bootstrap(engine)
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
    return engine, factory, fire_at


@pytest.mark.asyncio
async def test_two_schedulers_dispatch_one_logical_fire(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        queue = _DatabaseTaskQueue(factory)
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
        task_ids = {task_id for task_id in results if task_id is not None}
        assert len(task_ids) == 1

        async with factory() as session:
            assert len((await session.execute(Task.__table__.select())).all()) == 1
            fires = (await session.execute(ScheduleFireRow.__table__.select())).all()
            assert len(fires) == 1
            assert fires[0].scheduled_fire_at.replace(tzinfo=UTC) == fire_at
            assert fires[0].status == "dispatched"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_fire_runs_now_without_changing_recurring_cadence(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        before = datetime.now(UTC)
        task_id = await scheduler.trigger_now("schedule-1")
        after = datetime.now(UTC)
        assert task_id is not None

        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            task = await session.get(Task, task_id)
            fire = (await session.execute(ScheduleFireRow.__table__.select())).first()
            assert schedule is not None
            assert schedule.next_fire_at.replace(tzinfo=UTC) == fire_at
            assert schedule.last_fired_at is None
            assert schedule.consecutive_errors == 0
            assert task is not None
            scheduled_for = task.scheduled_for.replace(tzinfo=UTC)
            assert before - timedelta(seconds=1) <= scheduled_for <= after
            assert fire is not None
            assert fire.fire_id.startswith("sfire_manual_")
            assert fire.status == "dispatched"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_schedulers_deduplicate_concurrent_manual_fire(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        queue = _DatabaseTaskQueue(factory)
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
            *(scheduler.trigger_now("schedule-1") for scheduler in schedulers)
        )
        assert len({task_id for task_id in results if task_id is not None}) == 1
        async with factory() as session:
            assert len((await session.execute(Task.__table__.select())).all()) == 1
            assert len((await session.execute(ScheduleFireRow.__table__.select())).all()) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_scheduler_coalesces_concurrent_manual_fire(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        queue = _DatabaseTaskQueue(factory)
        release = asyncio.Event()
        queue.before_return = release
        scheduler = Scheduler(
            factory,
            queue,
            EventBus(),
            controller_owner_id="controller-1",
        )
        first = asyncio.create_task(scheduler.trigger_now("schedule-1"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(scheduler.trigger_now("schedule-1"))
        release.set()
        results = await asyncio.gather(first, second)
        assert results[0] == results[1]
        assert results[0] is not None
        async with factory() as session:
            assert len((await session.execute(Task.__table__.select())).all()) == 1
            assert len((await session.execute(ScheduleFireRow.__table__.select())).all()) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_and_recurring_fire_at_same_second_use_separate_ledger_rows(
    tmp_path: Any,
) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        recurring_at = datetime.now(UTC).replace(microsecond=0)
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.next_fire_at = recurring_at
            schedule.max_concurrent_runs = 2
            await session.commit()
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        manual_task_id = await scheduler.trigger_now("schedule-1")
        recurring_task_id = await scheduler._fire_schedule("schedule-1")  # noqa: SLF001
        assert manual_task_id is not None
        assert recurring_task_id is not None
        assert manual_task_id != recurring_task_id
        async with factory() as session:
            fires = (await session.execute(ScheduleFireRow.__table__.select())).all()
            assert len(fires) == 2
            manual = next(fire for fire in fires if fire.fire_id.startswith("sfire_manual_"))
            recurring = next(fire for fire in fires if not fire.fire_id.startswith("sfire_manual_"))
            assert manual.scheduled_fire_at.microsecond != 0
            assert manual.scheduled_fire_at != recurring.scheduled_fire_at
            assert recurring.scheduled_fire_at.replace(tzinfo=UTC) == recurring_at
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_overdue_recurring_fire_can_share_manual_ledger_timestamp(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        async with factory() as session:
            session.add(
                ScheduleFireRow(
                    fire_id="sfire_manual_same_timestamp",
                    schedule_id="schedule-1",
                    fire_kind="manual",
                    scheduled_fire_at=fire_at,
                    status="failed",
                    attempt_count=1,
                )
            )
            await session.commit()
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        recurring_task_id = await scheduler._fire_schedule("schedule-1")  # noqa: SLF001
        assert recurring_task_id is not None
        async with factory() as session:
            fires = (await session.execute(ScheduleFireRow.__table__.select())).all()
            assert {
                (fire.fire_kind, fire.scheduled_fire_at.replace(tzinfo=UTC)) for fire in fires
            } == {("manual", fire_at), ("recurring", fire_at)}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_fire_skips_at_active_concurrency_without_changing_cadence(
    tmp_path: Any,
) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                task_id="active-task",
                created_by="owner@example.com",
                agent_id="agent-1",
                title="Active",
                description="",
                source_type="scheduler",
                source_ref="schedule-1",
                status="running",
            )
            await session.commit()
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        assert await scheduler.trigger_now("schedule-1") is None
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            fires = (await session.execute(ScheduleFireRow.__table__.select())).all()
            assert schedule is not None
            assert schedule.next_fire_at.replace(tzinfo=UTC) == fire_at
            assert schedule.last_fired_at is None
            assert len(fires) == 1
            assert fires[0].status == "skipped"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_fire_reconciles_task_after_transient_crash(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        queue = _DatabaseTaskQueue(factory)
        queue.create_status = "queued"
        scheduler = Scheduler(
            factory,
            queue,
            EventBus(),
            controller_owner_id="controller-1",
        )
        original_mark = scheduler._fire_store.settle_manual_dispatched  # noqa: SLF001
        crashed = False

        async def _crash_once(**kwargs: Any) -> bool:
            nonlocal crashed
            if not crashed:
                crashed = True
                raise asyncio.CancelledError
            return await original_mark(**kwargs)

        scheduler._fire_store.settle_manual_dispatched = _crash_once  # type: ignore[method-assign]  # noqa: SLF001
        with pytest.raises(asyncio.CancelledError):
            await scheduler.trigger_now("schedule-1")
        async with factory() as session:
            crash_fires = (await session.execute(ScheduleFireRow.__table__.select())).all()
            assert [(fire.fire_id, fire.status) for fire in crash_fires] == [
                (crash_fires[0].fire_id, "claimed")
            ]
        scheduler._fire_store.settle_manual_dispatched = original_mark  # type: ignore[method-assign]  # noqa: SLF001
        task_id = await scheduler.trigger_now("schedule-1")
        assert task_id is not None
        async with factory() as session:
            assert len((await session.execute(Task.__table__.select())).all()) == 1
            task = await session.get(Task, task_id)
            fire = (await session.execute(ScheduleFireRow.__table__.select())).first()
            assert task is not None and task.status == "ready"
            assert fire is not None and fire.status == "dispatched"
            assert fire.attempt_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_fire_settlement_failure_keeps_claim_for_retry(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        original_settle = scheduler._fire_store.settle_manual_dispatched  # noqa: SLF001
        failed = False

        async def _fail_before_settle(**kwargs: Any) -> bool:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("settlement unavailable")
            return await original_settle(**kwargs)

        scheduler._fire_store.settle_manual_dispatched = _fail_before_settle  # type: ignore[method-assign]  # noqa: SLF001
        assert await scheduler.trigger_now("schedule-1") is None
        async with factory() as session:
            fires = (await session.execute(ScheduleFireRow.__table__.select())).all()
            tasks = (await session.execute(Task.__table__.select())).all()
            assert len(fires) == 1
            assert fires[0].status == "claimed"
            assert len(tasks) == 1

        scheduler._fire_store.settle_manual_dispatched = original_settle  # type: ignore[method-assign]  # noqa: SLF001
        task_id = await scheduler.trigger_now("schedule-1")
        assert task_id is not None
        async with factory() as session:
            fires = (await session.execute(ScheduleFireRow.__table__.select())).all()
            tasks = (await session.execute(Task.__table__.select())).all()
            assert len(fires) == 1
            assert fires[0].status == "dispatched"
            assert fires[0].task_id == task_id
            assert len(tasks) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_fire_failure_preserves_schedule_health_and_cadence(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:

        class _FailingQueue:
            async def submit(self, **_kwargs: Any) -> Task:
                raise RuntimeError("dispatch failed")

        scheduler = Scheduler(
            factory,
            _FailingQueue(),
            EventBus(),
            controller_owner_id="controller-1",
        )
        assert await scheduler.trigger_now("schedule-1") is None
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            fire = (await session.execute(ScheduleFireRow.__table__.select())).first()
            assert schedule is not None
            assert schedule.enabled is True
            assert schedule.next_fire_at.replace(tzinfo=UTC) == fire_at
            assert schedule.last_fired_at is None
            assert schedule.last_run_status is None
            assert schedule.consecutive_errors == 0
            assert fire is not None and fire.status == "failed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_one_shot_preserves_legacy_consumption_semantics(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.schedule_type = "one_shot"
            schedule.interval_seconds = None
            schedule.one_shot_at = fire_at
            await session.commit()
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        task_id = await scheduler.trigger_now("schedule-1")
        assert task_id is not None
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            task = await session.get(Task, task_id)
            assert schedule is not None
            assert schedule.next_fire_at is None
            assert schedule.last_run_status == "success"
            assert schedule.last_fired_at == task.scheduled_for
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_one_shot_atomic_settlement_reconciles_after_crash(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.schedule_type = "one_shot"
            schedule.interval_seconds = None
            schedule.one_shot_at = fire_at
            await session.commit()
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        original_settle = scheduler._fire_store.settle_manual_dispatched  # noqa: SLF001
        crashed = False

        async def _settle_then_crash(**kwargs: Any) -> bool:
            nonlocal crashed
            settled = await original_settle(**kwargs)
            if not crashed:
                crashed = True
                raise asyncio.CancelledError
            return settled

        scheduler._fire_store.settle_manual_dispatched = _settle_then_crash  # type: ignore[method-assign]  # noqa: SLF001
        with pytest.raises(asyncio.CancelledError):
            await scheduler.trigger_now("schedule-1")
        scheduler._fire_store.settle_manual_dispatched = original_settle  # type: ignore[method-assign]  # noqa: SLF001
        retry_task_id = await scheduler.trigger_now("schedule-1")
        assert retry_task_id is not None
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            fires = (await session.execute(ScheduleFireRow.__table__.select())).all()
            tasks = (await session.execute(Task.__table__.select())).all()
            assert schedule is not None and schedule.next_fire_at is None
            assert len(fires) == 1
            assert fires[0].status == "dispatched"
            assert fires[0].task_id == retry_task_id
            assert len(tasks) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_one_shot_delete_is_atomic_after_task_activation(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.schedule_type = "one_shot"
            schedule.interval_seconds = None
            schedule.one_shot_at = fire_at
            schedule.delete_after_run = True
            await session.commit()
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        original_settle = scheduler._fire_store.settle_manual_dispatched  # noqa: SLF001

        async def _settle_then_crash(**kwargs: Any) -> bool:
            await original_settle(**kwargs)
            raise asyncio.CancelledError

        scheduler._fire_store.settle_manual_dispatched = _settle_then_crash  # type: ignore[method-assign]  # noqa: SLF001
        with pytest.raises(asyncio.CancelledError):
            await scheduler.trigger_now("schedule-1")
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            tasks = (await session.execute(Task.__table__.select())).all()
            assert schedule is None
            assert len(tasks) == 1
            assert tasks[0].status == "ready"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "event_type"),
    [
        ("completed", EventType.TASK_COMPLETED),
        ("failed", EventType.TASK_FAILED),
        ("cancelled", EventType.TASK_CANCELLED),
    ],
)
async def test_manual_task_terminal_event_preserves_recurring_schedule_state(
    tmp_path: Any,
    task_status: str,
    event_type: EventType,
) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
            max_consecutive_errors=1,
        )
        task_id = await scheduler.trigger_now("schedule-1")
        assert task_id is not None
        async with factory() as session:
            task = await session.get(Task, task_id)
            schedule = await session.get(Schedule, "schedule-1")
            assert task is not None and schedule is not None
            task.status = task_status
            schedule.last_run_status = "baseline"
            schedule.consecutive_errors = 3
            await session.commit()

        await scheduler._handle_task_terminal_event(  # noqa: SLF001
            Event(type=event_type, data={"task_id": task_id})
        )
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            assert schedule.enabled is True
            assert schedule.next_fire_at.replace(tzinfo=UTC) == fire_at
            assert schedule.last_fired_at is None
            assert schedule.last_run_status == "baseline"
            assert schedule.consecutive_errors == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "event_type"),
    [
        ("completed", EventType.TASK_COMPLETED),
        ("failed", EventType.TASK_FAILED),
        ("cancelled", EventType.TASK_CANCELLED),
    ],
)
async def test_manual_terminal_event_before_dispatch_settlement_is_identified(
    tmp_path: Any,
    task_status: str,
    event_type: EventType,
) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
            max_consecutive_errors=1,
        )

        async def _observe_pre_settlement(*, claim: Any, **_kwargs: Any) -> bool:
            assert await scheduler._fire_store.is_manual_task(claim.task_id)  # noqa: SLF001
            async with factory() as session:
                task = await session.get(Task, claim.task_id)
                schedule = await session.get(Schedule, "schedule-1")
                assert task is not None and schedule is not None
                task.status = task_status
                schedule.last_run_status = "baseline"
                schedule.consecutive_errors = 3
                await session.commit()
            await scheduler._handle_task_terminal_event(  # noqa: SLF001
                Event(type=event_type, data={"task_id": claim.task_id})
            )
            return False

        scheduler._fire_store.settle_manual_dispatched = _observe_pre_settlement  # type: ignore[method-assign]  # noqa: SLF001
        assert await scheduler.trigger_now("schedule-1") is None
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            assert schedule.enabled is True
            assert schedule.next_fire_at.replace(tzinfo=UTC) == fire_at
            assert schedule.last_fired_at is None
            assert schedule.last_run_status == "baseline"
            assert schedule.consecutive_errors == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_builtin_manual_trigger_uses_public_trigger_now(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:

        class _Scheduler:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def trigger_now(self, schedule_id: str) -> str:
                self.calls.append(schedule_id)
                return "task_sched_public"

            async def _fire_schedule(self, _schedule_id: str) -> None:
                raise AssertionError("builtin manual trigger used _fire_schedule")

        scheduler = _Scheduler()
        result = await _handle_trigger(
            factory,
            scheduler,
            "owner@example.com",
            {"schedule_id": "schedule-1"},
        )
        assert result.is_error is False
        assert scheduler.calls == ["schedule-1"]
        assert "task_sched_public" in result.output
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claimed_fire_reconciles_task_created_before_settle(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        queue = _DatabaseTaskQueue(factory)
        queue.create_status = "queued"
        scheduler = Scheduler(
            factory,
            queue,
            EventBus(),
            controller_owner_id="controller-1",
        )
        original_mark = scheduler._fire_store.mark_dispatched  # noqa: SLF001
        crashed = False

        async def _crash_once(**kwargs: Any) -> bool:
            nonlocal crashed
            if not crashed:
                crashed = True
                raise asyncio.CancelledError
            return await original_mark(**kwargs)

        scheduler._fire_store.mark_dispatched = _crash_once  # type: ignore[method-assign]  # noqa: SLF001
        with pytest.raises(asyncio.CancelledError):
            await scheduler._fire_schedule("schedule-1")  # noqa: SLF001
        scheduler._fire_store.mark_dispatched = original_mark  # type: ignore[method-assign]  # noqa: SLF001
        task_id = await scheduler._fire_schedule("schedule-1")  # noqa: SLF001
        assert task_id is not None

        async with factory() as session:
            assert len((await session.execute(Task.__table__.select())).all()) == 1
            task = await session.get(Task, task_id)
            assert task is not None and task.status == "ready"
            fire = (await session.execute(ScheduleFireRow.__table__.select())).first()
            assert fire is not None
            assert fire.status == "dispatched"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_failure_records_backoff_instead_of_hot_loop(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:

        class _FailingQueue:
            async def submit(self, **_kwargs: Any) -> Task:
                raise RuntimeError("dispatch failed")

        scheduler = Scheduler(
            factory,
            _FailingQueue(),
            EventBus(),
            controller_owner_id="controller-1",
        )
        assert await scheduler._fire_schedule("schedule-1") is None  # noqa: SLF001
        async with factory() as session:
            from cognis.store.models import Schedule

            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            assert schedule.consecutive_errors == 1
            assert schedule.last_run_status == "failed"
            assert schedule.next_fire_at is not None
            assert schedule.next_fire_at.replace(tzinfo=UTC) > fire_at
            fire = (await session.execute(ScheduleFireRow.__table__.select())).first()
            assert fire is not None and fire.status == "failed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_post_dispatch_advance_failure_reconciles_without_marking_fire_failed(
    tmp_path: Any,
) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        original_advance = scheduler._fire_store.advance_schedule  # noqa: SLF001
        failed = False

        async def _fail_once(**kwargs: Any) -> bool:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("advance failed")
            return await original_advance(**kwargs)

        scheduler._fire_store.advance_schedule = _fail_once  # type: ignore[method-assign]  # noqa: SLF001
        assert await scheduler._fire_schedule("schedule-1") is None  # noqa: SLF001
        async with factory() as session:
            fire = (await session.execute(ScheduleFireRow.__table__.select())).first()
            assert fire is not None and fire.status == "dispatched"
        scheduler._fire_store.advance_schedule = original_advance  # type: ignore[method-assign]  # noqa: SLF001
        assert await scheduler._fire_schedule("schedule-1") is not None  # noqa: SLF001
        async with factory() as session:
            from cognis.store.models import Schedule

            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            assert schedule.consecutive_errors == 0
            assert schedule.last_run_status == "success"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_shot_advances_to_none_while_task_is_still_running(tmp_path: Any) -> None:
    engine, factory, fire_at = await _setup(tmp_path)
    try:
        from cognis.store.models import Schedule

        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.schedule_type = "one_shot"
            schedule.interval_seconds = None
            schedule.one_shot_at = fire_at
            await session.commit()
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        assert await scheduler._fire_schedule("schedule-1") is not None  # noqa: SLF001
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            assert schedule.next_fire_at is None
            assert len((await session.execute(Task.__table__.select())).all()) == 1
        async with factory() as session:
            from cognis.store.queries import list_due_schedules

            assert await list_due_schedules(session, datetime.now(UTC)) == []
            assert len((await session.execute(Task.__table__.select())).all()) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schedule_type", "cron_expr", "interval_seconds"),
    [
        ("interval", None, 60),
        ("cron", "* * * * *", None),
    ],
)
async def test_months_overdue_catchup_is_bounded_and_jumps_future(
    tmp_path: Any,
    schedule_type: str,
    cron_expr: str | None,
    interval_seconds: int | None,
) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        from cognis.store.models import Schedule

        historical = datetime(2025, 1, 1, tzinfo=UTC)
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.schedule_type = schedule_type
            schedule.cron_expr = cron_expr
            schedule.interval_seconds = interval_seconds
            schedule.next_fire_at = historical
            schedule.max_concurrent_runs = 100
            await session.commit()
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
            max_missed_on_startup=3,
            missed_stagger_seconds=0,
        )
        before = datetime.now(UTC)
        await scheduler._catch_up_missed()  # noqa: SLF001
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            assert schedule.next_fire_at is not None
            assert schedule.next_fire_at.replace(tzinfo=UTC) > before
            assert len((await session.execute(Task.__table__.select())).all()) == 3
            assert len((await session.execute(ScheduleFireRow.__table__.select())).all()) == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_scheduler_catchup_uses_one_cluster_budget(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        from cognis.store.models import Schedule

        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.next_fire_at = datetime(2025, 1, 1, tzinfo=UTC)
            schedule.max_concurrent_runs = 100
            await session.commit()
        queue = _DatabaseTaskQueue(factory)
        schedulers = [
            Scheduler(
                factory,
                queue,
                EventBus(),
                controller_owner_id=f"controller-{index}",
                max_missed_on_startup=2,
                missed_stagger_seconds=0,
            )
            for index in (1, 2)
        ]
        await asyncio.gather(*(scheduler._catch_up_missed() for scheduler in schedulers))  # noqa: SLF001
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None and schedule.next_fire_at is not None
            assert schedule.next_fire_at.replace(tzinfo=UTC) > datetime.now(UTC)
            assert len((await session.execute(Task.__table__.select())).all()) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catchup_budget_survives_lease_owner_loss(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        first = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
            max_missed_on_startup=3,
        )
        second = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-2",
            max_missed_on_startup=3,
        )
        cutoff = datetime.now(UTC)
        first_lease = await first._lease_store.acquire(  # noqa: SLF001
            "scheduler:startup-catchup",
            "controller-1",
            ttl_seconds=60,
        )
        assert first_lease is not None
        prepared = await first._fire_store.prepare_catchup(  # noqa: SLF001
            lease=first_lease,
            cutoff_at=cutoff,
            budget=3,
        )
        assert prepared is not None and prepared[1] == 3
        assert await first._fire_store.reserve_catchup_fire(  # noqa: SLF001
            lease=first_lease
        )
        assert await first._fire_store.reserve_catchup_fire(  # noqa: SLF001
            lease=first_lease
        )
        assert await first._lease_store.revoke("scheduler:startup-catchup")  # noqa: SLF001
        second_lease = await second._lease_store.acquire(  # noqa: SLF001
            "scheduler:startup-catchup",
            "controller-2",
            ttl_seconds=60,
        )
        assert second_lease is not None
        resumed = await second._fire_store.prepare_catchup(  # noqa: SLF001
            lease=second_lease,
            cutoff_at=datetime.now(UTC),
            budget=3,
        )
        assert resumed is not None and resumed[1] == 1
        assert await second._fire_store.reserve_catchup_fire(  # noqa: SLF001
            lease=second_lease
        )
        assert (
            await second._fire_store.reserve_catchup_fire(lease=second_lease)  # noqa: SLF001
            is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_running_scheduler_tick_defers_while_cluster_catchup_is_active(
    tmp_path: Any,
) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        leader = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
        )
        follower = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-2",
            max_missed_on_startup=3,
            missed_stagger_seconds=0,
        )
        from cognis.store.models import Schedule

        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.next_fire_at = datetime(2025, 1, 1, tzinfo=UTC)
            schedule.max_concurrent_runs = 100
            await session.commit()
        lease = await leader._lease_store.acquire(  # noqa: SLF001
            "scheduler:startup-catchup",
            "controller-1",
            ttl_seconds=2,
        )
        assert lease is not None
        assert await leader._fire_store.prepare_catchup(  # noqa: SLF001
            lease=lease,
            cutoff_at=datetime.now(UTC),
            budget=3,
        )
        tick = asyncio.create_task(follower._tick())  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert not tick.done()
        await asyncio.wait_for(tick, timeout=5)
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None and schedule.next_fire_at is not None
            assert schedule.next_fire_at.replace(tzinfo=UTC) > datetime.now(UTC)
            assert len((await session.execute(Task.__table__.select())).all()) == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catchup_waits_for_contended_schedule_before_completion(tmp_path: Any) -> None:
    engine, factory, _ = await _setup(tmp_path)
    try:
        from cognis.store.models import Schedule

        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None
            schedule.next_fire_at = datetime(2025, 1, 1, tzinfo=UTC)
            schedule.max_concurrent_runs = 100
            await session.commit()
        blocker = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="blocker",
        )
        scheduler = Scheduler(
            factory,
            _DatabaseTaskQueue(factory),
            EventBus(),
            controller_owner_id="controller-1",
            max_missed_on_startup=2,
            missed_stagger_seconds=0,
        )
        held = await blocker._lease_store.acquire(  # noqa: SLF001
            "schedule:schedule-1",
            "blocker",
            ttl_seconds=60,
        )
        assert held is not None
        catchup = asyncio.create_task(scheduler._catch_up_missed())  # noqa: SLF001
        await asyncio.sleep(0.1)
        assert not catchup.done()
        assert await scheduler._fire_store.catchup_active()  # noqa: SLF001
        await blocker._lease_store.release(held)  # noqa: SLF001
        await asyncio.wait_for(catchup, timeout=5)
        assert not await scheduler._fire_store.catchup_active()  # noqa: SLF001
        async with factory() as session:
            schedule = await session.get(Schedule, "schedule-1")
            assert schedule is not None and schedule.next_fire_at is not None
            assert schedule.next_fire_at.replace(tzinfo=UTC) > datetime.now(UTC)
            assert len((await session.execute(Task.__table__.select())).all()) == 2
    finally:
        await engine.dispose()

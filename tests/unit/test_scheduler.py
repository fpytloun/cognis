"""Unit tests for the Scheduler engine and schedule domain models."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.events import Event, EventBus, EventType
from cognis.core.scheduler import Scheduler
from cognis.models.schedule import (
    ScheduleModel,
    ScheduleType,
    describe_cron,
    describe_interval,
    describe_schedule,
)
from cognis.models.task import TaskDelivery
from cognis.tools.builtin import schedule as schedule_mod
from cognis.tools.introspection import validate_available_tool_call_with_context
from cognis.tools.native_validation import NativeValidationContext

# ---------------------------------------------------------------------------
# Schedule model validation
# ---------------------------------------------------------------------------


class TestScheduleModelValidation:
    """Test ScheduleModel field validation."""

    def test_declared_interval_schema_enforces_minimum(self) -> None:
        result = asyncio.run(
            validate_available_tool_call_with_context(
                [schedule_mod.MANAGE_SCHEDULES_TOOL],
                "manage_schedules",
                {
                    "action": "create",
                    "name": "Too frequent",
                    "schedule_type": "interval",
                    "interval_seconds": 5,
                },
                None,
            )
        )

        assert result["valid"] is False

    def test_update_domain_validation_uses_persisted_schedule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = SimpleNamespace(
            schedule_id="schedule-1",
            created_by="owner@example.com",
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=60,
            one_shot_at=None,
        )

        async def fake_get_schedule(_session: object, schedule_id: str) -> object:
            assert schedule_id == "schedule-1"
            return existing

        monkeypatch.setattr("cognis.store.queries.get_schedule", fake_get_schedule)
        result = asyncio.run(
            validate_available_tool_call_with_context(
                [schedule_mod.MANAGE_SCHEDULES_TOOL],
                "manage_schedules",
                {
                    "action": "update",
                    "schedule_id": "schedule-1",
                    "schedule_type": "cron",
                },
                NativeValidationContext(
                    actor_email="owner@example.com",
                    session_factory=lambda: _Session(),
                ),
            )
        )

        assert result["valid"] is False
        assert any(error["code"] == "invalid_schedule_definition" for error in result["errors"])

    def test_cron_requires_expr(self) -> None:
        with pytest.raises(ValueError, match="cron_expr is required"):
            ScheduleModel(
                schedule_id="s1",
                name="test",
                schedule_type=ScheduleType.CRON,
                agent_id="a1",
                task_template={},
                created_by="user@test.com",
            )

    def test_cron_with_expr_ok(self) -> None:
        m = ScheduleModel(
            schedule_id="s1",
            name="test",
            schedule_type=ScheduleType.CRON,
            cron_expr="0 9 * * *",
            agent_id="a1",
            task_template={},
            created_by="user@test.com",
        )
        assert m.cron_expr == "0 9 * * *"

    def test_interval_requires_seconds(self) -> None:
        with pytest.raises(ValueError, match="interval_seconds is required"):
            ScheduleModel(
                schedule_id="s1",
                name="test",
                schedule_type=ScheduleType.INTERVAL,
                agent_id="a1",
                task_template={},
                created_by="user@test.com",
            )

    def test_interval_minimum(self) -> None:
        with pytest.raises(ValueError, match="at least 10"):
            ScheduleModel(
                schedule_id="s1",
                name="test",
                schedule_type=ScheduleType.INTERVAL,
                interval_seconds=5,
                agent_id="a1",
                task_template={},
                created_by="user@test.com",
            )

    def test_interval_ok(self) -> None:
        m = ScheduleModel(
            schedule_id="s1",
            name="test",
            schedule_type=ScheduleType.INTERVAL,
            interval_seconds=300,
            agent_id="a1",
            task_template={},
            created_by="user@test.com",
        )
        assert m.interval_seconds == 300

    def test_one_shot_requires_at(self) -> None:
        with pytest.raises(ValueError, match="one_shot_at is required"):
            ScheduleModel(
                schedule_id="s1",
                name="test",
                schedule_type=ScheduleType.ONE_SHOT,
                agent_id="a1",
                task_template={},
                created_by="user@test.com",
            )

    def test_one_shot_ok(self) -> None:
        at = datetime.now(UTC) + timedelta(hours=1)
        m = ScheduleModel(
            schedule_id="s1",
            name="test",
            schedule_type=ScheduleType.ONE_SHOT,
            one_shot_at=at,
            agent_id="a1",
            task_template={},
            created_by="user@test.com",
        )
        assert m.one_shot_at == at


# ---------------------------------------------------------------------------
# Human-readable descriptions
# ---------------------------------------------------------------------------


class TestDescribeCron:
    """Test cron expression to human-readable conversion."""

    def test_preset_every_minute(self) -> None:
        assert describe_cron("* * * * *") == "Every minute"

    def test_preset_daily_at_8(self) -> None:
        assert describe_cron("0 8 * * *") == "Every day at 8:00"

    def test_preset_weekdays_at_9(self) -> None:
        assert describe_cron("0 9 * * 1-5") == "Weekdays at 9:00"

    def test_every_n_minutes(self) -> None:
        assert describe_cron("*/15 * * * *") == "Every 15 minutes"

    def test_every_n_hours(self) -> None:
        assert describe_cron("0 */4 * * *") == "Every 4 hours"

    def test_specific_weekday(self) -> None:
        assert describe_cron("0 10 * * 3") == "Every Wednesday at 10:00"

    def test_complex_fallback(self) -> None:
        # 6-field expressions or unusual patterns fall back to raw
        result = describe_cron("0 0 1 1 *")
        assert result == "Every year on January 1st"


class TestDescribeInterval:
    """Test interval seconds to human-readable conversion."""

    def test_seconds(self) -> None:
        assert describe_interval(30) == "Every 30 seconds"

    def test_minutes(self) -> None:
        assert describe_interval(300) == "Every 5 minutes"

    def test_hours(self) -> None:
        assert describe_interval(7200) == "Every 2 hours"

    def test_hours_with_minutes(self) -> None:
        assert describe_interval(5400) == "Every 1h 30m"

    def test_days(self) -> None:
        assert describe_interval(86400) == "Every 1 day"


class TestDescribeSchedule:
    """Test the unified describe_schedule function."""

    def test_cron_schedule(self) -> None:
        m = ScheduleModel(
            schedule_id="s1",
            name="test",
            schedule_type=ScheduleType.CRON,
            cron_expr="0 9 * * *",
            agent_id="a1",
            task_template={},
            created_by="user@test.com",
        )
        assert describe_schedule(m) == "Every day at 9:00"

    def test_cron_with_timezone(self) -> None:
        m = ScheduleModel(
            schedule_id="s1",
            name="test",
            schedule_type=ScheduleType.CRON,
            cron_expr="0 9 * * *",
            timezone="Europe/Prague",
            agent_id="a1",
            task_template={},
            created_by="user@test.com",
        )
        assert "Europe/Prague" in describe_schedule(m)

    def test_interval_schedule(self) -> None:
        m = ScheduleModel(
            schedule_id="s1",
            name="test",
            schedule_type=ScheduleType.INTERVAL,
            interval_seconds=1800,
            agent_id="a1",
            task_template={},
            created_by="user@test.com",
        )
        assert describe_schedule(m) == "Every 30 minutes"


# ---------------------------------------------------------------------------
# Scheduler engine — next fire computation
# ---------------------------------------------------------------------------


class TestSchedulerNextFire:
    """Test Scheduler._compute_next_fire for different schedule types."""

    def _make_scheduler(self) -> Scheduler:
        """Create a Scheduler instance without starting it."""
        s = Scheduler.__new__(Scheduler)
        return s

    def test_interval_next_fire(self) -> None:
        s = self._make_scheduler()
        now = datetime.now(UTC)
        sched = _FakeSchedule(
            schedule_type="interval",
            interval_seconds=300,
            cron_expr=None,
            one_shot_at=None,
            timezone="UTC",
            last_fired_at=None,
        )
        result = s._compute_next_fire(sched, now)
        assert result is not None
        assert (result - now).total_seconds() == pytest.approx(300, abs=1)

    def test_cron_next_fire(self) -> None:
        s = self._make_scheduler()
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        sched = _FakeSchedule(
            schedule_type="cron",
            cron_expr="0 9 * * *",
            interval_seconds=None,
            one_shot_at=None,
            timezone="UTC",
            last_fired_at=None,
        )
        result = s._compute_next_fire(sched, now)
        assert result is not None
        assert result.hour == 9
        assert result.day == 1

    def test_cron_next_fire_preserves_dst_transition(self) -> None:
        s = self._make_scheduler()
        after = datetime(2026, 3, 28, 8, 30, tzinfo=UTC)
        sched = _FakeSchedule(
            schedule_type="cron",
            cron_expr="0 9 * * *",
            interval_seconds=None,
            one_shot_at=None,
            timezone="Europe/Prague",
            last_fired_at=None,
        )

        result = s._compute_next_fire(sched, after)

        assert result == datetime(2026, 3, 29, 7, 0, tzinfo=UTC)

    def test_one_shot_not_yet_fired(self) -> None:
        s = self._make_scheduler()
        now = datetime.now(UTC)
        future = now + timedelta(hours=2)
        sched = _FakeSchedule(
            schedule_type="one_shot",
            cron_expr=None,
            interval_seconds=None,
            one_shot_at=future,
            timezone="UTC",
            last_fired_at=None,
        )
        result = s._compute_next_fire(sched, now)
        assert result == future

    def test_one_shot_already_fired(self) -> None:
        s = self._make_scheduler()
        now = datetime.now(UTC)
        past = now - timedelta(hours=1)
        sched = _FakeSchedule(
            schedule_type="one_shot",
            cron_expr=None,
            interval_seconds=None,
            one_shot_at=past,
            timezone="UTC",
            last_fired_at=now - timedelta(minutes=30),
        )
        result = s._compute_next_fire(sched, now)
        assert result is None


class TestSchedulerBackoff:
    """Test backoff delay computation."""

    def test_first_error(self) -> None:
        s = Scheduler.__new__(Scheduler)
        assert s._compute_backoff_delay(1) == 30

    def test_second_error(self) -> None:
        s = Scheduler.__new__(Scheduler)
        assert s._compute_backoff_delay(2) == 60

    def test_fifth_error(self) -> None:
        s = Scheduler.__new__(Scheduler)
        assert s._compute_backoff_delay(5) == 3600

    def test_beyond_schedule(self) -> None:
        s = Scheduler.__new__(Scheduler)
        # Should cap at the last value
        assert s._compute_backoff_delay(10) == 3600


@pytest.mark.asyncio
async def test_fire_schedule_defaults_empty_delivery_to_preferred_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_submit: dict[str, Any] = {}
    updates: list[dict[str, Any]] = []

    class _TaskQueue:
        async def submit(self, **kwargs: Any) -> SimpleNamespace:
            captured_submit.update(kwargs)
            return SimpleNamespace(task_id="task_1")

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def _get_schedule(_db: object, schedule_id: str) -> Any:
        assert schedule_id == "sched_1"
        return _schedule_row(
            task_template={"title": "Scheduled task", "delivery": {}},
            consecutive_errors=2,
        )

    async def _get_task(_db: object, task_id: str) -> Any:
        assert task_id == "task_1"
        return SimpleNamespace(status="ready")

    async def _count_active_tasks_for_schedule(*_args: Any, **_kwargs: Any) -> int:
        return 0

    scheduler = Scheduler.__new__(Scheduler)
    scheduler._task_queue = _TaskQueue()  # type: ignore[attr-defined]
    scheduler._db_session = lambda: _Session()  # type: ignore[attr-defined]
    scheduler._event_bus = EventBus()  # type: ignore[attr-defined]
    scheduler._max_concurrent_runs = 1  # type: ignore[attr-defined]
    scheduler._max_consecutive_errors = 3  # type: ignore[attr-defined]

    async def _update_schedule_fire_state(_db: object, _schedule_id: str, **kwargs: Any) -> None:
        updates.append(kwargs)

    monkeypatch.setattr(
        "cognis.core.scheduler.update_schedule_fire_state",
        _update_schedule_fire_state,
    )
    monkeypatch.setattr("cognis.core.scheduler.get_schedule", _get_schedule)
    monkeypatch.setattr("cognis.core.scheduler.get_task", _get_task)
    monkeypatch.setattr(
        "cognis.core.scheduler.count_active_tasks_for_schedule",
        _count_active_tasks_for_schedule,
    )

    task_id = await scheduler._fire_schedule("sched_1")  # type: ignore[attr-defined]

    assert task_id == "task_1"
    delivery = captured_submit["delivery"]
    assert isinstance(delivery, TaskDelivery)
    assert delivery.mode == "preferred_channel"
    assert captured_submit["source_type"] == "scheduler"
    assert captured_submit["scheduled_for"] is not None
    assert updates[0]["consecutive_errors"] == 2


@pytest.mark.asyncio
async def test_fire_schedule_error_publishes_schedule_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TaskQueue:
        async def submit(self, **_kwargs: Any) -> SimpleNamespace:
            raise RuntimeError("boom")

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def _get_schedule(_db: object, schedule_id: str) -> Any:
        assert schedule_id == "sched_1"
        return _schedule_row()

    async def _count_active_tasks_for_schedule(*_args: Any, **_kwargs: Any) -> int:
        return 0

    events: list[Any] = []
    bus = EventBus()

    async def _capture(event: Any) -> None:
        events.append(event)

    bus.subscribe_all(_capture)

    scheduler = Scheduler.__new__(Scheduler)
    scheduler._task_queue = _TaskQueue()  # type: ignore[attr-defined]
    scheduler._db_session = lambda: _Session()  # type: ignore[attr-defined]
    scheduler._event_bus = bus  # type: ignore[attr-defined]
    scheduler._max_concurrent_runs = 1  # type: ignore[attr-defined]
    scheduler._max_consecutive_errors = 3  # type: ignore[attr-defined]

    async def _update_schedule_fire_state(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "cognis.core.scheduler.update_schedule_fire_state",
        _update_schedule_fire_state,
    )
    monkeypatch.setattr("cognis.core.scheduler.get_schedule", _get_schedule)
    monkeypatch.setattr(
        "cognis.core.scheduler.count_active_tasks_for_schedule",
        _count_active_tasks_for_schedule,
    )

    await scheduler._fire_schedule("sched_1")  # type: ignore[attr-defined]

    assert len(events) == 1
    event = events[0]
    assert event.type == EventType.SCHEDULE_ERROR
    assert event.data["schedule_id"] == "sched_1"
    assert event.data["created_by"] == "user@example.com"
    assert event.data["agent_id"] == "agent_1"
    assert event.data["schedule_name"] == "Daily check"
    assert event.data["error"] == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_scheduler_task_failure_propagates_to_schedule_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_row = SimpleNamespace(
        task_id="task_1",
        source_type="scheduler",
        source_ref="sched_1",
        status="failed",
        result_summary="LLMStreamProviderError: usage_limit_reached",
        created_at=datetime(2026, 5, 4, 7, 1, tzinfo=UTC),
    )
    sched_row = _schedule_row(consecutive_errors=1)
    updates: list[dict[str, Any]] = []
    events: list[Event] = []
    bus = EventBus()

    async def _capture(event: Event) -> None:
        events.append(event)

    async def _get_task(_db: object, task_id: str) -> Any:
        assert task_id == "task_1"
        return task_row

    async def _get_schedule(_db: object, schedule_id: str) -> Any:
        assert schedule_id == "sched_1"
        return sched_row

    async def _update_schedule_fire_state(_db: object, schedule_id: str, **kwargs: Any) -> None:
        assert schedule_id == "sched_1"
        updates.append(kwargs)

    bus.subscribe_all(_capture)
    monkeypatch.setattr("cognis.core.scheduler.get_task", _get_task)
    monkeypatch.setattr("cognis.core.scheduler.get_schedule", _get_schedule)
    monkeypatch.setattr(
        "cognis.core.scheduler.update_schedule_fire_state",
        _update_schedule_fire_state,
    )

    scheduler = Scheduler.__new__(Scheduler)
    scheduler._db_session = lambda: _Session()  # type: ignore[attr-defined]
    scheduler._event_bus = bus  # type: ignore[attr-defined]
    scheduler._fire_store = _NonManualFireStore()  # type: ignore[attr-defined]
    scheduler._max_consecutive_errors = 3  # type: ignore[attr-defined]

    await scheduler._handle_task_terminal_event(  # type: ignore[attr-defined]
        Event(type=EventType.TASK_FAILED, data={"task_id": "task_1"})
    )

    assert len(updates) == 1
    assert updates[0]["last_run_status"] == "failed"
    assert updates[0]["consecutive_errors"] == 2
    assert updates[0]["next_fire_at"] is not None
    assert events[-1].type == EventType.SCHEDULE_ERROR
    assert events[-1].data["schedule_id"] == "sched_1"
    assert events[-1].data["consecutive_errors"] == 2
    assert events[-1].data["task_id"] == "task_1"


@pytest.mark.asyncio
async def test_scheduler_task_success_resets_consecutive_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_row = SimpleNamespace(
        task_id="task_1",
        source_type="scheduler",
        source_ref="sched_1",
        status="completed",
        result_summary="Done",
        created_at=datetime(2026, 5, 4, 7, 1, tzinfo=UTC),
    )
    sched_row = _schedule_row(consecutive_errors=3)
    updates: list[dict[str, Any]] = []

    async def _get_task(_db: object, task_id: str) -> Any:
        assert task_id == "task_1"
        return task_row

    async def _get_schedule(_db: object, schedule_id: str) -> Any:
        assert schedule_id == "sched_1"
        return sched_row

    async def _update_schedule_fire_state(_db: object, schedule_id: str, **kwargs: Any) -> None:
        assert schedule_id == "sched_1"
        updates.append(kwargs)

    monkeypatch.setattr("cognis.core.scheduler.get_task", _get_task)
    monkeypatch.setattr("cognis.core.scheduler.get_schedule", _get_schedule)
    monkeypatch.setattr(
        "cognis.core.scheduler.update_schedule_fire_state",
        _update_schedule_fire_state,
    )

    scheduler = Scheduler.__new__(Scheduler)
    scheduler._db_session = lambda: _Session()  # type: ignore[attr-defined]
    scheduler._event_bus = EventBus()  # type: ignore[attr-defined]
    scheduler._fire_store = _NonManualFireStore()  # type: ignore[attr-defined]

    await scheduler._handle_task_terminal_event(  # type: ignore[attr-defined]
        Event(type=EventType.TASK_COMPLETED, data={"task_id": "task_1"})
    )

    assert len(updates) == 1
    assert updates[0]["last_run_status"] == "success"
    assert updates[0]["consecutive_errors"] == 0
    assert updates[0]["next_fire_at"] is not None


@pytest.mark.asyncio
async def test_scheduler_ignores_stale_task_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_row = SimpleNamespace(
        task_id="task_old",
        source_type="scheduler",
        source_ref="sched_1",
        status="failed",
        result_summary="old failure",
        created_at=datetime(2026, 5, 4, 7, 0, tzinfo=UTC),
    )
    sched_row = _schedule_row(
        last_fired_at=datetime(2026, 5, 4, 8, 0, tzinfo=UTC),
        consecutive_errors=0,
    )
    updates: list[dict[str, Any]] = []
    events: list[Event] = []
    bus = EventBus()

    async def _capture(event: Event) -> None:
        events.append(event)

    async def _get_task(_db: object, task_id: str) -> Any:
        assert task_id == "task_old"
        return task_row

    async def _get_schedule(_db: object, schedule_id: str) -> Any:
        assert schedule_id == "sched_1"
        return sched_row

    async def _update_schedule_fire_state(_db: object, schedule_id: str, **kwargs: Any) -> None:
        assert schedule_id == "sched_1"
        updates.append(kwargs)

    bus.subscribe_all(_capture)
    monkeypatch.setattr("cognis.core.scheduler.get_task", _get_task)
    monkeypatch.setattr("cognis.core.scheduler.get_schedule", _get_schedule)
    monkeypatch.setattr(
        "cognis.core.scheduler.update_schedule_fire_state",
        _update_schedule_fire_state,
    )

    scheduler = Scheduler.__new__(Scheduler)
    scheduler._db_session = lambda: _Session()  # type: ignore[attr-defined]
    scheduler._event_bus = bus  # type: ignore[attr-defined]
    scheduler._fire_store = _NonManualFireStore()  # type: ignore[attr-defined]
    scheduler._max_consecutive_errors = 3  # type: ignore[attr-defined]

    await scheduler._handle_task_terminal_event(  # type: ignore[attr-defined]
        Event(type=EventType.TASK_FAILED, data={"task_id": "task_old"})
    )

    assert updates == []
    assert events == []


# ---------------------------------------------------------------------------
# Schedule tool classification
# ---------------------------------------------------------------------------


class TestScheduleToolClassification:
    """Test that the tool router classifies schedule tools correctly."""

    def test_manage_schedules_classified(self) -> None:
        from cognis.core.tool_router import ToolRoute, ToolRouter
        from cognis.tools.registry import ToolRegistry

        router = ToolRouter(guardrails=None)
        registry = ToolRegistry()
        route = router.classify("manage_schedules", registry)
        assert route is ToolRoute.SCHEDULE

    def test_non_schedule_not_classified(self) -> None:
        from cognis.core.tool_router import ToolRoute, ToolRouter
        from cognis.tools.registry import ToolRegistry

        router = ToolRouter(guardrails=None)
        registry = ToolRegistry()
        route = router.classify("some_other_tool", registry)
        assert route is not ToolRoute.SCHEDULE


# ---------------------------------------------------------------------------
# Schedule tool definition output and patch semantics
# ---------------------------------------------------------------------------


class TestScheduleToolDefinitionOutput:
    """Test LLM-facing schedule tool output and safe patch behavior."""

    def test_definition_payload_includes_full_editable_definition(self) -> None:
        payload = schedule_mod._schedule_definition_payload(_schedule_row(), active_tasks=1)

        assert payload["name"] == "Daily check"
        assert payload["schedule_type"] == "cron"
        assert payload["cron_expr"] == "0 8 * * *"
        assert payload["timezone"] == "UTC"
        assert payload["agent_id"] == "agent_1"
        assert payload["agent_profile_id"] == "fast"
        assert payload["workflow_id"] == "system:general-task"
        assert payload["project_id"] == "proj_1"
        assert payload["task_title"] == "Daily check task"
        assert payload["task_description"] == "Check the account and preserve these instructions."
        assert payload["expected_output"] == "A concise status update."
        assert payload["delivery_mode"] == "specific_conversation"
        assert payload["delivery_target"] == "conv_1"
        assert payload["completion_mode_family"] == "direct"
        assert payload["allow_silent_completion"] is True
        assert payload["enabled"] is True
        assert payload["max_concurrent_runs"] == 2
        assert payload["active_tasks"] == 1
        assert payload["task_template"]["api_token"] == "[redacted]"

    def test_schema_exposes_get_and_patch_guidance(self) -> None:
        operations = schedule_mod.MANAGE_SCHEDULES_TOOL.native_operations
        assert operations is not None
        by_name = {operation.operation: operation for operation in operations}
        assert "options" not in by_name
        assert "get" in by_name
        assert "include_definition" in by_name["list"].input_schema["properties"]
        assert "delivery_target" in by_name["create"].input_schema["properties"]
        assert "patch" in schedule_mod.MANAGE_SCHEDULES_TOOL.description.lower()

    def test_update_preserves_omitted_task_definition_fields(self, monkeypatch: Any) -> None:
        row = _schedule_row()
        updated_fields: dict[str, Any] = {}

        async def fake_get_schedule(_db: object, schedule_id: str) -> Any:
            assert schedule_id == "sched_1"
            return row

        async def fake_update_schedule(_db: object, schedule_id: str, **fields: Any) -> Any:
            assert schedule_id == "sched_1"
            updated_fields.update(fields)
            for key, value in fields.items():
                setattr(row, key, value)
            return row

        async def fake_count_active_tasks(_db: object, schedule_id: str) -> int:
            assert schedule_id == "sched_1"
            return 0

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)
        monkeypatch.setattr(schedule_mod, "update_schedule", fake_update_schedule)
        monkeypatch.setattr(
            schedule_mod,
            "count_active_tasks_for_schedule",
            fake_count_active_tasks,
        )

        result = asyncio.run(
            schedule_mod._handle_update(
                lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                arguments={
                    "schedule_id": "sched_1",
                    "allow_silent_completion": False,
                    "delivery_mode": "latest_active_for_agent",
                },
            )
        )

        assert result.is_error is False
        payload = json.loads(result.output)
        assert payload["allow_silent_completion"] is False
        assert payload["delivery_mode"] == "latest_active_for_agent"
        assert payload["delivery_target"] is None
        assert payload["task_description"] == "Check the account and preserve these instructions."
        assert payload["expected_output"] == "A concise status update."
        assert updated_fields["task_template"]["description"] == (
            "Check the account and preserve these instructions."
        )
        assert updated_fields["task_template"]["delivery"] == {
            "mode": "latest_active_for_agent",
            "target": None,
        }

    def test_handle_tool_strips_empty_delivery_target_for_non_specific_mode(
        self, monkeypatch: Any
    ) -> None:
        row = _schedule_row()
        updated_fields: dict[str, Any] = {}
        conversation_lookups: list[str] = []

        async def fake_get_schedule(_db: object, _schedule_id: str) -> Any:
            return row

        async def fake_update_schedule(_db: object, _schedule_id: str, **fields: Any) -> Any:
            updated_fields.update(fields)
            for key, value in fields.items():
                setattr(row, key, value)
            return row

        async def fake_count_active_tasks(_db: object, _schedule_id: str) -> int:
            return 0

        async def fake_get_conversation(_db: object, conversation_id: str) -> Any:
            conversation_lookups.append(conversation_id)
            return None

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)
        monkeypatch.setattr(schedule_mod, "update_schedule", fake_update_schedule)
        monkeypatch.setattr(
            schedule_mod,
            "count_active_tasks_for_schedule",
            fake_count_active_tasks,
        )
        monkeypatch.setattr(schedule_mod, "get_conversation", fake_get_conversation)

        result = asyncio.run(
            schedule_mod.handle_schedule_tool(
                "manage_schedules",
                {
                    "action": "update",
                    "schedule_id": "sched_1",
                    "delivery_mode": "latest_active_for_agent",
                    "delivery_target": "",
                },
                session_factory=lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                agent_id="agent_1",
            )
        )

        assert result.is_error is False
        assert conversation_lookups == []
        assert updated_fields["task_template"]["delivery"] == {
            "mode": "latest_active_for_agent",
            "target": None,
        }

    def test_update_ignores_empty_irrelevant_one_shot_at_for_interval_patch(
        self, monkeypatch: Any
    ) -> None:
        row = _schedule_row(
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=3600,
            task_template={
                "description": "Run interval task.",
                "delivery": {"mode": "preferred_channel", "target": None},
            },
        )
        updated_fields: dict[str, Any] = {}

        async def fake_get_schedule(_db: object, _schedule_id: str) -> Any:
            return row

        async def fake_update_schedule(_db: object, _schedule_id: str, **fields: Any) -> Any:
            updated_fields.update(fields)
            for key, value in fields.items():
                setattr(row, key, value)
            return row

        async def fake_count_active_tasks(_db: object, _schedule_id: str) -> int:
            return 0

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)
        monkeypatch.setattr(schedule_mod, "update_schedule", fake_update_schedule)
        monkeypatch.setattr(
            schedule_mod,
            "count_active_tasks_for_schedule",
            fake_count_active_tasks,
        )

        result = asyncio.run(
            schedule_mod.handle_schedule_tool(
                "manage_schedules",
                {
                    "action": "update",
                    "schedule_id": "sched_1",
                    "task_description": "Updated interval task.",
                    "delivery_mode": "preferred_channel",
                    "delivery_target": "",
                    "one_shot_at": "",
                },
                session_factory=lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                agent_id="agent_1",
            )
        )

        assert result.is_error is False
        assert updated_fields["task_template"]["description"] == "Updated interval task."
        assert updated_fields["task_template"]["delivery"] == {
            "mode": "preferred_channel",
            "target": None,
        }
        assert (
            not {
                "schedule_type",
                "cron_expr",
                "interval_seconds",
                "one_shot_at",
                "next_fire_at",
            }
            & updated_fields.keys()
        )

    def test_rescheduling_fired_one_shot_computes_next_fire(self) -> None:
        next_time = datetime(2099, 1, 1, tzinfo=UTC)
        existing = _schedule_row(
            schedule_type="one_shot",
            cron_expr=None,
            interval_seconds=None,
            one_shot_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_fired_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        next_fire = schedule_mod._compute_next_fire_for_update(
            existing,
            {
                "schedule_type": "one_shot",
                "cron_expr": None,
                "interval_seconds": None,
                "one_shot_at": next_time,
            },
        )

        assert next_fire == next_time

    def test_update_ignores_stale_one_shot_at_when_switching_to_interval(
        self, monkeypatch: Any
    ) -> None:
        row = _schedule_row()
        updated_fields: dict[str, Any] = {}

        async def fake_get_schedule(_db: object, _schedule_id: str) -> Any:
            return row

        async def fake_update_schedule(_db: object, _schedule_id: str, **fields: Any) -> Any:
            updated_fields.update(fields)
            for key, value in fields.items():
                setattr(row, key, value)
            return row

        async def fake_count_active_tasks(_db: object, _schedule_id: str) -> int:
            return 0

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)
        monkeypatch.setattr(schedule_mod, "update_schedule", fake_update_schedule)
        monkeypatch.setattr(
            schedule_mod,
            "count_active_tasks_for_schedule",
            fake_count_active_tasks,
        )

        result = asyncio.run(
            schedule_mod._handle_update(
                lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                arguments={
                    "schedule_id": "sched_1",
                    "schedule_type": "interval",
                    "interval_seconds": 10800,
                    "one_shot_at": "2099-01-01T00:00:00+00:00",
                },
            )
        )

        assert result.is_error is False
        assert updated_fields["schedule_type"] == "interval"
        assert updated_fields["cron_expr"] is None
        assert updated_fields["interval_seconds"] == 10800
        assert updated_fields["one_shot_at"] is None
        assert isinstance(updated_fields["next_fire_at"], datetime)

    def test_update_ignores_irrelevant_interval_seconds_for_cron(self, monkeypatch: Any) -> None:
        row = _schedule_row()
        updated_fields: dict[str, Any] = {}

        async def fake_get_schedule(_db: object, _schedule_id: str) -> Any:
            return row

        async def fake_update_schedule(_db: object, _schedule_id: str, **fields: Any) -> Any:
            updated_fields.update(fields)
            for key, value in fields.items():
                setattr(row, key, value)
            return row

        async def fake_count_active_tasks(_db: object, _schedule_id: str) -> int:
            return 0

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)
        monkeypatch.setattr(schedule_mod, "update_schedule", fake_update_schedule)
        monkeypatch.setattr(
            schedule_mod,
            "count_active_tasks_for_schedule",
            fake_count_active_tasks,
        )

        result = asyncio.run(
            schedule_mod._handle_update(
                lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                arguments={
                    "schedule_id": "sched_1",
                    "schedule_type": "cron",
                    "cron_expr": "0 */3 * * *",
                    "interval_seconds": 10800,
                    "one_shot_at": "2099-01-01T00:00:00+00:00",
                },
            )
        )

        assert result.is_error is False
        assert updated_fields["schedule_type"] == "cron"
        assert updated_fields["cron_expr"] == "0 */3 * * *"
        assert updated_fields["interval_seconds"] is None
        assert updated_fields["one_shot_at"] is None
        assert isinstance(updated_fields["next_fire_at"], datetime)

    def test_update_rejects_invalid_delivery_mode(self) -> None:
        result = schedule_mod._resolve_delivery({"delivery_mode": "newest_chat"})

        assert result[1] is not None
        assert result[1].is_error is True
        assert "Invalid delivery_mode" in result[1].output
        assert "latest_active_for_agent" in result[1].output

    def test_update_rejects_inaccessible_delivery_target(self, monkeypatch: Any) -> None:
        row = _schedule_row()

        async def fake_get_schedule(_db: object, _schedule_id: str) -> Any:
            return row

        async def fake_get_conversation(_db: object, _conversation_id: str) -> Any:
            return None

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)
        monkeypatch.setattr(schedule_mod, "get_conversation", fake_get_conversation)

        result = asyncio.run(
            schedule_mod._handle_update(
                lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                arguments={
                    "schedule_id": "sched_1",
                    "delivery_mode": "specific_conversation",
                    "delivery_target": "conv_other",
                },
            )
        )

        assert result.is_error is True
        assert "Conversation conv_other not found" in result.output

    def test_update_rejects_inaccessible_workflow_without_agent_patch(
        self, monkeypatch: Any
    ) -> None:
        row = _schedule_row()

        async def fake_get_schedule(_db: object, _schedule_id: str) -> Any:
            return row

        async def fake_list_workflows(
            _db: object, *, owner_email: str | None = None, include_system: bool = True
        ) -> list[Any]:
            assert owner_email == "user@example.com"
            assert include_system is False
            return []

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)
        monkeypatch.setattr(schedule_mod, "list_workflows", fake_list_workflows)

        result = asyncio.run(
            schedule_mod._handle_update(
                lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                arguments={
                    "schedule_id": "sched_1",
                    "workflow_id": "wf_other_user",
                },
            )
        )

        assert result.is_error is True
        assert "Workflow wf_other_user not found or not available" in result.output

    def test_descriptor_replaces_options_action(self) -> None:
        descriptor = schedule_mod.MANAGE_SCHEDULES_TOOL.descriptor
        assert descriptor is not None
        create = next(item for item in descriptor.operations if item.operation == "create")
        assert {item.source for item in create.dynamic_options} >= {
            "schedule.visible_agents",
            "schedule.agent_profiles",
            "schedule.available_workflows",
        }
        update = next(item for item in descriptor.operations if item.operation == "update")
        assert update.semantics.omitted == "preserve the persisted schedule field"

    def test_update_recomputes_next_fire_for_timing_changes(self, monkeypatch: Any) -> None:
        row = _schedule_row(
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=60,
            task_template={"description": "Run interval task."},
        )
        updated_fields: dict[str, Any] = {}

        async def fake_get_schedule(_db: object, _schedule_id: str) -> Any:
            return row

        async def fake_update_schedule(_db: object, _schedule_id: str, **fields: Any) -> Any:
            updated_fields.update(fields)
            for key, value in fields.items():
                setattr(row, key, value)
            return row

        async def fake_count_active_tasks(_db: object, _schedule_id: str) -> int:
            return 0

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)
        monkeypatch.setattr(schedule_mod, "update_schedule", fake_update_schedule)
        monkeypatch.setattr(
            schedule_mod,
            "count_active_tasks_for_schedule",
            fake_count_active_tasks,
        )

        result = asyncio.run(
            schedule_mod._handle_update(
                lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                arguments={"schedule_id": "sched_1", "interval_seconds": 120},
            )
        )

        assert result.is_error is False
        assert updated_fields["interval_seconds"] == 120
        assert isinstance(updated_fields["next_fire_at"], datetime)

    def test_update_does_not_mutate_project_or_skill_fields(self, monkeypatch: Any) -> None:
        row = _schedule_row()

        async def fake_get_schedule(_db: object, _schedule_id: str) -> Any:
            return row

        monkeypatch.setattr(schedule_mod, "get_schedule", fake_get_schedule)

        result = asyncio.run(
            schedule_mod._handle_update(
                lambda: _Session(),
                scheduler=None,
                user_email="user@example.com",
                arguments={
                    "schedule_id": "sched_1",
                    "project_id": "proj_other",
                    "skill_id": "skill_other",
                },
            )
        )

        assert result.is_error is False
        assert result.output == "No fields to update."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schedule_row(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "schedule_id": "sched_1",
        "name": "Daily check",
        "description": "Run the daily check.",
        "schedule_type": "cron",
        "cron_expr": "0 8 * * *",
        "interval_seconds": None,
        "one_shot_at": None,
        "timezone": "UTC",
        "agent_id": "agent_1",
        "agent_profile_id": "fast",
        "workflow_id": "system:general-task",
        "project_id": "proj_1",
        "skill_id": None,
        "task_template": {
            "title": "Daily check task",
            "description": "Check the account and preserve these instructions.",
            "priority": 3,
            "expected_output": "A concise status update.",
            "delivery": {"mode": "specific_conversation", "target": "conv_1"},
            "api_token": "secret-value",
        },
        "enabled": True,
        "max_concurrent_runs": 2,
        "delete_after_run": False,
        "completion_mode_family": "direct",
        "allow_silent_completion": True,
        "interaction_mode_override": "none",
        "last_fired_at": datetime(2026, 5, 4, 7, 0, tzinfo=UTC),
        "next_fire_at": datetime(2026, 5, 5, 8, 0, tzinfo=UTC),
        "last_run_status": "success",
        "consecutive_errors": 0,
        "disabled_reason": None,
        "created_by": "user@example.com",
        "created_at": datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 4, 7, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _NonManualFireStore:
    async def is_manual_task(self, _task_id: str) -> bool:
        return False


class _FakeSchedule:
    """Minimal schedule-like object for testing _compute_next_fire."""

    def __init__(
        self,
        schedule_type: str,
        cron_expr: str | None,
        interval_seconds: int | None,
        one_shot_at: datetime | None,
        timezone: str,
        last_fired_at: datetime | None,
    ) -> None:
        self.schedule_type = schedule_type
        self.cron_expr = cron_expr
        self.interval_seconds = interval_seconds
        self.one_shot_at = one_shot_at
        self.timezone = timezone
        self.last_fired_at = last_fired_at

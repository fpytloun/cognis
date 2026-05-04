"""Unit tests for the Scheduler engine and schedule domain models."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.scheduler import Scheduler
from cognis.models.schedule import (
    ScheduleModel,
    ScheduleType,
    describe_cron,
    describe_interval,
    describe_schedule,
)
from cognis.tools.builtin import schedule as schedule_mod

# ---------------------------------------------------------------------------
# Schedule model validation
# ---------------------------------------------------------------------------


class TestScheduleModelValidation:
    """Test ScheduleModel field validation."""

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
        schema = schedule_mod.MANAGE_SCHEDULES_TOOL.parameters
        assert "get" in schema["properties"]["action"]["enum"]
        assert "include_definition" in schema["properties"]
        assert "delivery_target" in schema["properties"]
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
        assert payload["delivery_target"] == "conv_1"
        assert payload["task_description"] == "Check the account and preserve these instructions."
        assert payload["expected_output"] == "A concise status update."
        assert updated_fields["task_template"]["description"] == (
            "Check the account and preserve these instructions."
        )

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

"""Unit tests for the Scheduler engine and schedule domain models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cognis.core.scheduler import Scheduler
from cognis.models.schedule import (
    ScheduleModel,
    ScheduleType,
    describe_cron,
    describe_interval,
    describe_schedule,
)

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
# Helpers
# ---------------------------------------------------------------------------


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

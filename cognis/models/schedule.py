"""Schedule domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from cognis.models.workflow import CompletionDeliveryPolicy


class ScheduleType(StrEnum):
    """Schedule trigger types."""

    CRON = "cron"
    INTERVAL = "interval"
    ONE_SHOT = "one_shot"


class ScheduleRunStatus(StrEnum):
    """Outcome of the most recent schedule fire."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ScheduleModel(BaseModel):
    """Domain model for a schedule (task factory)."""

    schedule_id: str
    name: str
    description: str | None = None
    schedule_type: ScheduleType = ScheduleType.CRON
    cron_expr: str | None = None
    interval_seconds: int | None = None
    one_shot_at: datetime | None = None
    timezone: str = "UTC"
    agent_id: str
    workflow_id: str | None = None
    project_id: str | None = None
    skill_id: str | None = None
    task_template: dict[str, Any]
    enabled: bool = True
    max_concurrent_runs: int = 1
    delete_after_run: bool = False
    completion_delivery: CompletionDeliveryPolicy = Field(default_factory=CompletionDeliveryPolicy)
    last_fired_at: datetime | None = None
    next_fire_at: datetime | None = None
    last_run_status: ScheduleRunStatus | None = None
    consecutive_errors: int = 0
    disabled_reason: str | None = None
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_schedule_fields(self) -> ScheduleModel:
        """Ensure the correct fields are set for each schedule type."""
        if self.schedule_type == ScheduleType.CRON and not self.cron_expr:
            raise ValueError("cron_expr is required for cron schedules")
        if self.schedule_type == ScheduleType.INTERVAL and not self.interval_seconds:
            raise ValueError("interval_seconds is required for interval schedules")
        if self.schedule_type == ScheduleType.INTERVAL and (self.interval_seconds or 0) < 10:
            raise ValueError("interval_seconds must be at least 10")
        if self.schedule_type == ScheduleType.ONE_SHOT and not self.one_shot_at:
            raise ValueError("one_shot_at is required for one_shot schedules")
        return self


# ---------------------------------------------------------------------------
# Human-readable schedule description helpers
# ---------------------------------------------------------------------------

_CRON_PRESETS: list[tuple[str, str]] = [
    ("* * * * *", "Every minute"),
    ("*/5 * * * *", "Every 5 minutes"),
    ("*/10 * * * *", "Every 10 minutes"),
    ("*/15 * * * *", "Every 15 minutes"),
    ("*/30 * * * *", "Every 30 minutes"),
    ("0 * * * *", "Every hour"),
    ("0 */2 * * *", "Every 2 hours"),
    ("0 */4 * * *", "Every 4 hours"),
    ("0 */6 * * *", "Every 6 hours"),
    ("0 */12 * * *", "Every 12 hours"),
    ("0 0 * * *", "Every day at midnight"),
    ("0 6 * * *", "Every day at 6:00"),
    ("0 8 * * *", "Every day at 8:00"),
    ("0 9 * * *", "Every day at 9:00"),
    ("0 12 * * *", "Every day at noon"),
    ("0 18 * * *", "Every day at 18:00"),
    ("0 0 * * 1", "Every Monday at midnight"),
    ("0 9 * * 1-5", "Weekdays at 9:00"),
    ("0 0 1 * *", "First day of every month"),
    ("0 0 1 1 *", "Every year on January 1st"),
]

# Indexed by cron day-of-week: 0=Sunday, 1=Monday, ..., 6=Saturday
_WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def describe_cron(expr: str) -> str:
    """Return a human-readable description of a cron expression.

    Handles common patterns; falls back to the raw expression for complex ones.
    """
    # Check presets first
    for pattern, label in _CRON_PRESETS:
        if expr == pattern:
            return label

    parts = expr.split()
    if len(parts) != 5:
        return expr

    minute, hour, dom, month, dow = parts

    try:
        return _describe_parts(minute, hour, dom, month, dow)
    except (ValueError, IndexError):
        return expr


def _describe_parts(minute: str, hour: str, dom: str, month: str, dow: str) -> str:
    """Build human-readable description from cron parts."""
    # Every N minutes
    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"Every {minute[2:]} minutes"

    # Every N hours
    if minute == "0" and hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        return f"Every {hour[2:]} hours"

    # Specific time patterns
    if dom == "*" and month == "*":
        time_str = _format_time(minute, hour)
        if time_str is None:
            return f"{minute} {hour} {dom} {month} {dow}"

        if dow == "*":
            return f"Every day at {time_str}"
        if dow == "1-5":
            return f"Weekdays at {time_str}"
        if dow == "0,6" or dow == "6,0":
            return f"Weekends at {time_str}"
        if "," in dow:
            days = [_WEEKDAYS[int(d) % 7] for d in dow.split(",")]
            return f"{', '.join(days)} at {time_str}"
        if "-" in dow:
            start, end = dow.split("-")
            return f"{_WEEKDAYS[int(start) % 7]}–{_WEEKDAYS[int(end) % 7]} at {time_str}"
        day_idx = int(dow) % 7
        return f"Every {_WEEKDAYS[day_idx]} at {time_str}"

    # Fallback
    return f"{minute} {hour} {dom} {month} {dow}"


def _format_time(minute: str, hour: str) -> str | None:
    """Format minute and hour into HH:MM, or None if not simple integers."""
    try:
        h = int(hour)
        m = int(minute)
        return f"{h:02d}:{m:02d}"
    except ValueError:
        return None


def describe_interval(seconds: int) -> str:
    """Return a human-readable description of an interval in seconds."""
    if seconds < 60:
        return f"Every {seconds} seconds"
    if seconds < 3600:
        mins = seconds // 60
        return f"Every {mins} minute{'s' if mins != 1 else ''}"
    if seconds < 86400:
        hours = seconds // 3600
        remainder = (seconds % 3600) // 60
        if remainder:
            return f"Every {hours}h {remainder}m"
        return f"Every {hours} hour{'s' if hours != 1 else ''}"
    days = seconds // 86400
    return f"Every {days} day{'s' if days != 1 else ''}"


def describe_schedule(schedule: ScheduleModel) -> str:
    """Return a human-readable description of any schedule."""
    if schedule.schedule_type == ScheduleType.CRON and schedule.cron_expr:
        desc = describe_cron(schedule.cron_expr)
        if schedule.timezone != "UTC":
            desc += f" ({schedule.timezone})"
        return desc
    if schedule.schedule_type == ScheduleType.INTERVAL and schedule.interval_seconds:
        return describe_interval(schedule.interval_seconds)
    if schedule.schedule_type == ScheduleType.ONE_SHOT and schedule.one_shot_at:
        return f"Once at {schedule.one_shot_at.strftime('%Y-%m-%d %H:%M %Z')}"
    return "Unknown schedule"

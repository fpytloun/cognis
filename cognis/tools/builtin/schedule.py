"""Built-in schedule management tool definitions and handlers.

These tools are controller-intercepted — they never reach the executor.
The agent can create, list, update, delete, and trigger schedules.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from cognis.logging import get_logger
from cognis.models.schedule import (
    ScheduleModel,
    describe_schedule,
)
from cognis.models.tool import ToolDefinition, ToolResult, ToolSource
from cognis.store.queries import (
    count_active_tasks_for_schedule,
    create_schedule,
    delete_schedule,
    get_schedule,
    list_schedules,
    update_schedule,
)

logger = get_logger(__name__)

_SOURCE = ToolSource(type="builtin")

MANAGE_SCHEDULES_TOOL = ToolDefinition(
    name="manage_schedules",
    description=(
        "Create, list, update, delete, and trigger scheduled tasks. "
        "Schedules are task factories that create tasks on a cron expression, "
        "fixed interval, or one-shot basis. Use this to set up recurring "
        "tasks, reminders, heartbeat checks, and timed automations."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "update", "delete", "trigger", "status"],
                "description": "The action to perform.",
            },
            "schedule_id": {
                "type": "string",
                "description": "Schedule ID (required for update, delete, trigger, status).",
            },
            "name": {
                "type": "string",
                "description": "Human-readable name for the schedule.",
            },
            "description": {
                "type": "string",
                "description": "Description of what this schedule does.",
            },
            "schedule_type": {
                "type": "string",
                "enum": ["cron", "interval", "one_shot"],
                "description": "Type of schedule trigger. Default: cron.",
            },
            "cron_expr": {
                "type": "string",
                "description": (
                    "Cron expression (5 fields: minute hour day month weekday). "
                    "Examples: '0 9 * * 1-5' (weekdays at 9am), "
                    "'*/30 * * * *' (every 30 min), '0 8 * * *' (daily at 8am)."
                ),
            },
            "interval_seconds": {
                "type": "integer",
                "description": "Interval in seconds (minimum 10). For interval type.",
            },
            "one_shot_at": {
                "type": "string",
                "description": "ISO-8601 timestamp for one-shot execution.",
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone for cron evaluation. Default: UTC.",
            },
            "agent_id": {
                "type": "string",
                "description": "Agent to run the task. Defaults to current agent.",
            },
            "workflow_id": {
                "type": "string",
                "description": "Workflow to use for the task (optional).",
            },
            "task_title": {
                "type": "string",
                "description": "Title for tasks created by this schedule.",
            },
            "task_description": {
                "type": "string",
                "description": "Description/prompt for tasks created by this schedule.",
            },
            "task_priority": {
                "type": "integer",
                "description": "Priority for created tasks (0=normal, higher=more urgent).",
            },
            "enabled": {
                "type": "boolean",
                "description": "Whether the schedule is active.",
            },
            "delivery_mode": {
                "type": "string",
                "enum": ["latest_active_for_agent"],
                "description": (
                    "How task results are delivered. "
                    "'latest_active_for_agent' delivers to the most recent active conversation (default)."
                ),
            },
            "expected_output": {
                "type": "string",
                "description": (
                    "What the agent should produce. Used for semantic step evaluation "
                    "in workflow-driven tasks."
                ),
            },
            "completion_mode_family": {
                "type": "string",
                "enum": ["default", "direct"],
                "description": (
                    "How successful task completion is delivered. 'default' uses the normal "
                    "follow-up flow. 'direct' sends the final assistant message directly to the "
                    "resolved target channel."
                ),
            },
            "allow_silent_completion": {
                "type": "boolean",
                "description": (
                    "If true, the agent may complete silently when the work succeeded and there is "
                    "nothing user-actionable to report."
                ),
            },
        },
        "required": ["action"],
    },
    source=_SOURCE,
    category="schedule",
    read_only=False,
    timeout_seconds=30,
)

_TOOL_NAMES = {"manage_schedules"}


def schedule_tools() -> list[ToolDefinition]:
    """Return built-in schedule tool definitions."""
    return [MANAGE_SCHEDULES_TOOL]


def is_schedule_tool(name: str) -> bool:
    """Check if a tool name is a schedule management tool."""
    return name in _TOOL_NAMES


async def handle_schedule_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    session_factory: Any,
    scheduler: Any | None,
    user_email: str,
    agent_id: str | None = None,
) -> ToolResult:
    """Handle a schedule management tool call."""
    action = arguments.get("action", "")

    try:
        if action == "list":
            return await _handle_list(session_factory, user_email, arguments)
        if action == "create":
            return await _handle_create(session_factory, scheduler, user_email, agent_id, arguments)
        if action == "update":
            return await _handle_update(session_factory, scheduler, user_email, arguments)
        if action == "delete":
            return await _handle_delete(session_factory, scheduler, user_email, arguments)
        if action == "trigger":
            return await _handle_trigger(session_factory, scheduler, user_email, arguments)
        if action == "status":
            return await _handle_status(session_factory, user_email, arguments)
        return ToolResult(
            output=f"Unknown action: {action}. Use list, create, update, delete, trigger, or status.",
            is_error=True,
        )
    except Exception as exc:
        logger.exception("Schedule tool error: action=%s", action)
        return ToolResult(output=f"Error: {exc}", is_error=True)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def _handle_list(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
) -> ToolResult:
    """List schedules for the current user."""
    async with session_factory() as db:
        rows = await list_schedules(db, created_by=user_email)

    if not rows:
        return ToolResult(output="No schedules found.")

    items = []
    for r in rows:
        model = _row_to_model(r)
        items.append(
            {
                "schedule_id": r.schedule_id,
                "name": r.name,
                "type": r.schedule_type,
                "schedule": describe_schedule(model),
                "enabled": r.enabled,
                "agent_id": r.agent_id,
                "next_fire_at": r.next_fire_at.isoformat() if r.next_fire_at else None,
                "last_run_status": r.last_run_status,
            }
        )
    return ToolResult(output=json.dumps(items, indent=2))


async def _handle_create(
    session_factory: Any,
    scheduler: Any | None,
    user_email: str,
    agent_id: str | None,
    arguments: dict[str, Any],
) -> ToolResult:
    """Create a new schedule."""
    name = arguments.get("name")
    if not name:
        return ToolResult(output="'name' is required for create.", is_error=True)

    stype = arguments.get("schedule_type", "cron")
    cron_expr = arguments.get("cron_expr")
    interval_seconds = arguments.get("interval_seconds")
    one_shot_at_str = arguments.get("one_shot_at")

    if stype == "cron" and not cron_expr:
        return ToolResult(output="'cron_expr' is required for cron schedules.", is_error=True)
    if stype == "interval" and not interval_seconds:
        return ToolResult(
            output="'interval_seconds' is required for interval schedules.", is_error=True
        )
    if stype == "one_shot" and not one_shot_at_str:
        return ToolResult(output="'one_shot_at' is required for one_shot schedules.", is_error=True)

    # Validate cron expression
    if cron_expr:
        try:
            from croniter import croniter

            croniter(cron_expr)
        except (ValueError, KeyError) as exc:
            return ToolResult(output=f"Invalid cron expression: {exc}", is_error=True)

    # Parse one_shot_at
    one_shot_at = None
    if one_shot_at_str:
        try:
            one_shot_at = datetime.fromisoformat(one_shot_at_str)
        except ValueError:
            return ToolResult(output=f"Invalid one_shot_at: {one_shot_at_str}", is_error=True)

    # Build task template
    task_template: dict[str, Any] = {}
    if arguments.get("task_title"):
        task_template["title"] = arguments["task_title"]
    if arguments.get("task_description"):
        task_template["description"] = arguments["task_description"]
    if arguments.get("task_priority") is not None:
        task_template["priority"] = arguments["task_priority"]
    if arguments.get("expected_output"):
        task_template["expected_output"] = arguments["expected_output"]
    delivery_mode = arguments.get("delivery_mode", "latest_active_for_agent")
    task_template["delivery"] = {"mode": delivery_mode, "target": None}

    target_agent = arguments.get("agent_id") or agent_id
    if not target_agent:
        return ToolResult(output="'agent_id' is required.", is_error=True)

    # Compute initial next_fire_at
    from cognis.core.scheduler import Scheduler

    now = datetime.now(UTC)
    temp = type(
        "_S",
        (),
        {
            "schedule_type": stype,
            "cron_expr": cron_expr,
            "interval_seconds": interval_seconds,
            "one_shot_at": one_shot_at,
            "timezone": arguments.get("timezone", "UTC"),
            "last_fired_at": None,
        },
    )()
    sched_inst = Scheduler.__new__(Scheduler)
    next_fire = sched_inst._compute_next_fire(temp, now)

    async with session_factory() as db:
        row = await create_schedule(
            db,
            name=name,
            description=arguments.get("description"),
            schedule_type=stype,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            one_shot_at=one_shot_at,
            timezone=arguments.get("timezone", "UTC"),
            agent_id=target_agent,
            workflow_id=arguments.get("workflow_id"),
            task_template=task_template,
            enabled=arguments.get("enabled", True),
            completion_mode_family=arguments.get("completion_mode_family", "default"),
            allow_silent_completion=arguments.get("allow_silent_completion", False),
            next_fire_at=next_fire,
            created_by=user_email,
        )
        await db.commit()
        schedule_id = row.schedule_id

    if scheduler is not None:
        await scheduler.notify_schedule_changed(schedule_id)

    model = _row_to_model(row)
    return ToolResult(
        output=(
            f"Schedule '{name}' created (ID: {schedule_id}). "
            f"Schedule: {describe_schedule(model)}. "
            f"Next fire: {next_fire.isoformat() if next_fire else 'N/A'}."
        )
    )


async def _handle_update(
    session_factory: Any,
    scheduler: Any | None,
    user_email: str,
    arguments: dict[str, Any],
) -> ToolResult:
    """Update an existing schedule."""
    schedule_id = arguments.get("schedule_id")
    if not schedule_id:
        return ToolResult(output="'schedule_id' is required for update.", is_error=True)

    async with session_factory() as db:
        existing = await get_schedule(db, schedule_id)
        if existing is None or existing.created_by != user_email:
            return ToolResult(output=f"Schedule {schedule_id} not found.", is_error=True)

        fields: dict[str, Any] = {}
        for key in (
            "name",
            "description",
            "schedule_type",
            "cron_expr",
            "interval_seconds",
            "timezone",
            "agent_id",
            "workflow_id",
            "enabled",
            "completion_mode_family",
            "allow_silent_completion",
        ):
            if key in arguments and arguments[key] is not None:
                fields[key] = arguments[key]

        template_keys = {
            "task_title",
            "task_description",
            "task_priority",
            "delivery_mode",
            "expected_output",
        }
        if template_keys & arguments.keys():
            template = dict(existing.task_template)
            if arguments.get("task_title"):
                template["title"] = arguments["task_title"]
            if arguments.get("task_description"):
                template["description"] = arguments["task_description"]
            if arguments.get("task_priority") is not None:
                template["priority"] = arguments["task_priority"]
            if arguments.get("expected_output") is not None:
                template["expected_output"] = arguments["expected_output"]
            if arguments.get("delivery_mode"):
                delivery = template.get("delivery", {})
                if not isinstance(delivery, dict):
                    delivery = {}
                delivery["mode"] = arguments["delivery_mode"]
                template["delivery"] = delivery
            fields["task_template"] = template

        if not fields:
            return ToolResult(output="No fields to update.")

        await update_schedule(db, schedule_id, **fields)
        await db.commit()

    if scheduler is not None:
        await scheduler.notify_schedule_changed(schedule_id)

    return ToolResult(output=f"Schedule {schedule_id} updated.")


async def _handle_delete(
    session_factory: Any,
    scheduler: Any | None,
    user_email: str,
    arguments: dict[str, Any],
) -> ToolResult:
    """Delete a schedule."""
    schedule_id = arguments.get("schedule_id")
    if not schedule_id:
        return ToolResult(output="'schedule_id' is required for delete.", is_error=True)

    async with session_factory() as db:
        existing = await get_schedule(db, schedule_id)
        if existing is None or existing.created_by != user_email:
            return ToolResult(output=f"Schedule {schedule_id} not found.", is_error=True)
        await delete_schedule(db, schedule_id)
        await db.commit()

    if scheduler is not None:
        scheduler.wake()

    return ToolResult(output=f"Schedule {schedule_id} deleted.")


async def _handle_trigger(
    session_factory: Any,
    scheduler: Any | None,
    user_email: str,
    arguments: dict[str, Any],
) -> ToolResult:
    """Trigger a schedule immediately."""
    schedule_id = arguments.get("schedule_id")
    if not schedule_id:
        return ToolResult(output="'schedule_id' is required for trigger.", is_error=True)

    async with session_factory() as db:
        existing = await get_schedule(db, schedule_id)
        if existing is None or existing.created_by != user_email:
            return ToolResult(output=f"Schedule {schedule_id} not found.", is_error=True)

    if scheduler is None:
        return ToolResult(output="Scheduler is not running.", is_error=True)

    await scheduler._fire_schedule(schedule_id)
    return ToolResult(output=f"Schedule {schedule_id} triggered.")


async def _handle_status(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
) -> ToolResult:
    """Get detailed status of a schedule."""
    schedule_id = arguments.get("schedule_id")
    if not schedule_id:
        return ToolResult(output="'schedule_id' is required for status.", is_error=True)

    async with session_factory() as db:
        row = await get_schedule(db, schedule_id)
        if row is None or row.created_by != user_email:
            return ToolResult(output=f"Schedule {schedule_id} not found.", is_error=True)
        active_tasks = await count_active_tasks_for_schedule(db, schedule_id)

    model = _row_to_model(row)
    info = {
        "schedule_id": row.schedule_id,
        "name": row.name,
        "type": row.schedule_type,
        "schedule": describe_schedule(model),
        "enabled": row.enabled,
        "agent_id": row.agent_id,
        "next_fire_at": row.next_fire_at.isoformat() if row.next_fire_at else None,
        "last_fired_at": row.last_fired_at.isoformat() if row.last_fired_at else None,
        "last_run_status": row.last_run_status,
        "consecutive_errors": row.consecutive_errors,
        "disabled_reason": row.disabled_reason,
        "active_tasks": active_tasks,
        "max_concurrent_runs": row.max_concurrent_runs,
        "completion_mode_family": getattr(row, "completion_mode_family", "default"),
        "allow_silent_completion": bool(getattr(row, "allow_silent_completion", False)),
    }
    return ToolResult(output=json.dumps(info, indent=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_model(row: Any) -> ScheduleModel:
    """Convert a Schedule ORM row to a domain model for description."""
    return ScheduleModel(
        schedule_id=row.schedule_id,
        name=row.name,
        description=row.description,
        schedule_type=row.schedule_type,
        cron_expr=row.cron_expr,
        interval_seconds=row.interval_seconds,
        one_shot_at=row.one_shot_at,
        timezone=row.timezone,
        agent_id=row.agent_id,
        workflow_id=row.workflow_id,
        task_template=row.task_template,
        enabled=row.enabled,
        max_concurrent_runs=row.max_concurrent_runs,
        delete_after_run=row.delete_after_run,
        completion_delivery={
            "completion_mode_family": getattr(row, "completion_mode_family", "default"),
            "allow_silent_completion": bool(getattr(row, "allow_silent_completion", False)),
        },
        last_fired_at=row.last_fired_at,
        next_fire_at=row.next_fire_at,
        last_run_status=row.last_run_status,
        consecutive_errors=row.consecutive_errors,
        disabled_reason=row.disabled_reason,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )

"""Built-in schedule management tool definitions and handlers.

These tools are controller-intercepted — they never reach the executor.
The agent can create, inspect, list, update, delete, and trigger schedules.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from cognis.api.serializers import agent_to_response
from cognis.core.agent_profiles import normalize_agent_profile_id, resolve_agent_profile
from cognis.core.workflow_registry import SYSTEM_WORKFLOWS
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.schedule import (
    ScheduleModel,
    describe_schedule,
)
from cognis.models.tool import (
    NativeToolOperation,
    ToolDefinition,
    ToolDynamicOption,
    ToolMutationKind,
    ToolResult,
    ToolSource,
    ToolValueSemantics,
)
from cognis.models.workflow import CompletionDeliveryPolicy, SessionPolicy
from cognis.store.queries import (
    count_active_tasks_for_schedule,
    create_schedule,
    delete_schedule,
    get_conversation,
    get_schedule,
    list_schedules,
    list_visible_agents,
    list_workflows,
    update_schedule,
)
from cognis.tools.argument_normalization import strip_empty_optional_values

logger = get_logger(__name__)

_SOURCE = ToolSource(type="builtin")

_DESCRIPTION = (
    "Create, inspect, list, update, delete, and trigger scheduled tasks. "
    "Schedules are task factories that create tasks on a cron expression, "
    "fixed interval, or one-shot basis. Use this to set up recurring "
    "tasks, reminders, heartbeat checks, and timed automations. Use update "
    "as a patch: omitted fields are preserved. Use get/status before editing "
    "an existing schedule so task instructions are not lost. Call describe_tool "
    "before unfamiliar or complex mutations."
)

_BASE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list",
                "get",
                "create",
                "update",
                "delete",
                "trigger",
                "status",
            ],
            "description": (
                "The operation to perform. Use describe_tool for full operation semantics "
                "and live option-provider metadata."
            ),
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
            "minimum": 10,
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
        "agent_profile_id": {
            "type": ["string", "null"],
            "description": (
                "Optional runtime profile for the selected schedule agent. Omit or pass null "
                "to use that agent's default profile."
            ),
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
            "enum": [
                "preferred_channel",
                "latest_active_for_agent",
                "specific_conversation",
            ],
            "description": (
                "How task results are delivered. "
                "'preferred_channel' uses the configured preferred channel (default); "
                "'latest_active_for_agent' delivers to the most recent active conversation; "
                "'specific_conversation' requires delivery_target."
            ),
        },
        "delivery_target": {
            "type": "string",
            "description": "Conversation ID, required when delivery_mode is specific_conversation.",
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
        "max_concurrent_runs": {
            "type": "integer",
            "description": "Maximum active tasks this schedule may have at once.",
        },
        "delete_after_run": {
            "type": "boolean",
            "description": "Whether to delete a one-shot schedule after it fires.",
        },
        "interaction_mode_override": {
            "type": "string",
            "enum": ["none", "explicit_gates", "step_requests"],
            "description": "Interaction policy for tasks created by this schedule.",
        },
        "session_policy": {
            "type": "object",
            "description": (
                "Optional Intaris session policy for created tasks. Supports "
                "allow_policies and deny_policies; prefer plain English strings."
            ),
        },
        "include_definition": {
            "type": "boolean",
            "description": (
                "For list/status, include the full persisted schedule definition. "
                "Defaults to true so prompt and delivery fields are visible for safe edits."
            ),
        },
    },
    "required": ["action"],
}

_READ_SEMANTICS = ToolValueSemantics(
    omitted="use the operation default or leave the value absent",
    null="accepted only where the operation schema explicitly permits null",
    arrays="arrays are filters or returned resource collections",
    concurrency="reads observe the current committed schedule state",
)
_MUTATION_SEMANTICS = ToolValueSemantics(
    omitted="use the operation default or leave the value absent",
    null="accepted only where the operation schema explicitly permits null",
    arrays="replace supplied arrays",
    concurrency="validated against current schedule state and committed atomically",
)
_PATCH_SEMANTICS = ToolValueSemantics(
    omitted="preserve the persisted schedule field",
    null="clear only fields whose operation schema explicitly permits null",
    arrays="replace supplied arrays",
    concurrency="call get/status first to avoid overwriting a concurrent edit",
)


def _operation(
    name: str,
    *,
    kind: ToolMutationKind,
    fields: tuple[str, ...] = (),
    required: tuple[str, ...] = (),
    example: dict[str, Any],
    semantics: ToolValueSemantics | None = None,
    dynamic_options: list[ToolDynamicOption] | None = None,
    validator_ids: tuple[str, ...] = (),
    all_of: list[dict[str, Any]] | None = None,
    side_effects: list[str] | None = None,
) -> NativeToolOperation:
    properties = _BASE_SCHEMA["properties"]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "const": name},
            **{field: properties[field] for field in fields},
        },
        "required": ["action", *required],
        "additionalProperties": False,
    }
    if all_of:
        schema["allOf"] = all_of
    return NativeToolOperation(
        operation=name,
        summary=f"{name}: {_DESCRIPTION.split('.', 1)[0]}",
        mutation_kind=kind,
        input_schema=schema,
        semantics=semantics
        or (_READ_SEMANTICS if kind is ToolMutationKind.READ else _MUTATION_SEMANTICS),
        dynamic_options=dynamic_options or [],
        examples=[example],
        side_effects=side_effects
        if side_effects is not None
        else ([] if kind is ToolMutationKind.READ else ["May modify durable schedule state."]),
        validator_ids=list(validator_ids),
    )


_DEFINITION_FIELDS = (
    "name",
    "description",
    "schedule_type",
    "cron_expr",
    "interval_seconds",
    "one_shot_at",
    "timezone",
    "agent_id",
    "agent_profile_id",
    "workflow_id",
    "task_title",
    "task_description",
    "task_priority",
    "enabled",
    "delivery_mode",
    "delivery_target",
    "expected_output",
    "completion_mode_family",
    "allow_silent_completion",
    "max_concurrent_runs",
    "delete_after_run",
    "interaction_mode_override",
    "session_policy",
)
_SCHEDULE_OPTIONS = [
    ToolDynamicOption(path="$.agent_id", source="schedule.visible_agents"),
    ToolDynamicOption(path="$.agent_profile_id", source="schedule.agent_profiles"),
    ToolDynamicOption(path="$.workflow_id", source="schedule.available_workflows"),
]
_CREATE_CONSTRAINTS = [
    {
        "if": {"not": {"required": ["schedule_type"]}},
        "then": {"required": ["cron_expr"]},
    },
    {
        "if": {
            "properties": {"schedule_type": {"const": "interval"}},
            "required": ["schedule_type"],
        },
        "then": {"required": ["interval_seconds"]},
    },
    {
        "if": {
            "properties": {"schedule_type": {"const": "one_shot"}},
            "required": ["schedule_type"],
        },
        "then": {"required": ["one_shot_at"]},
        "else": {
            "if": {
                "properties": {"schedule_type": {"const": "cron"}},
                "required": ["schedule_type"],
            },
            "then": {"required": ["cron_expr"]},
        },
    },
]

MANAGE_SCHEDULES_TOOL = ToolDefinition(
    name="manage_schedules",
    description=_DESCRIPTION,
    parameters={},
    native_operations=[
        _operation(
            "list",
            kind=ToolMutationKind.READ,
            fields=("include_definition",),
            example={"action": "list"},
        ),
        _operation(
            "get",
            kind=ToolMutationKind.READ,
            fields=("schedule_id",),
            required=("schedule_id",),
            example={"action": "get", "schedule_id": "schedule-id"},
        ),
        _operation(
            "create",
            kind=ToolMutationKind.CREATE,
            fields=_DEFINITION_FIELDS,
            required=("name",),
            example={
                "action": "create",
                "name": "Daily check",
                "schedule_type": "cron",
                "cron_expr": "0 8 * * *",
            },
            dynamic_options=_SCHEDULE_OPTIONS,
            validator_ids=("schedule.definition",),
            all_of=_CREATE_CONSTRAINTS,
        ),
        _operation(
            "update",
            kind=ToolMutationKind.UPDATE,
            fields=("schedule_id", *_DEFINITION_FIELDS),
            required=("schedule_id",),
            example={
                "action": "update",
                "schedule_id": "schedule-id",
                "name": "Updated schedule",
            },
            semantics=_PATCH_SEMANTICS,
            dynamic_options=_SCHEDULE_OPTIONS,
            validator_ids=("schedule.definition",),
        ),
        _operation(
            "delete",
            kind=ToolMutationKind.DELETE,
            fields=("schedule_id",),
            required=("schedule_id",),
            example={"action": "delete", "schedule_id": "schedule-id"},
        ),
        _operation(
            "trigger",
            kind=ToolMutationKind.EXECUTE,
            fields=("schedule_id",),
            required=("schedule_id",),
            example={"action": "trigger", "schedule_id": "schedule-id"},
            side_effects=[
                "Creates a task immediately without changing the schedule's next fire time."
            ],
        ),
        _operation(
            "status",
            kind=ToolMutationKind.READ,
            fields=("schedule_id", "include_definition"),
            required=("schedule_id",),
            example={"action": "status", "schedule_id": "schedule-id"},
        ),
    ],
    source=_SOURCE,
    category="schedule",
    read_only=False,
    timeout_seconds=30,
)

_TOOL_NAMES = {"manage_schedules"}
_ACTIONS = ("list", "get", "create", "update", "delete", "trigger", "status")
_SCHEDULE_TYPES = ("cron", "interval", "one_shot")
_DELIVERY_MODES = ("preferred_channel", "latest_active_for_agent", "specific_conversation")
_COMPLETION_MODE_FAMILIES = ("default", "direct")
_INTERACTION_MODE_OVERRIDES = ("none", "explicit_gates", "step_requests")


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
    arguments = strip_empty_optional_values(dict(arguments), MANAGE_SCHEDULES_TOOL.parameters)
    action = arguments.get("action", "")

    try:
        if action == "list":
            return await _handle_list(session_factory, user_email, arguments)
        if action == "get":
            return await _handle_get(session_factory, user_email, arguments)
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
            output=f"Unknown action: {action}. Use one of: {', '.join(_ACTIONS)}.",
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

    include_definition = arguments.get("include_definition", True)
    items = []
    for r in rows:
        if include_definition:
            active_tasks = await _count_active_tasks_safe(session_factory, r.schedule_id)
            items.append(_schedule_definition_payload(r, active_tasks=active_tasks))
        else:
            model = _row_to_model(r)
            items.append(
                {
                    "schedule_id": r.schedule_id,
                    "name": r.name,
                    "type": r.schedule_type,
                    "schedule": describe_schedule(model),
                    "enabled": r.enabled,
                    "agent_id": r.agent_id,
                    "agent_profile_id": getattr(r, "agent_profile_id", None),
                    "next_fire_at": _iso(r.next_fire_at),
                    "last_run_status": r.last_run_status,
                }
            )
    return ToolResult(output=json.dumps(items, indent=2))


async def _handle_get(
    session_factory: Any,
    user_email: str,
    arguments: dict[str, Any],
) -> ToolResult:
    """Get the full persisted definition of one schedule."""
    schedule_id = arguments.get("schedule_id")
    if not schedule_id:
        return ToolResult(output="'schedule_id' is required for get.", is_error=True)

    async with session_factory() as db:
        row = await get_schedule(db, schedule_id)
        if row is None or row.created_by != user_email:
            return ToolResult(output=f"Schedule {schedule_id} not found.", is_error=True)
        active_tasks = await count_active_tasks_for_schedule(db, schedule_id)

    return ToolResult(
        output=json.dumps(_schedule_definition_payload(row, active_tasks=active_tasks), indent=2)
    )


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

    timing_fields, timing_error = _resolve_timing_fields(arguments)
    if timing_error is not None:
        return timing_error
    stype = timing_fields["schedule_type"]
    cron_expr = timing_fields.get("cron_expr")
    interval_seconds = timing_fields.get("interval_seconds")
    one_shot_at = timing_fields.get("one_shot_at")

    for key, allowed in (
        ("completion_mode_family", _COMPLETION_MODE_FAMILIES),
        ("interaction_mode_override", _INTERACTION_MODE_OVERRIDES),
    ):
        enum_error = _validate_optional_enum_arg(arguments, key, allowed)
        if enum_error is not None:
            return enum_error

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
    if isinstance(arguments.get("session_policy"), dict):
        task_template["session_policy"] = arguments["session_policy"]

    delivery, delivery_error = _resolve_delivery(arguments)
    if delivery_error is not None:
        return delivery_error

    target_agent = arguments.get("agent_id") or agent_id
    if not target_agent:
        return ToolResult(output="'agent_id' is required.", is_error=True)
    try:
        target_agent_profile = normalize_agent_profile_id(arguments.get("agent_profile_id"))
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    async with session_factory() as db:
        validation_error = await _validate_agent_and_workflow(
            db,
            user_email,
            target_agent=target_agent,
            workflow_id=arguments.get("workflow_id"),
        )
        if validation_error is not None:
            return validation_error
        profile_error = await _validate_agent_profile(
            db,
            user_email,
            target_agent=target_agent,
            agent_profile_id=target_agent_profile,
        )
        if profile_error is not None:
            return profile_error
        if delivery["mode"] == "specific_conversation":
            delivery_error = await _validate_delivery_target(
                db, user_email, str(delivery["target"])
            )
            if delivery_error is not None:
                return delivery_error

    task_template["delivery"] = delivery

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
            agent_profile_id=target_agent_profile,
            workflow_id=arguments.get("workflow_id"),
            task_template=task_template,
            enabled=arguments.get("enabled", True),
            max_concurrent_runs=arguments.get("max_concurrent_runs", 1),
            delete_after_run=arguments.get("delete_after_run", False),
            completion_mode_family=arguments.get("completion_mode_family", "default"),
            allow_silent_completion=arguments.get("allow_silent_completion", False),
            interaction_mode_override=arguments.get("interaction_mode_override", "none"),
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
        for key, allowed in (
            ("schedule_type", _SCHEDULE_TYPES),
            ("completion_mode_family", _COMPLETION_MODE_FAMILIES),
            ("interaction_mode_override", _INTERACTION_MODE_OVERRIDES),
        ):
            enum_error = _validate_optional_enum_arg(arguments, key, allowed)
            if enum_error is not None:
                return enum_error
        for key in (
            "name",
            "description",
            "timezone",
            "agent_id",
            "agent_profile_id",
            "workflow_id",
            "enabled",
            "max_concurrent_runs",
            "delete_after_run",
            "completion_mode_family",
            "allow_silent_completion",
            "interaction_mode_override",
        ):
            if key == "agent_profile_id":
                if key in arguments:
                    try:
                        fields[key] = normalize_agent_profile_id(arguments[key])
                    except ValueError as exc:
                        return ToolResult(output=str(exc), is_error=True)
            elif key in arguments and arguments[key] is not None:
                fields[key] = arguments[key]

        timing_keys = {"schedule_type", "cron_expr", "interval_seconds", "one_shot_at"}
        if timing_keys & arguments.keys():
            timing_fields, timing_error = _resolve_timing_fields(arguments, existing=existing)
            if timing_error is not None:
                return timing_error
            fields.update(timing_fields)

        validation_error = await _validate_agent_and_workflow(
            db,
            user_email,
            target_agent=fields.get("agent_id"),
            workflow_id=fields.get("workflow_id"),
        )
        if validation_error is not None:
            return validation_error
        if "agent_id" in fields and "agent_profile_id" not in fields:
            fields["agent_profile_id"] = None
        effective_agent_id = fields.get("agent_id", existing.agent_id)
        profile_error = await _validate_agent_profile(
            db,
            user_email,
            target_agent=effective_agent_id,
            agent_profile_id=fields.get("agent_profile_id"),
        )
        if profile_error is not None:
            return profile_error

        template_keys = {
            "task_title",
            "task_description",
            "task_priority",
            "delivery_mode",
            "delivery_target",
            "expected_output",
            "session_policy",
        }
        if template_keys & arguments.keys():
            template = dict(existing.task_template)
            if "task_title" in arguments and arguments["task_title"] is not None:
                template["title"] = arguments["task_title"]
            if "task_description" in arguments and arguments["task_description"] is not None:
                template["description"] = arguments["task_description"]
            if arguments.get("task_priority") is not None:
                template["priority"] = arguments["task_priority"]
            if arguments.get("expected_output") is not None:
                template["expected_output"] = arguments["expected_output"]
            if "session_policy" in arguments:
                if arguments["session_policy"] is None:
                    template.pop("session_policy", None)
                elif isinstance(arguments["session_policy"], dict):
                    template["session_policy"] = arguments["session_policy"]
            if arguments.get("delivery_mode") or arguments.get("delivery_target") is not None:
                existing_delivery = template.get("delivery")
                if not isinstance(existing_delivery, dict):
                    existing_delivery = {}
                delivery, delivery_error = _resolve_delivery(arguments, existing=existing_delivery)
                if delivery_error is not None:
                    return delivery_error
                if delivery["mode"] == "specific_conversation":
                    delivery_error = await _validate_delivery_target(
                        db,
                        user_email,
                        str(delivery["target"]),
                    )
                    if delivery_error is not None:
                        return delivery_error
                template["delivery"] = delivery
            fields["task_template"] = template

        if not fields:
            return ToolResult(output="No fields to update.")

        if _requires_next_fire_recompute(fields):
            fields["next_fire_at"] = _compute_next_fire_for_update(existing, fields)

        row = await update_schedule(db, schedule_id, **fields)
        active_tasks = await count_active_tasks_for_schedule(db, schedule_id)
        output = _schedule_definition_payload(row or existing, active_tasks=active_tasks)
        await db.commit()

    if scheduler is not None:
        await scheduler.notify_schedule_changed(schedule_id)

    return ToolResult(output=json.dumps(output, indent=2))


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

    info = _schedule_definition_payload(row, active_tasks=active_tasks)
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
        agent_profile_id=getattr(row, "agent_profile_id", None),
        workflow_id=row.workflow_id,
        project_id=getattr(row, "project_id", None),
        skill_id=getattr(row, "skill_id", None),
        task_template=row.task_template,
        enabled=row.enabled,
        max_concurrent_runs=row.max_concurrent_runs,
        delete_after_run=row.delete_after_run,
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family=getattr(row, "completion_mode_family", "default"),
            allow_silent_completion=bool(getattr(row, "allow_silent_completion", False)),
        ),
        session_policy=SessionPolicy.model_validate(
            (row.task_template or {}).get("session_policy") or {}
        ),
        last_fired_at=row.last_fired_at,
        next_fire_at=row.next_fire_at,
        last_run_status=row.last_run_status,
        consecutive_errors=row.consecutive_errors,
        disabled_reason=row.disabled_reason,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _count_active_tasks_safe(session_factory: Any, schedule_id: str) -> int:
    """Return active task count for list output without failing the whole listing."""
    async with session_factory() as db:
        return await count_active_tasks_for_schedule(db, schedule_id)


def _schedule_definition_payload(row: Any, *, active_tasks: int | None = None) -> dict[str, Any]:
    """Serialize the full persisted schedule definition for safe LLM edits."""
    template = row.task_template if isinstance(row.task_template, dict) else {}
    delivery = template.get("delivery") if isinstance(template.get("delivery"), dict) else {}
    model = _row_to_model(row)
    return {
        "schedule_id": row.schedule_id,
        "name": row.name,
        "description": row.description,
        "schedule_type": row.schedule_type,
        "type": row.schedule_type,
        "cron_expr": row.cron_expr,
        "interval_seconds": row.interval_seconds,
        "one_shot_at": _iso(row.one_shot_at),
        "timezone": row.timezone,
        "human_schedule": describe_schedule(model),
        "schedule": describe_schedule(model),
        "agent_id": row.agent_id,
        "agent_profile_id": getattr(row, "agent_profile_id", None),
        "workflow_id": row.workflow_id,
        "project_id": getattr(row, "project_id", None),
        "skill_id": getattr(row, "skill_id", None),
        "task_title": template.get("title"),
        "task_description": template.get("description"),
        "task_priority": template.get("priority"),
        "expected_output": template.get("expected_output"),
        "delivery_mode": delivery.get("mode"),
        "delivery_target": delivery.get("target"),
        "delivery": _redact_sensitive(delivery),
        "completion_mode_family": getattr(row, "completion_mode_family", "default"),
        "allow_silent_completion": bool(getattr(row, "allow_silent_completion", False)),
        "enabled": row.enabled,
        "max_concurrent_runs": row.max_concurrent_runs,
        "delete_after_run": row.delete_after_run,
        "interaction_mode_override": getattr(row, "interaction_mode_override", "none"),
        "session_policy": template.get("session_policy") or {},
        "last_fired_at": _iso(row.last_fired_at),
        "next_fire_at": _iso(row.next_fire_at),
        "last_run_status": row.last_run_status,
        "consecutive_errors": row.consecutive_errors,
        "disabled_reason": row.disabled_reason,
        "active_tasks": active_tasks,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "task_template": _redact_sensitive(template),
    }


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _parse_datetime_arg(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _resolve_timing_fields(
    arguments: dict[str, Any],
    *,
    existing: Any | None = None,
) -> tuple[dict[str, Any], ToolResult | None]:
    current_type = getattr(existing, "schedule_type", None) if existing is not None else None
    stype = arguments.get("schedule_type") or current_type or "cron"
    enum_error = _validate_enum_value("schedule_type", stype, _SCHEDULE_TYPES)
    if enum_error is not None:
        return {}, enum_error

    cron_expr = arguments.get("cron_expr")
    if cron_expr is None and existing is not None and stype == current_type:
        cron_expr = getattr(existing, "cron_expr", None)

    interval_seconds = arguments.get("interval_seconds")
    if interval_seconds is None and existing is not None and stype == current_type:
        interval_seconds = getattr(existing, "interval_seconds", None)

    one_shot_at_value = arguments.get("one_shot_at")
    if one_shot_at_value is None and existing is not None and stype == current_type:
        one_shot_at_value = getattr(existing, "one_shot_at", None)

    fields: dict[str, Any] = {"schedule_type": stype}
    if stype == "cron":
        if not cron_expr:
            return {}, ToolResult(
                output="'cron_expr' is required for cron schedules.", is_error=True
            )
        try:
            from croniter import croniter

            croniter(cron_expr)
        except (ValueError, KeyError) as exc:
            return {}, ToolResult(output=f"Invalid cron expression: {exc}", is_error=True)
        fields.update(
            {
                "cron_expr": cron_expr,
                "interval_seconds": None,
                "one_shot_at": None,
            }
        )
        return fields, None

    if stype == "interval":
        if not interval_seconds:
            return {}, ToolResult(
                output="'interval_seconds' is required for interval schedules.", is_error=True
            )
        fields.update(
            {
                "cron_expr": None,
                "interval_seconds": interval_seconds,
                "one_shot_at": None,
            }
        )
        return fields, None

    if stype == "one_shot":
        if not one_shot_at_value:
            return {}, ToolResult(
                output="'one_shot_at' is required for one_shot schedules.", is_error=True
            )
        one_shot_at = _parse_datetime_arg(one_shot_at_value)
        if one_shot_at is None:
            return {}, ToolResult(output=f"Invalid one_shot_at: {one_shot_at_value}", is_error=True)
        fields.update(
            {
                "cron_expr": None,
                "interval_seconds": None,
                "one_shot_at": one_shot_at,
            }
        )
        return fields, None

    return {}, ToolResult(output=f"Invalid schedule_type: {stype!r}.", is_error=True)


async def _validate_delivery_target(
    db: Any, user_email: str, conversation_id: str
) -> ToolResult | None:
    conversation = await get_conversation(db, conversation_id)
    if conversation is None or conversation.user_email != user_email:
        return ToolResult(output=f"Conversation {conversation_id} not found.", is_error=True)
    return None


async def _validate_agent_profile(
    db: Any,
    user_email: str,
    *,
    target_agent: str | None,
    agent_profile_id: str | None,
) -> ToolResult | None:
    if agent_profile_id is None:
        return None
    if not target_agent:
        return ToolResult(
            output="'agent_id' is required when agent_profile_id is set.", is_error=True
        )
    visible_agents = await list_visible_agents(db, user_email)
    agent_row = next(
        (
            row
            for row, _grant in visible_agents
            if row.agent_id == target_agent and getattr(row, "status", "active") == "active"
        ),
        None,
    )
    if agent_row is None:
        return ToolResult(
            output=f"Agent {target_agent} not found or not available to this user.",
            is_error=True,
        )
    agent_definition = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
    try:
        resolve_agent_profile(agent_definition, agent_profile_id, source="manage_schedules")
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    return None


async def _validate_agent_and_workflow(
    db: Any,
    user_email: str,
    *,
    target_agent: str | None,
    workflow_id: str | None,
) -> ToolResult | None:
    if target_agent is not None:
        visible_agents = await list_visible_agents(db, user_email)
        if target_agent not in {
            row.agent_id
            for row, _grant in visible_agents
            if getattr(row, "status", "active") == "active"
        }:
            return ToolResult(
                output=f"Agent {target_agent} not found or not available to this user.",
                is_error=True,
            )

    if workflow_id is None:
        return None
    if workflow_id in SYSTEM_WORKFLOWS:
        return None

    workflows = await list_workflows(db, owner_email=user_email, include_system=False)
    if workflow_id not in {row.workflow_id for row in workflows}:
        return ToolResult(
            output=f"Workflow {workflow_id} not found or not available to this user.",
            is_error=True,
        )
    return None


def _resolve_delivery(
    arguments: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ToolResult | None]:
    current = existing if isinstance(existing, dict) else {}
    mode = arguments.get("delivery_mode") or current.get("mode") or "preferred_channel"
    enum_error = _validate_enum_value("delivery_mode", mode, _DELIVERY_MODES)
    if enum_error is not None:
        return {}, enum_error

    if mode == "specific_conversation":
        target = arguments.get("delivery_target")
        if target is None:
            target = current.get("target")
        if not target:
            return {}, ToolResult(
                output="'delivery_target' is required when delivery_mode is specific_conversation.",
                is_error=True,
            )
        return {"mode": mode, "target": target}, None

    return {"mode": mode, "target": None}, None


def _validate_optional_enum_arg(
    arguments: dict[str, Any],
    field_name: str,
    allowed: tuple[str, ...],
) -> ToolResult | None:
    if field_name not in arguments or arguments[field_name] is None:
        return None
    return _validate_enum_value(field_name, arguments[field_name], allowed)


def _validate_enum_value(
    field_name: str,
    value: Any,
    allowed: tuple[str, ...],
) -> ToolResult | None:
    if value in allowed:
        return None
    return ToolResult(
        output=f"Invalid {field_name}: {value!r}. Use one of: {', '.join(allowed)}.",
        is_error=True,
    )


def _requires_next_fire_recompute(fields: dict[str, Any]) -> bool:
    timing_keys = {
        "schedule_type",
        "cron_expr",
        "interval_seconds",
        "one_shot_at",
        "timezone",
        "enabled",
    }
    return bool(timing_keys & fields.keys())


def _compute_next_fire_for_update(existing: Any, fields: dict[str, Any]) -> datetime | None:
    if fields.get("enabled", existing.enabled) is False:
        return None

    from cognis.core.scheduler import Scheduler

    schedule_type = fields.get("schedule_type", existing.schedule_type)
    last_fired_at = existing.last_fired_at
    if schedule_type == "one_shot" and (
        fields.get("schedule_type") == "one_shot" or "one_shot_at" in fields
    ):
        last_fired_at = None

    temp = type(
        "_S",
        (),
        {
            "schedule_type": schedule_type,
            "cron_expr": fields.get("cron_expr", existing.cron_expr),
            "interval_seconds": fields.get("interval_seconds", existing.interval_seconds),
            "one_shot_at": fields.get("one_shot_at", existing.one_shot_at),
            "timezone": fields.get("timezone", existing.timezone),
            "last_fired_at": last_fired_at,
        },
    )()
    sched_inst = Scheduler.__new__(Scheduler)
    return sched_inst._compute_next_fire(temp, datetime.now(UTC))


_SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "auth_header",
    "bearer",
    "cookie",
    "headers",
    "session",
)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if any(part in key_str.lower() for part in _SENSITIVE_KEY_PARTS):
                redacted[key_str] = "[redacted]"
            else:
                redacted[key_str] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value

"""Shared schedule management helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from cognis.api.models import CreateScheduleRequest
from cognis.models.workflow import CompletionDeliveryPolicy
from cognis.store.queries import create_schedule, get_agent, get_schedule


async def create_user_schedule(
    *,
    session_factory: Any,
    scheduler: Any,
    workflow_registry: Any,
    owner_email: str,
    payload: dict[str, Any],
) -> Any:
    """Create a user-owned schedule after validation."""

    try:
        request_model = CreateScheduleRequest.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc

    async with session_factory() as db:
        agent = await get_agent(db, request_model.agent_id)
        if agent is None or agent.owner_email != owner_email:
            raise ValueError("Agent not found")

    workflow_id = request_model.workflow_id
    if isinstance(workflow_id, str) and workflow_id.strip():
        workflow = await workflow_registry.get(workflow_id, owner_email=owner_email, include_disabled=True)
        if workflow is None:
            raise ValueError("Workflow not found")
        if str(getattr(workflow, "lifecycle", "persistent")) != "persistent":
            raise ValueError("Schedules require a persistent workflow")

    if request_model.schedule_type == "cron" and request_model.cron_expr:
        try:
            from croniter import croniter

            croniter(str(request_model.cron_expr))
        except (ValueError, KeyError) as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc

    now = datetime.now(UTC)
    temp_sched = type(
        "_S",
        (),
        {
            "schedule_type": request_model.schedule_type,
            "cron_expr": request_model.cron_expr,
            "interval_seconds": request_model.interval_seconds,
            "one_shot_at": request_model.one_shot_at,
            "timezone": request_model.timezone,
            "last_fired_at": None,
        },
    )()
    scheduler_instance = scheduler
    if scheduler_instance is None:
        from cognis.core.scheduler import Scheduler

        scheduler_instance = Scheduler.__new__(Scheduler)
    next_fire = scheduler_instance._compute_next_fire(temp_sched, now)

    CompletionDeliveryPolicy(
        completion_mode_family=request_model.completion_mode_family,
        allow_silent_completion=bool(request_model.allow_silent_completion),
    )

    async with session_factory() as db:
        row = await create_schedule(
            db,
            name=request_model.name,
            description=request_model.description,
            schedule_type=request_model.schedule_type,
            cron_expr=request_model.cron_expr,
            interval_seconds=request_model.interval_seconds,
            one_shot_at=request_model.one_shot_at,
            timezone=request_model.timezone,
            agent_id=request_model.agent_id,
            workflow_id=request_model.workflow_id,
            task_template=request_model.task_template,
            enabled=bool(request_model.enabled),
            max_concurrent_runs=int(request_model.max_concurrent_runs),
            delete_after_run=bool(request_model.delete_after_run),
            completion_mode_family=request_model.completion_mode_family,
            allow_silent_completion=bool(request_model.allow_silent_completion),
            interaction_mode_override=request_model.interaction_mode_override,
            next_fire_at=next_fire,
            created_by=owner_email,
        )
        await db.commit()
        row = await get_schedule(db, row.schedule_id)

    if scheduler is not None and row is not None:
        await scheduler.notify_schedule_changed(row.schedule_id)
    return row

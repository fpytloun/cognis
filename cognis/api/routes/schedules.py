"""Schedule CRUD and trigger routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.api.models import (
    CreateScheduleRequest,
    ScheduleResponse,
    UpdateScheduleRequest,
)
from cognis.models.schedule import ScheduleModel as _ScheduleModel
from cognis.models.schedule import describe_schedule
from cognis.models.workflow import CompletionDeliveryPolicy
from cognis.store.queries import (
    create_schedule,
    delete_schedule,
    get_agent,
    get_latest_schedule_task_runs,
    get_schedule,
    list_schedules,
    update_schedule,
)

router = APIRouter(tags=["schedules"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VALID_SCHEDULE_RUN_STATUSES = {"success", "failed", "skipped"}


def _coerce_schedule_run_status(value: Any) -> str | None:
    """Accept the three valid ScheduleRunStatus strings; drop unknown values.

    Used only where the ScheduleRunStatus enum domain model constructor
    refuses anything else. The API response surface intentionally
    returns a free-form string so derived task statuses (``"running"``,
    ``"completed"``, etc.) can be surfaced to the UI without enum drift
    breaking the response validation.
    """

    if isinstance(value, str) and value in _VALID_SCHEDULE_RUN_STATUSES:
        return value
    return None


def _effective_last_run_status(
    row: Any,
    latest_task_run: tuple[str, datetime | None] | None,
) -> str | None:
    """Return the user-visible latest run status for a schedule."""

    if latest_task_run is None:
        return row.last_run_status

    task_status, task_created_at = latest_task_run
    if row.last_run_status in {"failed", "skipped"} and (
        task_created_at is None
        or (row.last_fired_at is not None and task_created_at < row.last_fired_at)
    ):
        return row.last_run_status
    return task_status


def _row_to_response(
    row: Any,
    latest_task_run: tuple[str, datetime | None] | None = None,
) -> ScheduleResponse:
    """Convert a Schedule ORM row to an API response."""
    model = _ScheduleModel(
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
        last_fired_at=row.last_fired_at,
        next_fire_at=row.next_fire_at,
        last_run_status=_coerce_schedule_run_status(row.last_run_status),
        consecutive_errors=row.consecutive_errors,
        disabled_reason=row.disabled_reason,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    return ScheduleResponse(
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
        completion_mode_family=getattr(row, "completion_mode_family", "default"),
        allow_silent_completion=bool(getattr(row, "allow_silent_completion", False)),
        last_fired_at=row.last_fired_at,
        next_fire_at=row.next_fire_at,
        last_run_status=_effective_last_run_status(row, latest_task_run),
        consecutive_errors=row.consecutive_errors,
        disabled_reason=row.disabled_reason,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        human_schedule=describe_schedule(model),
    )


async def _load_latest_task_run(
    request: Request,
    schedule_id: str,
    *,
    created_by: str,
) -> tuple[str, datetime | None] | None:
    """Load the latest scheduler-created task state for a schedule."""
    async with request.app.state.session_factory() as db:
        latest_runs = await get_latest_schedule_task_runs(
            db,
            [schedule_id],
            created_by=created_by,
        )
    return latest_runs.get(schedule_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/v1/schedules", response_model=list[ScheduleResponse])
async def list_schedules_route(
    request: Request,
    enabled: bool | None = Query(None),
    schedule_type: str | None = Query(None),
    agent_id: str | None = Query(None),
) -> list[ScheduleResponse]:
    """List schedules for the current user."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as db:
        rows = await list_schedules(
            db,
            created_by=user.email,
            enabled=enabled,
            schedule_type=schedule_type,
            agent_id=agent_id,
        )
        latest_runs = await get_latest_schedule_task_runs(
            db,
            [row.schedule_id for row in rows],
            created_by=user.email,
        )
    return [_row_to_response(r, latest_runs.get(r.schedule_id)) for r in rows]


@router.post("/api/v1/schedules", response_model=ScheduleResponse, status_code=201)
async def create_schedule_route(
    request: Request,
    body: CreateScheduleRequest,
) -> ScheduleResponse:
    """Create a new schedule."""
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)

    # Validate agent exists and belongs to user
    async with request.app.state.session_factory() as db:
        agent = await get_agent(db, body.agent_id)
        if agent is None or agent.owner_email != user.email:
            raise api_exception(404, "agent_not_found", "Agent not found")

    # Validate cron expression
    if body.schedule_type == "cron" and body.cron_expr:
        try:
            from croniter import croniter

            croniter(body.cron_expr)
        except (ValueError, KeyError) as exc:
            raise api_exception(400, "invalid_cron", f"Invalid cron expression: {exc}") from exc

    # Compute initial next_fire_at
    from cognis.core.scheduler import Scheduler

    now = datetime.now(UTC)
    # Build a temporary model to compute next fire
    temp_sched = type(
        "_S",
        (),
        {
            "schedule_type": body.schedule_type,
            "cron_expr": body.cron_expr,
            "interval_seconds": body.interval_seconds,
            "one_shot_at": body.one_shot_at,
            "timezone": body.timezone,
            "last_fired_at": None,
        },
    )()
    scheduler_instance = Scheduler.__new__(Scheduler)
    next_fire = scheduler_instance._compute_next_fire(temp_sched, now)
    CompletionDeliveryPolicy(
        completion_mode_family=body.completion_mode_family,
        allow_silent_completion=body.allow_silent_completion,
    )

    async with request.app.state.session_factory() as db:
        row = await create_schedule(
            db,
            name=body.name,
            description=body.description,
            schedule_type=body.schedule_type,
            cron_expr=body.cron_expr,
            interval_seconds=body.interval_seconds,
            one_shot_at=body.one_shot_at,
            timezone=body.timezone,
            agent_id=body.agent_id,
            workflow_id=body.workflow_id,
            task_template=body.task_template,
            enabled=body.enabled,
            max_concurrent_runs=body.max_concurrent_runs,
            delete_after_run=body.delete_after_run,
            completion_mode_family=body.completion_mode_family,
            allow_silent_completion=body.allow_silent_completion,
            next_fire_at=next_fire,
            created_by=user.email,
        )
        await db.commit()
        # Re-read to get all defaults
        row = await get_schedule(db, row.schedule_id)

    # Notify scheduler
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        await scheduler.notify_schedule_changed(row.schedule_id)

    return _row_to_response(
        row,
        await _load_latest_task_run(request, row.schedule_id, created_by=user.email),
    )


@router.get("/api/v1/schedules/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule_route(
    request: Request,
    schedule_id: str,
) -> ScheduleResponse:
    """Get a schedule by ID."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as db:
        row = await get_schedule(db, schedule_id)
        latest_run = await get_latest_schedule_task_runs(
            db,
            [schedule_id],
            created_by=user.email,
        )
    if row is None or row.created_by != user.email:
        raise api_exception(404, "schedule_not_found", "Schedule not found")
    return _row_to_response(row, latest_run.get(schedule_id))


@router.put("/api/v1/schedules/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule_route(
    request: Request,
    schedule_id: str,
    body: UpdateScheduleRequest,
) -> ScheduleResponse:
    """Update a schedule."""
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)

    async with request.app.state.session_factory() as db:
        existing = await get_schedule(db, schedule_id)
        if existing is None or existing.created_by != user.email:
            raise api_exception(404, "schedule_not_found", "Schedule not found")

        # Validate agent if changing
        if body.agent_id is not None:
            agent = await get_agent(db, body.agent_id)
            if agent is None or agent.owner_email != user.email:
                raise api_exception(404, "agent_not_found", "Agent not found")

        # Validate cron expression if changing
        if body.cron_expr is not None:
            try:
                from croniter import croniter

                croniter(body.cron_expr)
            except (ValueError, KeyError) as exc:
                raise api_exception(400, "invalid_cron", f"Invalid cron expression: {exc}") from exc

        fields = body.model_dump(exclude_unset=True)
        if "completion_mode_family" in fields or "allow_silent_completion" in fields:
            CompletionDeliveryPolicy(
                completion_mode_family=fields.get(
                    "completion_mode_family", getattr(existing, "completion_mode_family", "default")
                ),
                allow_silent_completion=fields.get(
                    "allow_silent_completion",
                    bool(getattr(existing, "allow_silent_completion", False)),
                ),
            )
        if not fields:
            return _row_to_response(
                existing,
                await _load_latest_task_run(request, schedule_id, created_by=user.email),
            )

        row = await update_schedule(db, schedule_id, **fields)
        await db.commit()

    # Notify scheduler to recompute next_fire_at
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        await scheduler.notify_schedule_changed(schedule_id)

    return _row_to_response(
        row,
        await _load_latest_task_run(request, schedule_id, created_by=user.email),
    )


@router.delete("/api/v1/schedules/{schedule_id}", status_code=204)
async def delete_schedule_route(
    request: Request,
    schedule_id: str,
) -> None:
    """Delete a schedule."""
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)

    async with request.app.state.session_factory() as db:
        existing = await get_schedule(db, schedule_id)
        if existing is None or existing.created_by != user.email:
            raise api_exception(404, "schedule_not_found", "Schedule not found")
        await delete_schedule(db, schedule_id)
        await db.commit()

    # Notify scheduler
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.wake()


@router.post("/api/v1/schedules/{schedule_id}/trigger", response_model=ScheduleResponse)
async def trigger_schedule_route(
    request: Request,
    schedule_id: str,
) -> ScheduleResponse:
    """Fire a schedule immediately (create a task now)."""
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)

    async with request.app.state.session_factory() as db:
        row = await get_schedule(db, schedule_id)
        if row is None or row.created_by != user.email:
            raise api_exception(404, "schedule_not_found", "Schedule not found")

    # Use the scheduler to fire it
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        await scheduler._fire_schedule(schedule_id)
    else:
        raise api_exception(503, "scheduler_unavailable", "Scheduler is not running")

    # Re-read updated state
    async with request.app.state.session_factory() as db:
        row = await get_schedule(db, schedule_id)
    if row is None:
        raise api_exception(404, "schedule_not_found", "Schedule was deleted after trigger")
    async with request.app.state.session_factory() as db:
        latest_run = await get_latest_schedule_task_runs(
            db,
            [schedule_id],
            created_by=user.email,
        )
    return _row_to_response(row, latest_run.get(schedule_id))


@router.post("/api/v1/schedules/{schedule_id}/enable", response_model=ScheduleResponse)
async def enable_schedule_route(
    request: Request,
    schedule_id: str,
) -> ScheduleResponse:
    """Enable a schedule."""
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)

    async with request.app.state.session_factory() as db:
        existing = await get_schedule(db, schedule_id)
        if existing is None or existing.created_by != user.email:
            raise api_exception(404, "schedule_not_found", "Schedule not found")
        row = await update_schedule(
            db,
            schedule_id,
            enabled=True,
            disabled_reason=None,
            consecutive_errors=0,
        )
        await db.commit()

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        await scheduler.notify_schedule_changed(schedule_id)

    return _row_to_response(
        row,
        await _load_latest_task_run(request, schedule_id, created_by=user.email),
    )


@router.post("/api/v1/schedules/{schedule_id}/disable", response_model=ScheduleResponse)
async def disable_schedule_route(
    request: Request,
    schedule_id: str,
) -> ScheduleResponse:
    """Disable a schedule."""
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)

    async with request.app.state.session_factory() as db:
        existing = await get_schedule(db, schedule_id)
        if existing is None or existing.created_by != user.email:
            raise api_exception(404, "schedule_not_found", "Schedule not found")
        row = await update_schedule(db, schedule_id, enabled=False)
        await db.commit()

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        await scheduler.notify_schedule_changed(schedule_id)

    return _row_to_response(
        row,
        await _load_latest_task_run(request, schedule_id, created_by=user.email),
    )


@router.get(
    "/api/v1/schedules/{schedule_id}/runs",
    response_model=list[dict[str, Any]],
)
async def list_schedule_runs_route(
    request: Request,
    schedule_id: str,
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List recent tasks created by this schedule (run history)."""
    user = require_current_user(request)

    async with request.app.state.session_factory() as db:
        existing = await get_schedule(db, schedule_id)
        if existing is None or existing.created_by != user.email:
            raise api_exception(404, "schedule_not_found", "Schedule not found")

        from sqlalchemy import select

        from cognis.store.models import Task

        stmt = (
            select(Task)
            .where(Task.source_type == "scheduler", Task.source_ref == schedule_id)
            .order_by(Task.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        tasks = list(result.scalars().all())

    return [
        {
            "task_id": t.task_id,
            "title": t.title,
            "status": t.status,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "result_summary": t.result_summary,
        }
        for t in tasks
    ]

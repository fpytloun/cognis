"""Database-authoritative schedule fire identity and reconciliation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.store.coordination import Lease, database_now, database_now_expression
from cognis.store.models import (
    CoordinationLeaseRow,
    Schedule,
    ScheduleCatchupStateRow,
    ScheduleFireRow,
    Task,
)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    return _utc_datetime(value).isoformat()


def schedule_fire_id(schedule_id: str, scheduled_fire_at: datetime) -> str:
    normalized = _utc_iso(scheduled_fire_at)
    digest = hashlib.sha256(f"{schedule_id}\0{normalized}".encode()).hexdigest()[:32]
    return f"sfire_{digest}"


def schedule_task_id(schedule_id: str, scheduled_fire_at: datetime) -> str:
    normalized = _utc_iso(scheduled_fire_at)
    digest = hashlib.sha256(f"task\0{schedule_id}\0{normalized}".encode()).hexdigest()[:32]
    return f"task_sched_{digest}"


def manual_schedule_fire_id(schedule_id: str, scheduled_fire_at: datetime) -> str:
    normalized = _utc_iso(scheduled_fire_at)
    digest = hashlib.sha256(f"manual\0{schedule_id}\0{normalized}".encode()).hexdigest()[:32]
    return f"sfire_manual_{digest}"


def manual_schedule_task_id(schedule_id: str, scheduled_fire_at: datetime) -> str:
    normalized = _utc_iso(scheduled_fire_at)
    digest = hashlib.sha256(f"manual-task\0{schedule_id}\0{normalized}".encode()).hexdigest()[:32]
    return f"task_sched_{digest}"


@dataclass(frozen=True)
class ScheduleFireClaim:
    fire_id: str
    schedule_id: str
    scheduled_fire_at: datetime
    task_id: str
    status: str
    should_dispatch: bool
    schedule: Any


class ScheduleFireStore:
    """Mutate schedule fire state only while holding its per-schedule lease."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(
        self,
        *,
        schedule_id: str,
        scheduled_fire_at: datetime,
        lease: Lease,
    ) -> ScheduleFireClaim | None:
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return None
            schedule_stmt = select(Schedule).where(Schedule.schedule_id == schedule_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                schedule_stmt = schedule_stmt.with_for_update()
            schedule = (await session.execute(schedule_stmt)).scalar_one_or_none()
            if (
                schedule is None
                or not schedule.enabled
                or schedule.next_fire_at != scheduled_fire_at
            ):
                await session.rollback()
                return None

            fire_id = schedule_fire_id(schedule_id, scheduled_fire_at)
            task_id = schedule_task_id(schedule_id, scheduled_fire_at)
            now = database_now_expression(session)
            dialect = session.bind.dialect.name if session.bind is not None else ""
            insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
            await session.execute(
                insert(ScheduleFireRow)
                .values(
                    fire_id=fire_id,
                    schedule_id=schedule_id,
                    fire_kind="recurring",
                    scheduled_fire_at=scheduled_fire_at,
                    task_id=None,
                    status="claimed",
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ScheduleFireRow.schedule_id,
                        ScheduleFireRow.fire_kind,
                        ScheduleFireRow.scheduled_fire_at,
                    ]
                )
            )
            fire_stmt = select(ScheduleFireRow).where(
                ScheduleFireRow.fire_id == fire_id,
                ScheduleFireRow.fire_kind == "recurring",
            )
            if dialect == "postgresql":
                fire_stmt = fire_stmt.with_for_update()
            fire = (await session.execute(fire_stmt)).scalar_one_or_none()
            if fire is None:
                await session.rollback()
                return None
            if fire.status in {"dispatched", "skipped"}:
                await session.commit()
                return ScheduleFireClaim(
                    fire.fire_id,
                    schedule_id,
                    scheduled_fire_at,
                    fire.task_id or task_id,
                    fire.status,
                    False,
                    schedule,
                )

            existing_task = await session.get(Task, task_id)
            if existing_task is not None:
                fire.status = "claimed"
                fire.task_id = task_id
                fire.attempt_count += 1
                fire.last_error = None
                fire.updated_at = datetime.now(UTC)
                await session.commit()
                return ScheduleFireClaim(
                    fire.fire_id,
                    schedule_id,
                    scheduled_fire_at,
                    task_id,
                    "claimed",
                    True,
                    schedule,
                )

            active = int(
                await session.scalar(
                    select(func.count(Task.task_id)).where(
                        Task.source_type == "scheduler",
                        Task.source_ref == schedule_id,
                        Task.status.in_(["queued", "ready", "running", "paused"]),
                    )
                )
                or 0
            )
            if active >= schedule.max_concurrent_runs:
                fire.status = "skipped"
                fire.updated_at = datetime.now(UTC)
                await session.commit()
                return ScheduleFireClaim(
                    fire.fire_id,
                    schedule_id,
                    scheduled_fire_at,
                    task_id,
                    "skipped",
                    False,
                    schedule,
                )

            fire.status = "claimed"
            fire.attempt_count += 1
            fire.last_error = None
            fire.updated_at = datetime.now(UTC)
            await session.commit()
            return ScheduleFireClaim(
                fire.fire_id,
                schedule_id,
                scheduled_fire_at,
                task_id,
                "claimed",
                True,
                schedule,
            )

    async def claim_manual(
        self,
        *,
        schedule_id: str,
        lease: Lease,
    ) -> ScheduleFireClaim | None:
        """Claim an immediate fire without changing the recurring fire state."""
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return None
            dialect = session.bind.dialect.name if session.bind is not None else ""
            schedule_stmt = select(Schedule).where(Schedule.schedule_id == schedule_id)
            if dialect == "postgresql":
                schedule_stmt = schedule_stmt.with_for_update()
            schedule = (await session.execute(schedule_stmt)).scalar_one_or_none()
            if schedule is None or not schedule.enabled:
                await session.rollback()
                return None

            if schedule.schedule_type == "one_shot" and schedule.next_fire_at is None:
                dispatched_stmt = (
                    select(ScheduleFireRow)
                    .where(
                        ScheduleFireRow.schedule_id == schedule_id,
                        ScheduleFireRow.fire_kind == "manual",
                        ScheduleFireRow.status == "dispatched",
                        ScheduleFireRow.task_id.is_not(None),
                    )
                    .order_by(ScheduleFireRow.dispatched_at.desc())
                    .limit(1)
                )
                dispatched = (await session.execute(dispatched_stmt)).scalar_one_or_none()
                if dispatched is None:
                    await session.rollback()
                    return None
                await session.commit()
                return ScheduleFireClaim(
                    dispatched.fire_id,
                    schedule_id,
                    dispatched.scheduled_fire_at,
                    str(dispatched.task_id),
                    "dispatched",
                    False,
                    schedule,
                )

            pending_stmt = (
                select(ScheduleFireRow)
                .where(
                    ScheduleFireRow.schedule_id == schedule_id,
                    ScheduleFireRow.fire_kind == "manual",
                    ScheduleFireRow.status == "claimed",
                )
                .order_by(ScheduleFireRow.created_at.desc())
                .limit(1)
            )
            if dialect == "postgresql":
                pending_stmt = pending_stmt.with_for_update()
            pending = (await session.execute(pending_stmt)).scalar_one_or_none()
            if pending is not None:
                task_id = pending.task_id or manual_schedule_task_id(
                    schedule_id, pending.scheduled_fire_at
                )
                pending.attempt_count += 1
                pending.last_error = None
                pending.updated_at = database_now_expression(session)
                await session.commit()
                return ScheduleFireClaim(
                    pending.fire_id,
                    schedule_id,
                    pending.scheduled_fire_at,
                    task_id,
                    "claimed",
                    True,
                    schedule,
                )

            scheduled_fire_at = await self._manual_fire_at(
                session,
                schedule_id=schedule_id,
                recurring_fire_at=schedule.next_fire_at,
            )
            fire_id = manual_schedule_fire_id(schedule_id, scheduled_fire_at)
            task_id = manual_schedule_task_id(schedule_id, scheduled_fire_at)
            now = database_now_expression(session)
            fire = ScheduleFireRow(
                fire_id=fire_id,
                schedule_id=schedule_id,
                fire_kind="manual",
                scheduled_fire_at=scheduled_fire_at,
                task_id=None,
                status="claimed",
                attempt_count=0,
                created_at=now,
                updated_at=now,
            )
            session.add(fire)

            active = int(
                await session.scalar(
                    select(func.count(Task.task_id)).where(
                        Task.source_type == "scheduler",
                        Task.source_ref == schedule_id,
                        Task.status.in_(["queued", "ready", "running", "paused"]),
                    )
                )
                or 0
            )
            if active >= schedule.max_concurrent_runs:
                fire.status = "skipped"
                if schedule.schedule_type == "one_shot":
                    if schedule.delete_after_run:
                        await session.delete(schedule)
                    else:
                        schedule.last_fired_at = scheduled_fire_at
                        schedule.next_fire_at = None
                        schedule.last_run_status = "skipped"
                        schedule.updated_at = now
                await session.commit()
                return ScheduleFireClaim(
                    fire_id,
                    schedule_id,
                    scheduled_fire_at,
                    task_id,
                    "skipped",
                    False,
                    schedule,
                )

            fire.attempt_count = 1
            await session.commit()
            return ScheduleFireClaim(
                fire_id,
                schedule_id,
                scheduled_fire_at,
                task_id,
                "claimed",
                True,
                schedule,
            )

    async def is_manual_task(self, task_id: str) -> bool:
        """Return whether a task belongs to a durable manual schedule fire."""
        async with self._session_factory() as session:
            fire_id = await session.scalar(
                select(ScheduleFireRow.fire_id).where(
                    ScheduleFireRow.task_id == task_id,
                    ScheduleFireRow.fire_kind == "manual",
                )
            )
            return fire_id is not None

    async def link_manual_task(
        self,
        *,
        claim: ScheduleFireClaim,
        lease: Lease,
    ) -> bool:
        """Persist manual task identity while the reserved task is not runnable."""
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return False
            result = await session.execute(
                update(ScheduleFireRow)
                .where(
                    ScheduleFireRow.fire_id == claim.fire_id,
                    ScheduleFireRow.fire_kind == "manual",
                    ScheduleFireRow.status == "claimed",
                    ScheduleFireRow.task_id.is_(None),
                )
                .values(
                    task_id=claim.task_id,
                    updated_at=database_now_expression(session),
                )
            )
            if not getattr(result, "rowcount", 0):
                linked_task_id = await session.scalar(
                    select(ScheduleFireRow.task_id).where(
                        ScheduleFireRow.fire_id == claim.fire_id,
                        ScheduleFireRow.fire_kind == "manual",
                        ScheduleFireRow.status == "claimed",
                    )
                )
                if linked_task_id != claim.task_id:
                    await session.rollback()
                    return False
            await session.commit()
            return True

    async def mark_dispatched(
        self,
        *,
        claim: ScheduleFireClaim,
        lease: Lease,
    ) -> bool:
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return False
            now = database_now_expression(session)
            result = await session.execute(
                update(ScheduleFireRow)
                .where(
                    ScheduleFireRow.fire_id == claim.fire_id,
                    ScheduleFireRow.status == "claimed",
                )
                .values(
                    status="dispatched",
                    task_id=claim.task_id,
                    dispatched_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(getattr(result, "rowcount", 0))

    async def settle_manual_dispatched(
        self,
        *,
        claim: ScheduleFireClaim,
        lease: Lease,
        one_shot_status: str,
    ) -> bool:
        """Settle a manual fire and consume a one-shot schedule atomically."""
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return False
            now = database_now_expression(session)
            result = await session.execute(
                update(ScheduleFireRow)
                .where(
                    ScheduleFireRow.fire_id == claim.fire_id,
                    ScheduleFireRow.status == "claimed",
                )
                .values(
                    status="dispatched",
                    task_id=claim.task_id,
                    dispatched_at=now,
                    updated_at=now,
                )
            )
            if not getattr(result, "rowcount", 0):
                await session.rollback()
                return False

            schedule_stmt = select(Schedule).where(Schedule.schedule_id == claim.schedule_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                schedule_stmt = schedule_stmt.with_for_update()
            schedule = (await session.execute(schedule_stmt)).scalar_one_or_none()
            if schedule is not None and schedule.schedule_type == "one_shot":
                if schedule.delete_after_run:
                    await session.delete(schedule)
                else:
                    schedule.last_fired_at = claim.scheduled_fire_at
                    schedule.next_fire_at = None
                    schedule.last_run_status = one_shot_status
                    schedule.updated_at = now
            await session.commit()
            return True

    async def mark_failed(
        self,
        *,
        claim: ScheduleFireClaim,
        lease: Lease,
        error: str,
        next_fire_at: datetime | None,
        consecutive_errors: int,
        disabled_reason: str | None,
    ) -> bool:
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return False
            result = await session.execute(
                update(ScheduleFireRow)
                .where(
                    ScheduleFireRow.fire_id == claim.fire_id,
                    ScheduleFireRow.status == "claimed",
                )
                .values(
                    status="failed",
                    last_error=error,
                    updated_at=database_now_expression(session),
                )
            )
            schedule_values: dict[str, Any] = {
                "last_fired_at": claim.scheduled_fire_at,
                "next_fire_at": next_fire_at,
                "last_run_status": "failed",
                "consecutive_errors": consecutive_errors,
                "disabled_reason": disabled_reason,
                "updated_at": database_now_expression(session),
            }
            if disabled_reason is not None:
                schedule_values["enabled"] = False
            schedule_result = await session.execute(
                update(Schedule)
                .where(
                    Schedule.schedule_id == claim.schedule_id,
                    Schedule.next_fire_at == claim.scheduled_fire_at,
                    Schedule.enabled.is_(True),
                )
                .values(**schedule_values)
            )
            if not getattr(schedule_result, "rowcount", 0):
                await session.rollback()
                return False
            await session.commit()
            return bool(getattr(result, "rowcount", 0))

    async def mark_manual_failed(
        self,
        *,
        claim: ScheduleFireClaim,
        lease: Lease,
        error: str,
    ) -> bool:
        """Settle a manual fire failure without changing schedule cadence or health."""
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return False
            result = await session.execute(
                update(ScheduleFireRow)
                .where(
                    ScheduleFireRow.fire_id == claim.fire_id,
                    ScheduleFireRow.status == "claimed",
                )
                .values(
                    status="failed",
                    last_error=error,
                    updated_at=database_now_expression(session),
                )
            )
            await session.commit()
            return bool(getattr(result, "rowcount", 0))

    async def advance_schedule(
        self,
        *,
        schedule_id: str,
        scheduled_fire_at: datetime,
        next_fire_at: datetime | None,
        status: str,
        consecutive_errors: int,
        lease: Lease,
    ) -> bool:
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return False
            result = await session.execute(
                update(Schedule)
                .where(
                    Schedule.schedule_id == schedule_id,
                    Schedule.next_fire_at == scheduled_fire_at,
                )
                .values(
                    last_fired_at=scheduled_fire_at,
                    next_fire_at=next_fire_at,
                    last_run_status=status,
                    consecutive_errors=consecutive_errors,
                    updated_at=database_now_expression(session),
                )
            )
            await session.commit()
            return bool(getattr(result, "rowcount", 0))

    async def prepare_catchup(
        self,
        *,
        lease: Lease,
        cutoff_at: datetime,
        budget: int,
        start_new: bool = True,
    ) -> tuple[datetime, int] | None:
        """Create a catch-up epoch or continue an interrupted active epoch."""
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return None
            statement = select(ScheduleCatchupStateRow).where(
                ScheduleCatchupStateRow.catchup_id == "startup"
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            state = (await session.execute(statement)).scalar_one_or_none()
            if state is None:
                state = ScheduleCatchupStateRow(
                    catchup_id="startup",
                    cutoff_at=cutoff_at,
                    remaining_budget=budget,
                    status="active",
                    updated_at=datetime.now(UTC),
                )
                session.add(state)
            elif state.status == "completed" and start_new:
                state.cutoff_at = cutoff_at
                state.remaining_budget = budget
                state.status = "active"
                state.updated_at = datetime.now(UTC)
            elif state.status == "completed":
                await session.commit()
                return None
            await session.commit()
            return state.cutoff_at, state.remaining_budget

    async def reserve_catchup_fire(
        self,
        *,
        lease: Lease,
    ) -> tuple[datetime, int] | None:
        """Durably consume one cluster catch-up budget slot."""
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return None
            statement = (
                select(ScheduleCatchupStateRow)
                .where(
                    ScheduleCatchupStateRow.catchup_id == "startup",
                    ScheduleCatchupStateRow.status == "active",
                )
                .with_for_update()
            )
            state = (await session.execute(statement)).scalar_one_or_none()
            if state is None or state.remaining_budget <= 0:
                await session.rollback()
                return None
            state.remaining_budget -= 1
            state.updated_at = datetime.now(UTC)
            await session.commit()
            return state.cutoff_at, state.remaining_budget

    async def restore_catchup_fire(self, *, lease: Lease) -> int | None:
        """Return an unused reservation after a contended fire attempt."""
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return None
            statement = (
                select(ScheduleCatchupStateRow)
                .where(
                    ScheduleCatchupStateRow.catchup_id == "startup",
                    ScheduleCatchupStateRow.status == "active",
                )
                .with_for_update()
            )
            state = (await session.execute(statement)).scalar_one_or_none()
            if state is None:
                await session.rollback()
                return None
            state.remaining_budget += 1
            state.updated_at = datetime.now(UTC)
            await session.commit()
            return state.remaining_budget

    async def complete_catchup(self, *, lease: Lease) -> bool:
        async with self._session_factory() as session:
            if not await self._lock_lease(session, lease):
                await session.rollback()
                return False
            result = await session.execute(
                update(ScheduleCatchupStateRow)
                .where(
                    ScheduleCatchupStateRow.catchup_id == "startup",
                    ScheduleCatchupStateRow.status == "active",
                )
                .values(
                    status="completed",
                    updated_at=database_now_expression(session),
                )
            )
            await session.commit()
            return bool(getattr(result, "rowcount", 0))

    async def catchup_active(self) -> bool:
        async with self._session_factory() as session:
            status = await session.scalar(
                select(ScheduleCatchupStateRow.status).where(
                    ScheduleCatchupStateRow.catchup_id == "startup"
                )
            )
            return status == "active"

    async def _lock_lease(self, session: AsyncSession, lease: Lease) -> bool:
        now = database_now_expression(session)
        statement: Any = (
            select(CoordinationLeaseRow)
            .where(
                CoordinationLeaseRow.resource_key == lease.resource_key,
                CoordinationLeaseRow.owner_id == lease.owner_id,
                CoordinationLeaseRow.fencing_token == lease.fencing_token,
                CoordinationLeaseRow.lease_expires_at > now,
            )
            .with_for_update()
        )
        return (await session.execute(statement)).scalar_one_or_none() is not None

    @staticmethod
    async def _manual_fire_at(
        session: AsyncSession,
        *,
        schedule_id: str,
        recurring_fire_at: datetime | None,
    ) -> datetime:
        """Create a UTC manual identity outside the recurring timestamp namespace."""
        candidate = await database_now(session)
        if candidate.microsecond == 0:
            candidate += timedelta(microseconds=1)
        collision_times = {
            _utc_datetime(value)
            for value in (
                await session.scalars(
                    select(ScheduleFireRow.scheduled_fire_at).where(
                        ScheduleFireRow.schedule_id == schedule_id
                    )
                )
            ).all()
        }
        candidate = _utc_datetime(candidate)
        if recurring_fire_at is not None:
            collision_times.add(_utc_datetime(recurring_fire_at))
        while candidate in collision_times:
            candidate += timedelta(microseconds=1)
        return candidate

"""Database-authoritative workflow task ownership and capacity fencing."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.store.coordination import (
    Lease,
    _lease_expiry_expression,
    database_now_expression,
)
from cognis.store.models import CoordinationLeaseRow, Task

TASK_LEASE_TTL_SECONDS = 60.0
TASK_LEASE_RENEW_SECONDS = 15.0


class StaleTaskExecutionOwner(RuntimeError):
    """Raised when a workflow task mutation has lost its ownership fence."""


@dataclass(frozen=True)
class TaskExecutionClaim:
    """Durable task ownership plus the scalar identity required for renewal.

    ORM rows are session-scoped and must not be retained by a claim: task
    execution outlives the claiming database session in HA mode.
    """

    task_id: str
    agent_id: str
    task_lease: Lease
    global_capacity_lease: Lease | None
    agent_capacity_lease: Lease | None

    @property
    def leases(self) -> tuple[Lease, ...]:
        capacity = (
            lease
            for lease in (self.global_capacity_lease, self.agent_capacity_lease)
            if lease is not None
        )
        return (self.task_lease, *capacity)

    @property
    def has_capacity(self) -> bool:
        return self.global_capacity_lease is not None and self.agent_capacity_lease is not None


def task_lease_key(task_id: str) -> str:
    return f"workflow-task:{task_id}"


def global_capacity_key(slot: int) -> str:
    return f"workflow-capacity:global:{slot}"


def agent_capacity_key(agent_id: str, slot: int) -> str:
    return f"workflow-capacity:agent:{agent_id}:{slot}"


async def _acquire_in_session(
    session: AsyncSession,
    resource_key: str,
    owner_id: str,
    *,
    ttl_seconds: float,
) -> Lease | None:
    now = database_now_expression(session)
    values = {
        "resource_key": resource_key,
        "owner_id": owner_id,
        "fencing_token": 1,
        "lease_expires_at": _lease_expiry_expression(session, ttl_seconds),
        "created_at": now,
        "updated_at": now,
    }
    dialect = session.bind.dialect.name if session.bind is not None else ""
    insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
    statement: Any = insert(CoordinationLeaseRow).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[CoordinationLeaseRow.resource_key],
        set_={
            "owner_id": owner_id,
            "fencing_token": CoordinationLeaseRow.fencing_token + 1,
            "lease_expires_at": values["lease_expires_at"],
            "updated_at": now,
        },
        where=or_(
            CoordinationLeaseRow.owner_id == owner_id,
            CoordinationLeaseRow.lease_expires_at <= now,
        ),
    ).returning(CoordinationLeaseRow)
    row = (await session.execute(statement)).scalar_one_or_none()
    if row is None:
        return None
    return Lease(
        resource_key=row.resource_key,
        owner_id=row.owner_id,
        fencing_token=row.fencing_token,
        lease_expires_at=row.lease_expires_at,
    )


class TaskExecutionStore:
    """Atomically claims tasks together with distributed capacity slots."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        owner_id: str,
        max_active_global: int,
        max_active_per_agent: int,
        ttl_seconds: float = TASK_LEASE_TTL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self.owner_id = owner_id
        self.max_active_global = max_active_global
        self.max_active_per_agent = max_active_per_agent
        self.ttl_seconds = ttl_seconds

    async def claim_ready(self, *, queue_name: str = "default") -> TaskExecutionClaim | None:
        async with self._session_factory() as session:
            now = database_now_expression(session)
            candidate_stmt = (
                select(Task)
                .where(
                    Task.status == "ready",
                    Task.queue_name == queue_name,
                    sa.or_(Task.scheduled_for.is_(None), Task.scheduled_for <= now),
                )
                .order_by(Task.priority.desc(), Task.created_at.asc())
                .limit(1)
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                candidate_stmt = candidate_stmt.with_for_update(skip_locked=True)
            task = (await session.execute(candidate_stmt)).scalar_one_or_none()
            if task is None:
                await session.rollback()
                return None
            claim_owner_id = f"{self.owner_id}:task:{task.task_id}:{uuid.uuid4().hex}"
            task_lease = await _acquire_in_session(
                session,
                task_lease_key(task.task_id),
                claim_owner_id,
                ttl_seconds=self.ttl_seconds,
            )
            if task_lease is None:
                await session.rollback()
                return None
            global_lease = await self._acquire_capacity(
                session,
                (global_capacity_key(slot) for slot in range(self.max_active_global)),
                owner_id=claim_owner_id,
            )
            if global_lease is None:
                await session.rollback()
                return None
            agent_lease = await self._acquire_capacity(
                session,
                (
                    agent_capacity_key(task.agent_id, slot)
                    for slot in range(self.max_active_per_agent)
                ),
                owner_id=claim_owner_id,
            )
            if agent_lease is None:
                await session.rollback()
                return None

            claimed = await session.execute(
                update(Task)
                .where(Task.task_id == task.task_id, Task.status == "ready")
                .values(status="running", started_at=now, updated_at=now)
            )
            if not cast(Any, claimed).rowcount:
                await session.rollback()
                return None
            task_id = task.task_id
            agent_id = task.agent_id
            await session.commit()
            return TaskExecutionClaim(task_id, agent_id, task_lease, global_lease, agent_lease)

    async def claim_paused(self, task_id: str) -> TaskExecutionClaim | None:
        """Claim one paused task without changing its durable paused state."""
        return await self.claim_existing(task_id, statuses={"paused"})

    async def claim_existing(
        self,
        task_id: str,
        *,
        statuses: set[str],
    ) -> TaskExecutionClaim | None:
        """Claim a specific task already transitioned by an explicit task API."""
        async with self._session_factory() as session:
            stmt = select(Task).where(Task.task_id == task_id, Task.status.in_(statuses))
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            task = (await session.execute(stmt)).scalar_one_or_none()
            if task is None:
                await session.rollback()
                return None
            claim_owner_id = f"{self.owner_id}:task:{task.task_id}:{uuid.uuid4().hex}"
            task_lease = await _acquire_in_session(
                session,
                task_lease_key(task.task_id),
                claim_owner_id,
                ttl_seconds=self.ttl_seconds,
            )
            global_lease = (
                await self._acquire_capacity(
                    session,
                    (global_capacity_key(slot) for slot in range(self.max_active_global)),
                    owner_id=claim_owner_id,
                )
                if task_lease is not None
                else None
            )
            agent_lease = (
                await self._acquire_capacity(
                    session,
                    (
                        agent_capacity_key(task.agent_id, slot)
                        for slot in range(self.max_active_per_agent)
                    ),
                    owner_id=claim_owner_id,
                )
                if global_lease is not None
                else None
            )
            if global_lease is None or agent_lease is None or task_lease is None:
                await session.rollback()
                return None
            claimed_task_id = task.task_id
            claimed_agent_id = task.agent_id
            await session.commit()
            return TaskExecutionClaim(
                claimed_task_id, claimed_agent_id, task_lease, global_lease, agent_lease
            )

    async def _acquire_capacity(
        self,
        session: AsyncSession,
        keys: Any,
        *,
        owner_id: str,
    ) -> Lease | None:
        for key in keys:
            lease = await _acquire_in_session(
                session,
                key,
                owner_id,
                ttl_seconds=self.ttl_seconds,
            )
            if lease is not None:
                return lease
        return None

    async def renew(self, claim: TaskExecutionClaim) -> TaskExecutionClaim | None:
        async with self._session_factory() as session:
            renewed: list[Lease] = []
            for lease in claim.leases:
                now = database_now_expression(session)
                row = (
                    await session.execute(
                        update(CoordinationLeaseRow)
                        .where(
                            CoordinationLeaseRow.resource_key == lease.resource_key,
                            CoordinationLeaseRow.owner_id == lease.owner_id,
                            CoordinationLeaseRow.fencing_token == lease.fencing_token,
                            CoordinationLeaseRow.lease_expires_at > now,
                        )
                        .values(
                            lease_expires_at=_lease_expiry_expression(session, self.ttl_seconds),
                            updated_at=now,
                        )
                        .returning(CoordinationLeaseRow)
                    )
                ).scalar_one_or_none()
                if row is None:
                    await session.rollback()
                    return None
                renewed.append(
                    Lease(
                        row.resource_key,
                        row.owner_id,
                        row.fencing_token,
                        row.lease_expires_at,
                    )
                )
            await session.commit()
        return TaskExecutionClaim(
            claim.task_id,
            claim.agent_id,
            renewed[0],
            renewed[1] if claim.global_capacity_lease is not None else None,
            renewed[2] if claim.agent_capacity_lease is not None else None,
        )

    async def release_capacity(self, claim: TaskExecutionClaim) -> TaskExecutionClaim | None:
        """Release capacity slots while retaining the authoritative task lease."""
        if not claim.has_capacity:
            return claim
        async with self._session_factory() as session:
            now = database_now_expression(session)
            task_current = await self._lease_is_current(session, claim.task_lease)
            if not task_current:
                await session.rollback()
                return None
            for lease in (
                claim.global_capacity_lease,
                claim.agent_capacity_lease,
            ):
                assert lease is not None
                released = await session.execute(
                    update(CoordinationLeaseRow)
                    .where(
                        CoordinationLeaseRow.resource_key == lease.resource_key,
                        CoordinationLeaseRow.owner_id == lease.owner_id,
                        CoordinationLeaseRow.fencing_token == lease.fencing_token,
                        CoordinationLeaseRow.lease_expires_at > now,
                    )
                    .values(lease_expires_at=now, updated_at=now)
                )
                if not cast(Any, released).rowcount:
                    await session.rollback()
                    return None
            await session.commit()
        return TaskExecutionClaim(claim.task_id, claim.agent_id, claim.task_lease, None, None)

    async def reacquire_capacity(self, claim: TaskExecutionClaim) -> TaskExecutionClaim | None:
        """Atomically reacquire both capacity slots under the retained task fence."""
        if claim.has_capacity:
            return claim
        async with self._session_factory() as session:
            if not await self._lease_is_current(session, claim.task_lease):
                await session.rollback()
                return None
            global_lease = await self._acquire_capacity(
                session,
                (global_capacity_key(slot) for slot in range(self.max_active_global)),
                owner_id=claim.task_lease.owner_id,
            )
            agent_lease = (
                await self._acquire_capacity(
                    session,
                    (
                        agent_capacity_key(claim.agent_id, slot)
                        for slot in range(self.max_active_per_agent)
                    ),
                    owner_id=claim.task_lease.owner_id,
                )
                if global_lease is not None
                else None
            )
            if global_lease is None or agent_lease is None:
                await session.rollback()
                return None
            await session.commit()
        return TaskExecutionClaim(
            claim.task_id,
            claim.agent_id,
            claim.task_lease,
            global_lease,
            agent_lease,
        )

    async def _lease_is_current(self, session: AsyncSession, lease: Lease) -> bool:
        now = database_now_expression(session)
        row = (
            await session.execute(
                update(CoordinationLeaseRow)
                .where(
                    CoordinationLeaseRow.resource_key == lease.resource_key,
                    CoordinationLeaseRow.owner_id == lease.owner_id,
                    CoordinationLeaseRow.fencing_token == lease.fencing_token,
                    CoordinationLeaseRow.lease_expires_at > now,
                )
                .values(updated_at=CoordinationLeaseRow.updated_at)
                .returning(CoordinationLeaseRow.resource_key)
            )
        ).scalar_one_or_none()
        return row is not None

    async def release(self, claim: TaskExecutionClaim) -> None:
        async with self._session_factory() as session:
            now = database_now_expression(session)
            for lease in claim.leases:
                await session.execute(
                    update(CoordinationLeaseRow)
                    .where(
                        CoordinationLeaseRow.resource_key == lease.resource_key,
                        CoordinationLeaseRow.owner_id == lease.owner_id,
                        CoordinationLeaseRow.fencing_token == lease.fencing_token,
                    )
                    .values(lease_expires_at=now, updated_at=now)
                )
            await session.commit()


class TaskExecutionFence:
    """Renewable task ownership fence shared across workflow boundaries."""

    def __init__(
        self,
        store: TaskExecutionStore,
        claim: TaskExecutionClaim,
        cancel_event: asyncio.Event,
    ) -> None:
        self.store = store
        self.claim = claim
        self.cancel_event = cancel_event
        self._lost = asyncio.Event()
        self._renew_task: asyncio.Task[None] | None = None
        self._claim_lock = asyncio.Lock()

    def start(self) -> None:
        self._renew_task = asyncio.create_task(
            self._renew_loop(), name=f"workflow-task-renew:{self.claim.task_id}"
        )

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(TASK_LEASE_RENEW_SECONDS)
            try:
                async with self._claim_lock:
                    renewed = await self.store.renew(self.claim)
                    if renewed is not None:
                        self.claim = renewed
            except asyncio.CancelledError:
                raise
            except Exception:
                self._lost.set()
                self.cancel_event.set()
                return
            if renewed is None:
                self._lost.set()
                self.cancel_event.set()
                return

    async def assert_current(self, session: AsyncSession | None = None) -> None:
        if session is None:
            async with self.store._session_factory() as owned_session:
                await self.assert_current(owned_session)
            return
        if self._lost.is_set():
            raise StaleTaskExecutionOwner(self.claim.task_id)
        now = database_now_expression(session)
        for lease in self.claim.leases:
            exists = (
                await session.execute(
                    update(CoordinationLeaseRow)
                    .where(
                        CoordinationLeaseRow.resource_key == lease.resource_key,
                        CoordinationLeaseRow.owner_id == lease.owner_id,
                        CoordinationLeaseRow.fencing_token == lease.fencing_token,
                        CoordinationLeaseRow.lease_expires_at > now,
                    )
                    .values(updated_at=CoordinationLeaseRow.updated_at)
                    .returning(CoordinationLeaseRow.resource_key)
                )
            ).scalar_one_or_none()
            if exists is None:
                self._lost.set()
                self.cancel_event.set()
                raise StaleTaskExecutionOwner(self.claim.task_id)

    async def checkpoint(self, _name: str, **_metadata: Any) -> None:
        await self.assert_current()

    async def suspend_capacity(self) -> None:
        """Release capacity for an interactive pause without dropping task ownership."""
        async with self._claim_lock:
            suspended = await self.store.release_capacity(self.claim)
            if suspended is None:
                self._lost.set()
                self.cancel_event.set()
                raise StaleTaskExecutionOwner(self.claim.task_id)
            self.claim = suspended

    async def ensure_capacity(self) -> bool:
        """Try to reacquire execution capacity while retaining the task fence."""
        async with self._claim_lock:
            if self.claim.has_capacity:
                return True
            resumed = await self.store.reacquire_capacity(self.claim)
            if resumed is None:
                await self.assert_current()
                return False
            self.claim = resumed
            return True

    async def close(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._renew_task
        await self.store.release(self.claim)


_CURRENT_TASK_FENCE: ContextVar[TaskExecutionFence | None] = ContextVar(
    "current_task_execution_fence", default=None
)


def bind_task_execution_fence(fence: TaskExecutionFence) -> Token[TaskExecutionFence | None]:
    return _CURRENT_TASK_FENCE.set(fence)


def reset_task_execution_fence(token: Token[TaskExecutionFence | None]) -> None:
    _CURRENT_TASK_FENCE.reset(token)


def current_task_cancel_event() -> asyncio.Event | None:
    fence = _CURRENT_TASK_FENCE.get()
    return fence.cancel_event if fence is not None else None


def current_task_execution_fence() -> TaskExecutionFence | None:
    return _CURRENT_TASK_FENCE.get()


async def assert_task_execution_fence(session: AsyncSession) -> None:
    fence = _CURRENT_TASK_FENCE.get()
    if fence is not None:
        await fence.assert_current(session)

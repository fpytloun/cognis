"""Database-authoritative workflow task ownership and capacity fencing."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis import json_codec as json
from cognis.core.followups import (
    LLM_CYCLE_CEILING_CONTINUATION_REASON,
    TOOL_CALL_CEILING_CONTINUATION_REASON,
    ContinuationFollowUp,
)
from cognis.store.coordination import (
    Lease,
    _lease_expiry_expression,
    database_now_expression,
)
from cognis.store.models import CoordinationLeaseRow, Task

TASK_LEASE_TTL_SECONDS = 60.0
TASK_LEASE_RENEW_SECONDS = 15.0


def delegation_arguments_binding(arguments: dict[str, Any]) -> str:
    """Bind a reserved delegation to its exact original arguments."""
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
    attempt_number: int
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

    async def next_scheduled_delay(self, *, queue_name: str = "default") -> float | None:
        """Read a DB-relative deadline; overdue capacity waits do not spin."""
        async with self._session_factory() as session:
            now = database_now_expression(session)
            result = await session.execute(
                select(
                    sa.func.min(Task.scheduled_for),
                    sa.type_coerce(now, sa.DateTime(timezone=True)),
                ).where(
                    Task.status == "ready",
                    Task.queue_name == queue_name,
                    Task.scheduled_for > now,
                )
            )
            deadline, database_now = result.one()
        if deadline is None:
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if database_now.tzinfo is None:
            database_now = database_now.replace(tzinfo=UTC)
        return max(0.0, float((deadline - database_now).total_seconds()))

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
            return TaskExecutionClaim(
                task_id,
                agent_id,
                int(task.attempt_number or 1),
                task_lease,
                global_lease,
                agent_lease,
            )

    async def claim_paused(self, task_id: str) -> TaskExecutionClaim | None:
        """Claim one paused task without changing its durable paused state."""
        return await self.claim_existing(task_id, statuses={"paused"})

    async def claim_existing(
        self,
        task_id: str,
        *,
        statuses: set[str],
        capacity: bool = True,
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
            if task_lease is not None and not capacity:
                await session.commit()
                return TaskExecutionClaim(
                    task.task_id,
                    task.agent_id,
                    int(task.attempt_number or 1),
                    task_lease,
                    None,
                    None,
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
                claimed_task_id,
                claimed_agent_id,
                int(task.attempt_number or 1),
                task_lease,
                global_lease,
                agent_lease,
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

    async def renew(
        self,
        claim: TaskExecutionClaim,
        *,
        terminal_status: str | None = None,
    ) -> TaskExecutionClaim | None:
        async with self._session_factory() as session:
            allowed_statuses = ["running", "paused"]
            if terminal_status is not None:
                allowed_statuses.append(terminal_status)
            task_is_current = await session.scalar(
                select(Task.task_id).where(
                    Task.task_id == claim.task_id,
                    Task.attempt_number == claim.attempt_number,
                    Task.status.in_(allowed_statuses),
                )
            )
            if task_is_current is None:
                await session.rollback()
                return None
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
            claim.attempt_number,
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
        return TaskExecutionClaim(
            claim.task_id,
            claim.agent_id,
            claim.attempt_number,
            claim.task_lease,
            None,
            None,
        )

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
            claim.attempt_number,
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

    async def invalidate_task_lease(self, session: AsyncSession, task_id: str) -> None:
        """Expire the task lease so another controller observes a durable mutation now."""

        now = database_now_expression(session)
        await session.execute(
            update(CoordinationLeaseRow)
            .where(CoordinationLeaseRow.resource_key == task_lease_key(task_id))
            .values(lease_expires_at=now, updated_at=now)
        )


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
        self._terminal_status: str | None = None

    def start(self) -> None:
        self._renew_task = asyncio.create_task(
            self._renew_loop(), name=f"workflow-task-renew:{self.claim.task_id}"
        )

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(TASK_LEASE_RENEW_SECONDS)
            try:
                async with self._claim_lock:
                    renewed = await self.store.renew(
                        self.claim,
                        terminal_status=self._terminal_status,
                    )
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
        allowed_statuses = ["running", "paused"]
        if self._terminal_status is not None:
            allowed_statuses.append(self._terminal_status)
        task_stmt = select(Task.task_id).where(
            Task.task_id == self.claim.task_id,
            Task.attempt_number == self.claim.attempt_number,
            Task.status.in_(allowed_statuses),
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            task_stmt = task_stmt.with_for_update()
        task_is_current = await session.scalar(task_stmt)
        if task_is_current is None:
            self._lost.set()
            self.cancel_event.set()
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

    async def record_tool_dispatch(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        descriptors: list[dict[str, Any]],
    ) -> None:
        """Persist dispatch identities only; arguments/content remain in Intaris."""
        allowed = {"call_id", "tool_name", "dispatch_state"}
        async with self._claim_lock, self.store._session_factory() as db:
            await self.assert_current(db)
            task = await db.get(Task, self.claim.task_id, with_for_update=True)
            assert task is not None
            data = dict(task.result_data or {})
            batches = dict(data.get("execution_recovery") or {})
            key = f"{session_id}:{turn_id}"
            previous = batches.get(key, {})
            prior_calls = {d["call_id"]: d for d in previous.get("tool_calls", [])}
            for descriptor in descriptors:
                call_id = descriptor["call_id"]
                if call_id not in prior_calls:
                    prior_calls[call_id] = {k: v for k, v in descriptor.items() if k in allowed}
                    prior_calls[call_id]["canonical_turn_id"] = turn_id
            batches[key] = {
                "attempt_number": self.claim.attempt_number,
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_calls": list(prior_calls.values()),
            }
            task.result_data = {**data, "execution_recovery": batches}
            await db.commit()

    async def bind_tool_dispatch(
        self,
        call_id: str,
        executor_id: str,
        executor_instance_id: str | None,
        *,
        session_id: str,
        turn_id: str | None,
        dispatch_state: str,
    ) -> None:
        """Commit dispatch identity or proven non-delivery within the current call scope."""
        async with self._claim_lock, self.store._session_factory() as db:
            await self.assert_current(db)
            task = await db.get(Task, self.claim.task_id, with_for_update=True)
            assert task is not None
            data = dict(task.result_data or {})
            batches = dict(data.get("execution_recovery") or {})
            key = f"{session_id}:{turn_id}"
            batch = dict(batches.get(key) or {})
            calls = [dict(d) for d in batch.get("tool_calls", [])]
            descriptor = next((d for d in calls if d["call_id"] == call_id), None)
            if descriptor is None:
                raise StaleTaskExecutionOwner("Missing durable task dispatch descriptor")
            descriptor.update(
                executor_id=executor_id,
                executor_instance_id=executor_instance_id,
                dispatch_state=dispatch_state,
            )
            batches[key] = {**batch, "tool_calls": calls}
            task.result_data = {**data, "execution_recovery": batches}
            await db.commit()

    async def recovery_batches(self) -> dict[str, Any]:
        """Read current attempt metadata, not a stale task-model snapshot."""
        async with self.store._session_factory() as db:
            await self.assert_current(db)
            task = await db.get(Task, self.claim.task_id)
            if task is None:
                raise StaleTaskExecutionOwner(self.claim.task_id)
            return {
                key: batch
                for key, batch in ((task.result_data or {}).get("execution_recovery") or {}).items()
                if batch.get("attempt_number") == self.claim.attempt_number
            }

    async def reserve_delegation(
        self,
        *,
        session_id: str,
        turn_id: str,
        call_id: str,
        binding: str,
        step_run_id: str | None = None,
    ) -> tuple[str, str]:
        """Reserve immutable child identities before provisioning or execution."""
        from cognis.core.task_recovery_errors import TaskRecoveryBlocked

        async with self._claim_lock, self.store._session_factory() as db:
            await self.assert_current(db)
            task = await db.get(Task, self.claim.task_id, with_for_update=True)
            assert task is not None
            data = dict(task.result_data or {})
            batches = dict(data.get("execution_recovery") or {})
            key = f"{session_id}:{turn_id}"
            batch = dict(batches.get(key) or {})
            calls = [dict(d) for d in batch.get("tool_calls", [])]
            descriptor = next((d for d in calls if d["call_id"] == call_id), None)
            if descriptor is None or descriptor.get("tool_name") != "delegate":
                raise TaskRecoveryBlocked("Delegation has no owned durable call reservation.")
            if descriptor.get("canonical_turn_id", turn_id) != turn_id:
                raise TaskRecoveryBlocked(
                    "Delegation canonical call turn differs from its reserved turn. "
                    "Cancel the task and start a fresh run after reconciling existing effects."
                )
            if descriptor.get("delegate_binding") not in (None, binding):
                raise TaskRecoveryBlocked("Delegation reservation binding does not match.")
            if not descriptor.get("child_session_id"):
                descriptor.update(
                    child_session_id=f"sess_{uuid.uuid4().hex}",
                    child_turn_id=f"turn_{uuid.uuid4().hex[:12]}",
                    delegate_binding=binding,
                )
                if step_run_id is not None:
                    descriptor.update(
                        task_id=self.claim.task_id,
                        attempt_number=self.claim.attempt_number,
                        step_run_id=step_run_id,
                    )
            if not descriptor.get("child_turn_id") or not descriptor.get("delegate_binding"):
                raise TaskRecoveryBlocked("Delegation reservation identity is incomplete.")
            batches[key] = {**batch, "tool_calls": calls}
            task.result_data = {**data, "execution_recovery": batches}
            await db.commit()
            return descriptor["child_session_id"], descriptor["child_turn_id"]

    async def restore_child_reservation(
        self, *, child_session_id: str, parent_session_id: str, call: dict[str, Any]
    ) -> None:
        """Restore a lost attachment only from complete same-turn task provenance."""
        from cognis.core.task_recovery_errors import TaskRecoveryBlocked
        from cognis.store.models import Session, StepRun

        async with self._claim_lock, self.store._session_factory() as db:
            await self.assert_current(db)
            task = await db.get(Task, self.claim.task_id, with_for_update=True)
            child = await db.get(Session, child_session_id, with_for_update=True)
            proof = (child.delegation_metadata or {}).get("task_call", {}) if child else {}
            parent = await db.get(Session, parent_session_id)
            step = (
                await db.get(StepRun, proof.get("step_run_id"))
                if proof.get("step_run_id")
                else None
            )
            if (
                task is None
                or child is None
                or parent is None
                or child.parent_session_id != parent_session_id
                or child.user_email != parent.user_email
                or child.conversation_id != parent.conversation_id
                or proof.get("task_id") != self.claim.task_id
                or proof.get("attempt_number") != self.claim.attempt_number
                or not (child.delegation_metadata or {}).get("task_runtime_fingerprint")
                or not (child.delegation_metadata or {}).get("task_execution_turn_id")
                or step is None
                or step.task_id != self.claim.task_id
                or step.attempt_number != self.claim.attempt_number
                or step.superseded_by_step_run_id is not None
                or proof.get("session_id") != parent_session_id
                or proof.get("child_session_id") != child_session_id
                or call.get("call_id") != proof.get("call_id")
                or call.get("name") != "delegate"
                or call.get("turn_id") != proof.get("turn_id")
                or delegation_arguments_binding(
                    call.get("recovery_arguments") or call.get("arguments") or {}
                )
                != proof.get("binding")
                or not proof.get("child_turn_id")
            ):
                raise TaskRecoveryBlocked(
                    f"Delegated child {child_session_id} has missing or conflicting "
                    "task/attempt/StepRun or same-turn canonical-call proof. "
                    "Cancel the task and start a fresh run after reconciling existing effects."
                )
            data = dict(task.result_data or {})
            batches = dict(data.get("execution_recovery") or {})
            key = f"{proof['session_id']}:{proof['turn_id']}"
            batch = dict(batches.get(key) or {})
            calls = list(batch.get("tool_calls") or [])
            existing = next((d for d in calls if d["call_id"] == proof["call_id"]), None)
            descriptor = {
                "call_id": proof["call_id"],
                "tool_name": "delegate",
                "session_id": proof["session_id"],
                "turn_id": proof["turn_id"],
                "canonical_turn_id": proof["turn_id"],
                "attempt_number": self.claim.attempt_number,
                "child_session_id": child_session_id,
                "child_turn_id": proof["child_turn_id"],
                "delegate_binding": proof["binding"],
            }
            if existing is not None:
                if any(
                    existing.get(k) != v for k, v in descriptor.items() if k != "canonical_turn_id"
                ):
                    raise TaskRecoveryBlocked(
                        "Conflicting delegated-child reservation; cancel the task."
                    )
                return
            calls.append(descriptor)
            batches[key] = {
                **batch,
                "session_id": proof["session_id"],
                "turn_id": proof["turn_id"],
                "attempt_number": self.claim.attempt_number,
                "tool_calls": calls,
            }
            task.result_data = {**data, "execution_recovery": batches}
            owned_receipt = False
            for pending in batches.values():
                if pending.get("attempt_number") != self.claim.attempt_number or not pending.get(
                    "tool_calls"
                ):
                    continue
                descendant = await db.get(Session, pending.get("session_id"))
                if (
                    descendant is not None
                    and pending.get("turn_id")
                    == (descendant.delegation_metadata or {}).get("task_execution_turn_id")
                    and await self._owns_reserved_child(db, task, descendant)
                ):
                    owned_receipt = True
                    break
            if not owned_receipt:
                raise TaskRecoveryBlocked(
                    "Delegated child has no current-attempt receipt with consistent session "
                    "lineage and active turn; cancel the task after reconciling existing effects."
                )
            await db.commit()

    async def bind_child_runtime(
        self, session_id: str, fingerprint: str
    ) -> tuple[str, ContinuationFollowUp | None]:
        """Validate scope and restore the active, durably reserved child turn."""
        from cognis.core.task_recovery_errors import TaskRecoveryBlocked
        from cognis.store.models import Session

        async with self._claim_lock, self.store._session_factory() as db:
            await self.assert_current(db)
            task = await db.get(Task, self.claim.task_id)
            row = await db.get(Session, session_id, with_for_update=True)
            if row is None or task is None or not await self._owns_reserved_child(db, task, row):
                raise TaskRecoveryBlocked("Reserved delegated child is unavailable.")
            metadata = dict(row.delegation_metadata or {})
            previous = metadata.get("task_runtime_fingerprint")
            if previous is not None and previous != fingerprint:
                raise TaskRecoveryBlocked(
                    "Delegated runtime configuration or tool scope changed; automatic recovery stopped."
                )
            original_turn = metadata["task_call"]["child_turn_id"]
            turn_id = metadata.get("task_execution_turn_id", original_turn)
            continuation = None
            try:
                if saved := metadata.get("task_continuation"):
                    continuation = ContinuationFollowUp.model_validate(saved)
                    if (
                        continuation.reason
                        not in {
                            TOOL_CALL_CEILING_CONTINUATION_REASON,
                            LLM_CYCLE_CEILING_CONTINUATION_REASON,
                        }
                        or continuation.attempt > continuation.max_attempts
                        or continuation.topic_ref != turn_id
                    ):
                        raise ValueError("Invalid continuation identity")
                elif turn_id != original_turn:
                    raise ValueError("Missing continuation identity")
                if not isinstance(turn_id, str) or not turn_id:
                    raise ValueError("Missing child turn")
            except ValueError as exc:
                raise TaskRecoveryBlocked("Delegated continuation identity is invalid.") from exc
            row.delegation_metadata = {
                **metadata,
                "task_runtime_fingerprint": fingerprint,
                "task_execution_turn_id": turn_id,
            }
            await db.commit()
            return turn_id, continuation

    async def advance_child_continuation(
        self, session_id: str, *, prior_turn_id: str, follow_up: ContinuationFollowUp
    ) -> str:
        """Reserve the next child turn before inference, keeping provenance unchanged."""
        from cognis.core.task_recovery_errors import TaskRecoveryBlocked
        from cognis.store.models import Session

        async with self._claim_lock, self.store._session_factory() as db:
            await self.assert_current(db)
            task = await db.get(Task, self.claim.task_id)
            row = await db.get(Session, session_id, with_for_update=True)
            if row is None or task is None or not await self._owns_reserved_child(db, task, row):
                raise TaskRecoveryBlocked("Reserved delegated child is unavailable.")
            metadata = dict(row.delegation_metadata or {})
            if metadata.get("task_execution_turn_id") != prior_turn_id:
                raise TaskRecoveryBlocked("Delegated continuation no longer owns its prior turn.")
            if any(
                batch.get("session_id") == (row.intaris_session_id or row.session_id)
                and batch.get("tool_calls")
                for batch in ((task.result_data or {}).get("execution_recovery") or {}).values()
            ):
                raise TaskRecoveryBlocked("Delegated continuation still has unsettled tools.")
            turn_id = f"turn_{uuid.uuid4().hex[:12]}"
            row.delegation_metadata = {
                **metadata,
                "task_execution_turn_id": turn_id,
                "task_continuation": {
                    **follow_up.model_dump(mode="json", exclude={"pending_todos"}),
                    "topic_ref": turn_id,
                },
            }
            await db.commit()
            return turn_id

    async def _owns_reserved_child(self, db: AsyncSession, task: Any, child: Any) -> bool:
        from cognis.store.models import Session

        binding = (child.delegation_metadata or {}).get("task_call") or {}
        roots = [
            descriptor["child_session_id"]
            for batch in ((task.result_data or {}).get("execution_recovery") or {}).values()
            for descriptor in batch.get("tool_calls", [])
            if (
                batch.get("attempt_number") == self.claim.attempt_number
                and batch.get("session_id") == binding.get("session_id")
                and child.parent_session_id == binding.get("session_id")
                and batch.get("turn_id") == binding.get("turn_id")
                and descriptor.get("tool_name") == "delegate"
                and descriptor.get("call_id") == binding.get("call_id")
                and descriptor.get("child_session_id")
                and descriptor.get("child_turn_id") == binding.get("child_turn_id")
                and descriptor.get("delegate_binding") == binding.get("binding")
            )
        ]
        if len(roots) != 1:
            return False
        cursor = child
        seen = set()
        for _ in range(128):
            if (
                cursor.user_email != task.created_by
                or cursor.parent_session_id != child.parent_session_id
                or cursor.conversation_id != child.conversation_id
                or cursor.agent_id != child.agent_id
                or (cursor.delegation_metadata or {}).get("task_call") != binding
                or cursor.session_id in seen
            ):
                return False
            if cursor.session_id == roots[0]:
                return True
            seen.add(cursor.session_id)
            if not cursor.previous_session_id:
                return False
            cursor = await db.get(Session, cursor.previous_session_id)
            if cursor is None:
                return False
        return False

    async def cancel_child_if_requested(
        self, session_id: str, *, explicit_cancel: bool = False
    ) -> bool:
        """Apply task cancellation or resolved human denial, never shutdown."""
        from cognis.store.models import Session
        from cognis.store.queries import set_session_status

        async with self._claim_lock, self.store._session_factory() as db:
            task = await db.get(Task, self.claim.task_id, with_for_update=True)
            if task is None or task.attempt_number != self.claim.attempt_number:
                return False
            if task.status != "cancelled":
                if not explicit_cancel or task.status not in {"running", "paused"}:
                    return False
                await self.assert_current(db)
            child = await db.get(Session, session_id, with_for_update=True)
            if child is None or not await self._owns_reserved_child(db, task, child):
                return False
            await set_session_status(
                db,
                session_id,
                "cancelled",
                result_summary="Cancelled (parent task cancelled)",
                allowed_from={"active", "idle", "suspended"},
            )
            await db.commit()
            return True

    async def complete_tool_dispatch(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        call_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Release settled descriptors after the canonical result boundary."""
        async with self._claim_lock, self.store._session_factory() as db:
            await self.assert_current(db)
            task = await db.get(Task, self.claim.task_id, with_for_update=True)
            assert task is not None
            data = dict(task.result_data or {})
            batches = dict(data.get("execution_recovery") or {})
            key = f"{session_id}:{turn_id}"
            batch = batches.get(key, {})
            remaining = [d for d in batch.get("tool_calls", []) if d["call_id"] not in call_ids]
            settled = [d for d in batch.get("tool_calls", []) if d["call_id"] in call_ids]
            if remaining:
                batches[key] = {**batch, "tool_calls": remaining}
            else:
                batches.pop(key, None)
            task.result_data = {**data, "execution_recovery": batches}
            await db.commit()
            return [
                {**descriptor, "session_id": session_id, "turn_id": turn_id}
                for descriptor in settled
            ]

    def mark_terminal(self, status: str) -> None:
        """Permit post-commit cleanup and delivery for this exact terminal winner."""

        self._terminal_status = status

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

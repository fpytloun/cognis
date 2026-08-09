"""DB-fenced ownership for physical executor WebSocket connections."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.store.coordination import DatabaseLeaseStore, Lease, database_now_expression
from cognis.store.models import CoordinationLeaseRow, ExecutorRow

EXECUTOR_CONNECTION_LEASE_TTL_SECONDS = 45.0
EXECUTOR_HEARTBEAT_FRESHNESS_SECONDS = 30.0
EXECUTOR_CALLBACK_FENCE_TIMEOUT_SECONDS = 5.0


def executor_connection_resource_key(executor_id: str) -> str:
    return f"executor_connection:{executor_id}"


@dataclass(slots=True)
class ExecutorConnectionOwner:
    """Exact controller/epoch tuple attached to one physical socket."""

    executor_id: str
    lease: Lease

    @property
    def owner_id(self) -> str:
        return self.lease.owner_id

    @property
    def epoch(self) -> int:
        return self.lease.fencing_token


class ExecutorConnectionOwnership:
    """Single public authority for executor ownership and fenced mutations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        controller_owner_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._lease_store = DatabaseLeaseStore(session_factory)
        self.controller_owner_id = controller_owner_id

    async def takeover(self, executor_id: str) -> ExecutorConnectionOwner:
        lease = await self._lease_store.takeover(
            executor_connection_resource_key(executor_id),
            self.controller_owner_id,
            ttl_seconds=EXECUTOR_CONNECTION_LEASE_TTL_SECONDS,
        )
        return ExecutorConnectionOwner(executor_id=executor_id, lease=lease)

    async def takeover_validated(
        self,
        executor_id: str,
        *,
        token_version: int,
    ) -> ExecutorConnectionOwner | None:
        """Atomically validate the executor row and advance its connection fence."""

        async with self._session_factory() as session:
            row = await session.scalar(
                select(ExecutorRow).where(ExecutorRow.executor_id == executor_id).with_for_update()
            )
            if (
                row is None
                or row.executor_type != "websocket"
                or row.status != "active"
                or row.token_version != token_version
            ):
                await session.rollback()
                return None
            lease = await self._lease_store.takeover_in_session(
                session,
                executor_connection_resource_key(executor_id),
                self.controller_owner_id,
                ttl_seconds=EXECUTOR_CONNECTION_LEASE_TTL_SECONDS,
            )
            await session.commit()
        return ExecutorConnectionOwner(executor_id=executor_id, lease=lease)

    async def renew_from_heartbeat(
        self,
        owner: ExecutorConnectionOwner,
        *,
        heartbeat_received_at: datetime,
    ) -> bool:
        received_at = (
            heartbeat_received_at.replace(tzinfo=UTC)
            if heartbeat_received_at.tzinfo is None
            else heartbeat_received_at.astimezone(UTC)
        )
        if (datetime.now(UTC) - received_at).total_seconds() > EXECUTOR_HEARTBEAT_FRESHNESS_SECONDS:
            return False
        renewed = await self._lease_store.renew(
            owner.lease,
            ttl_seconds=EXECUTOR_CONNECTION_LEASE_TTL_SECONDS,
        )
        if renewed is None:
            return False
        owner.lease = renewed
        return True

    async def release(self, owner: ExecutorConnectionOwner) -> bool:
        return await self._lease_store.release(owner.lease)

    async def revoke(self, executor_id: str) -> bool:
        return await self._lease_store.revoke(executor_connection_resource_key(executor_id))

    async def is_current(self, owner: ExecutorConnectionOwner) -> bool:
        return await self._lease_store.is_current(owner.lease)

    async def run_callback_if_current(
        self,
        owner: ExecutorConnectionOwner,
        callback: Callable[..., Any],
        *args: Any,
    ) -> bool:
        """Run a bounded final callback effect under the exact owner fence."""

        async with self._session_factory() as session:
            if not await self.lock_current(session, owner):
                await session.rollback()
                return False
            result = callback(*args)
            if inspect.isawaitable(result):
                async with asyncio.timeout(EXECUTOR_CALLBACK_FENCE_TIMEOUT_SECONDS):
                    await result
            await session.commit()
            return True

    async def run_durable_callback_if_current(
        self,
        owner: ExecutorConnectionOwner,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> tuple[bool, Any]:
        """Serialize one non-reentrant durable admission with owner takeover."""

        async with self._session_factory() as session:
            if not await self.lock_current(session, owner):
                await session.rollback()
                return False, None
            result = callback(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            await session.commit()
            return True, result

    async def apply_effect_if_current(
        self,
        owner: ExecutorConnectionOwner,
        effect: Callable[[], None],
    ) -> bool:
        """Apply one non-blocking in-memory dispatch under the durable fence."""

        async with self._session_factory() as session:
            if not await self.lock_current(session, owner):
                await session.rollback()
                return False
            effect()
            await session.commit()
            return True

    @staticmethod
    async def lock_current(
        session: AsyncSession,
        owner: ExecutorConnectionOwner,
    ) -> bool:
        """Lock and validate an exact owner/epoch inside a caller transaction."""

        now = database_now_expression(session)
        statement = (
            select(CoordinationLeaseRow.resource_key)
            .where(
                CoordinationLeaseRow.resource_key == owner.lease.resource_key,
                CoordinationLeaseRow.owner_id == owner.owner_id,
                CoordinationLeaseRow.fencing_token == owner.epoch,
                CoordinationLeaseRow.lease_expires_at > now,
            )
            .with_for_update()
        )
        return await session.scalar(statement) is not None

    async def update_runtime_state(
        self,
        session: AsyncSession,
        owner: ExecutorConnectionOwner,
        **values: Any,
    ) -> ExecutorRow | None:
        """Update an executor row only while the exact owner/epoch is current."""

        filtered_values = {key: value for key, value in values.items() if value is not None}
        if not filtered_values:
            return await session.get(ExecutorRow, owner.executor_id)
        locked_executor = await session.scalar(
            select(ExecutorRow.executor_id)
            .where(ExecutorRow.executor_id == owner.executor_id)
            .with_for_update()
        )
        if locked_executor is None or not await self.lock_current(session, owner):
            return None
        now = database_now_expression(session)
        current_owner = exists(
            select(CoordinationLeaseRow.resource_key).where(
                CoordinationLeaseRow.resource_key == owner.lease.resource_key,
                CoordinationLeaseRow.owner_id == owner.owner_id,
                CoordinationLeaseRow.fencing_token == owner.epoch,
                CoordinationLeaseRow.lease_expires_at > now,
            )
        )
        statement = (
            update(ExecutorRow)
            .where(
                ExecutorRow.executor_id == owner.executor_id,
                current_owner,
            )
            .values(**filtered_values)
            .returning(ExecutorRow)
        )
        return (await session.execute(statement)).scalar_one_or_none()

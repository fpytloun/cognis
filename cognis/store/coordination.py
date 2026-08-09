"""Narrow database-authoritative lease and fencing store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.store.models import CoordinationLeaseRow


def database_now_expression(session: AsyncSession) -> Any:
    """Return a dialect-appropriate expression evaluated by the mutation statement."""
    dialect = session.bind.dialect.name if session.bind is not None else ""
    return func.clock_timestamp() if dialect == "postgresql" else func.current_timestamp()


def _lease_expiry_expression(session: AsyncSession, ttl_seconds: float) -> Any:
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        return func.clock_timestamp() + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds)
    return func.datetime("now", f"{ttl_seconds:+f} seconds")


async def database_now(session: AsyncSession) -> datetime:
    """Read the database clock so lease expiry never depends on controller skew."""
    now = (await session.execute(select(database_now_expression(session)))).scalar_one()
    if not isinstance(now, datetime):
        raise RuntimeError("Database did not return a timestamp")
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


@dataclass(frozen=True)
class Lease:
    resource_key: str
    owner_id: str
    fencing_token: int
    lease_expires_at: datetime


class DatabaseLeaseStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lease(row: Any) -> Lease:
        return Lease(
            resource_key=row.resource_key,
            owner_id=row.owner_id,
            fencing_token=row.fencing_token,
            lease_expires_at=row.lease_expires_at,
        )

    async def acquire(
        self, resource_key: str, owner_id: str, *, ttl_seconds: float
    ) -> Lease | None:
        """Atomically insert or take an owned/expired lease and advance its fence."""
        for attempt in range(3):
            async with self._session_factory() as session:
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
                try:
                    row = (await session.execute(statement)).scalar_one_or_none()
                    await session.commit()
                    return self._lease(row) if row is not None else None
                except OperationalError:
                    await session.rollback()
                    if attempt == 2:
                        return None
                    await asyncio.sleep(0)
        return None

    async def takeover(self, resource_key: str, owner_id: str, *, ttl_seconds: float) -> Lease:
        """Atomically replace any owner and advance the resource fence.

        This is reserved for independently authenticated resources such as an
        executor establishing a new physical socket. Ordinary coordination
        consumers must use :meth:`acquire` so a live owner cannot be preempted.
        """

        for attempt in range(3):
            async with self._session_factory() as session:
                try:
                    lease = await self.takeover_in_session(
                        session,
                        resource_key,
                        owner_id,
                        ttl_seconds=ttl_seconds,
                    )
                    await session.commit()
                    return lease
                except OperationalError:
                    await session.rollback()
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0)
        raise RuntimeError("Executor ownership takeover exhausted retry budget")

    async def takeover_in_session(
        self,
        session: AsyncSession,
        resource_key: str,
        owner_id: str,
        *,
        ttl_seconds: float,
    ) -> Lease:
        """Advance a fence inside a caller-owned validation transaction."""

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
        ).returning(CoordinationLeaseRow)
        row = (await session.execute(statement)).scalar_one()
        return self._lease(row)

    async def acquire_in_session(
        self,
        session: AsyncSession,
        resource_key: str,
        owner_id: str,
        *,
        ttl_seconds: float,
    ) -> Lease | None:
        """Acquire without preempting another live owner in a caller transaction."""

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
        return self._lease(row) if row is not None else None

    async def renew(self, lease: Lease, *, ttl_seconds: float) -> Lease | None:
        """Renew only the current unexpired owner/fence tuple."""
        async with self._session_factory() as session:
            now = database_now_expression(session)
            statement = (
                update(CoordinationLeaseRow)
                .where(
                    CoordinationLeaseRow.resource_key == lease.resource_key,
                    CoordinationLeaseRow.owner_id == lease.owner_id,
                    CoordinationLeaseRow.fencing_token == lease.fencing_token,
                    CoordinationLeaseRow.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=_lease_expiry_expression(session, ttl_seconds),
                    updated_at=now,
                )
                .returning(CoordinationLeaseRow)
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()
            return self._lease(row) if row is not None else None

    async def release(self, lease: Lease) -> bool:
        """Expire only the current owner/fence tuple."""
        async with self._session_factory() as session:
            now = database_now_expression(session)
            result = await session.execute(
                update(CoordinationLeaseRow)
                .where(
                    CoordinationLeaseRow.resource_key == lease.resource_key,
                    CoordinationLeaseRow.owner_id == lease.owner_id,
                    CoordinationLeaseRow.fencing_token == lease.fencing_token,
                )
                .values(lease_expires_at=now, updated_at=now)
            )
            await session.commit()
            return bool(cast(Any, result).rowcount)

    async def revoke(self, resource_key: str) -> bool:
        """Invalidate any current owner and advance the resource fence."""
        async with self._session_factory() as session:
            now = database_now_expression(session)
            result = await session.execute(
                update(CoordinationLeaseRow)
                .where(CoordinationLeaseRow.resource_key == resource_key)
                .values(
                    owner_id="revoked",
                    fencing_token=CoordinationLeaseRow.fencing_token + 1,
                    lease_expires_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(cast(Any, result).rowcount)

    async def is_current(self, lease: Lease) -> bool:
        """Return whether the exact owner/fence remains current and unexpired."""
        async with self._session_factory() as session:
            return await self.is_current_in_session(session, lease)

    async def is_current_in_session(self, session: AsyncSession, lease: Lease) -> bool:
        """Check an exact lease inside the caller's settlement transaction."""
        now = database_now_expression(session)
        row = await session.scalar(
            select(CoordinationLeaseRow.resource_key)
            .where(
                CoordinationLeaseRow.resource_key == lease.resource_key,
                CoordinationLeaseRow.owner_id == lease.owner_id,
                CoordinationLeaseRow.fencing_token == lease.fencing_token,
                CoordinationLeaseRow.lease_expires_at > now,
            )
            .with_for_update()
        )
        return row is not None

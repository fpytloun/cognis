"""Minimal DB-authoritative controller instance directory."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.controller_runtime import ControllerRuntime
from cognis.logging import get_logger
from cognis.store.coordination import database_now_expression
from cognis.store.models import ControllerInstanceRow

logger = get_logger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 5.0
_DIRECTORY_TTL_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class ControllerInstance:
    owner_id: str
    controller_id: str
    incarnation_id: str
    internal_url: str | None
    lifecycle_state: str


class ControllerInstanceDirectory:
    """Register and heartbeat exactly one controller boot incarnation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        identity: ControllerRuntime,
        *,
        internal_url: str | None,
    ) -> None:
        self._session_factory = session_factory
        self._identity = identity
        self._internal_url = internal_url
        self._state = "starting"
        self._task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._write_lock:
            await self._write(self._state, live=True)
            self._task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"controller-directory-{self._identity.owner_id}",
            )

    async def mark_ready(self) -> None:
        async with self._write_lock:
            if self._state not in {"starting", "ready"}:
                return
            self._state = "ready"
            await self._write(self._state, live=True)

    async def begin_draining(self) -> None:
        async with self._write_lock:
            if self._state == "stopped":
                return
            self._state = "draining"
            await self._write(self._state, live=True)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        async with self._write_lock:
            self._state = "stopped"
            await self._write(self._state, live=False)

    async def get_ready(self, owner_id: str) -> ControllerInstance | None:
        """Resolve one ready controller for new routing or admission."""

        async with self._session_factory() as session:
            now = database_now_expression(session)
            row = await session.scalar(
                select(ControllerInstanceRow).where(
                    ControllerInstanceRow.owner_id == owner_id,
                    ControllerInstanceRow.lifecycle_state == "ready",
                    ControllerInstanceRow.expires_at > now,
                )
            )
        if row is None:
            return None
        return ControllerInstance(
            owner_id=row.owner_id,
            controller_id=row.controller_id,
            incarnation_id=row.incarnation_id,
            internal_url=row.internal_url,
            lifecycle_state=row.lifecycle_state,
        )

    async def get_reachable(self, owner_id: str) -> ControllerInstance | None:
        """Resolve one ready or draining exact incarnation for admitted work."""

        async with self._session_factory() as session:
            now = database_now_expression(session)
            row = await session.scalar(
                select(ControllerInstanceRow).where(
                    ControllerInstanceRow.owner_id == owner_id,
                    ControllerInstanceRow.controller_id == owner_id.rsplit(":", 1)[0],
                    ControllerInstanceRow.incarnation_id == owner_id.rsplit(":", 1)[-1],
                    ControllerInstanceRow.lifecycle_state.in_(("ready", "draining")),
                    ControllerInstanceRow.expires_at > now,
                )
            )
        if row is None:
            return None
        return ControllerInstance(
            owner_id=row.owner_id,
            controller_id=row.controller_id,
            incarnation_id=row.incarnation_id,
            internal_url=row.internal_url,
            lifecycle_state=row.lifecycle_state,
        )

    async def get_live(self, owner_id: str) -> ControllerInstance | None:
        """Compatibility alias for ready-only routing."""

        return await self.get_ready(owner_id)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            try:
                await self._heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("controller directory heartbeat failed", exc_info=True)

    async def _heartbeat_once(self) -> None:
        async with self._write_lock:
            if self._state == "stopped":
                return
            await self._write(self._state, live=True)

    async def _write(self, lifecycle_state: str, *, live: bool) -> None:
        async with self._session_factory() as session:
            now = database_now_expression(session)
            expires_at = (
                now
                if not live
                else (
                    func.clock_timestamp()
                    + func.make_interval(0, 0, 0, 0, 0, 0, _DIRECTORY_TTL_SECONDS)
                    if session.bind is not None and session.bind.dialect.name == "postgresql"
                    else func.datetime("now", f"+{_DIRECTORY_TTL_SECONDS:f} seconds")
                )
            )
            existing = await session.get(ControllerInstanceRow, self._identity.owner_id)
            if existing is None:
                session.add(
                    ControllerInstanceRow(
                        owner_id=self._identity.owner_id,
                        controller_id=self._identity.controller_id,
                        incarnation_id=self._identity.incarnation_id,
                        internal_url=self._internal_url,
                        lifecycle_state=lifecycle_state,
                        heartbeat_at=now,
                        expires_at=expires_at,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                await session.execute(
                    update(ControllerInstanceRow)
                    .where(ControllerInstanceRow.owner_id == self._identity.owner_id)
                    .values(
                        internal_url=self._internal_url,
                        lifecycle_state=lifecycle_state,
                        heartbeat_at=now,
                        expires_at=expires_at,
                        updated_at=now,
                    )
                )
            await session.commit()

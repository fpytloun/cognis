"""Periodic maintenance for uploaded artifacts."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from cognis.logging import get_logger
from cognis.store.queries import (
    delete_artifact_record,
    list_expired_temporary_artifacts,
    list_orphaned_attached_artifacts,
)

logger = get_logger(__name__)


class ArtifactMaintenanceService:
    """Background cleanup for temporary and orphaned artifacts."""

    def __init__(
        self,
        *,
        session_factory: Any,
        artifact_store: Any,
        interval_seconds: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_store = artifact_store
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="artifact-maintenance")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            expired = await list_expired_temporary_artifacts(session, now=now)
            orphaned = await list_orphaned_attached_artifacts(session)

            for row in expired:
                await self._artifact_store.async_delete_object(row.namespace, row.object_id)
                await delete_artifact_record(session, row.artifact_id)

            for row in orphaned:
                await self._artifact_store.async_delete_object(row.namespace, row.object_id)
                await delete_artifact_record(session, row.artifact_id)

            await session.commit()

        if expired or orphaned:
            logger.info(
                "artifact maintenance completed",
                extra={
                    "extra_data": {
                        "expired_deleted": len(expired),
                        "orphan_candidates": len(orphaned),
                    }
                },
            )

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.warning("artifact maintenance failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue

"""Periodic maintenance for uploaded artifacts."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from cognis.logging import get_logger
from cognis.store.queries import (
    delete_artifact_record,
    delete_expired_tts_cache_entries,
    get_setting_value,
    list_expired_temporary_artifacts,
    list_orphaned_attached_artifacts,
)

logger = get_logger(__name__)


_DEFAULT_TTS_TTL_DAYS = 30


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

            # TTS cache TTL prune. Deletes expired tts_cache rows AND the
            # corresponding artifact_records row + storage bytes so a future
            # re-synthesize for the same (message, voice, model) tuple does
            # not collide on the deterministic artifact_id.
            ttl_days = await _resolve_tts_ttl_days(session)
            tts_cutoff = now - timedelta(days=ttl_days)
            tts_expired = await delete_expired_tts_cache_entries(session, older_than=tts_cutoff)
            for tts_row in tts_expired:
                with contextlib.suppress(Exception):
                    await self._artifact_store.async_delete_object("tts", tts_row.artifact_id)
                with contextlib.suppress(Exception):
                    await delete_artifact_record(session, tts_row.artifact_id)

            await session.commit()

        if expired or orphaned or tts_expired:
            logger.info(
                "artifact maintenance completed",
                extra={
                    "extra_data": {
                        "expired_deleted": len(expired),
                        "orphan_candidates": len(orphaned),
                        "tts_cache_pruned": len(tts_expired),
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


async def _resolve_tts_ttl_days(session: Any) -> int:
    """Read ``tts.cache_ttl_days`` setting with sane defaults."""
    raw = await get_setting_value(session, "tts.cache_ttl_days", _DEFAULT_TTS_TTL_DAYS)
    try:
        days = int(raw) if raw is not None else _DEFAULT_TTS_TTL_DAYS
    except (TypeError, ValueError):
        return _DEFAULT_TTS_TTL_DAYS
    return max(1, days)

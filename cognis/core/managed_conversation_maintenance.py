"""Periodic retention cleanup for managed conversations."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cognis.logging import get_logger
from cognis.store.queries import (
    close_managed_conversation_link_for_retention,
    get_inactive_managed_conversation_link,
    get_setting_value,
    list_inactive_managed_conversation_links,
)

logger = get_logger(__name__)

_DEFAULT_RETENTION_DAYS = 7


@dataclass(frozen=True)
class ManagedConversationMaintenanceResult:
    """Summary of one managed conversation cleanup pass."""

    retention_days: int | None
    candidates: int
    closed: int
    cancelled_turns: int
    skipped_active: int


class ManagedConversationMaintenanceService:
    """Close stale managed conversation links after a configurable retention window."""

    def __init__(
        self,
        *,
        session_factory: Any,
        turn_scheduler: Any,
        interval_seconds: int = 3600,
        batch_limit: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._turn_scheduler = turn_scheduler
        self._interval_seconds = interval_seconds
        self._batch_limit = batch_limit
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="managed-conversation-maintenance",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> ManagedConversationMaintenanceResult:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            retention_days = await _resolve_retention_days(session)
            if retention_days is None:
                return ManagedConversationMaintenanceResult(
                    retention_days=None,
                    candidates=0,
                    closed=0,
                    cancelled_turns=0,
                    skipped_active=0,
                )
            cutoff = now - timedelta(days=retention_days)
            candidates = await list_inactive_managed_conversation_links(
                session,
                older_than=cutoff,
                limit=self._batch_limit,
            )

        closed = 0
        cancelled_turns = 0
        skipped_active = 0
        reason = f"Closed automatically after {retention_days} days without activity"

        for listed_candidate in candidates:
            async with self._session_factory() as session:
                candidate = await get_inactive_managed_conversation_link(
                    session,
                    listed_candidate.link_id,
                    older_than=cutoff,
                )
            if candidate is None:
                continue

            target_conversation_id = candidate.target_conversation_id
            stale_active_turn_id = candidate.active_turn_id
            should_cancel_turn = bool(stale_active_turn_id) or candidate.turn_state == "running"
            if not should_cancel_turn:
                should_cancel_turn = bool(
                    self._turn_scheduler.has_active_turn(target_conversation_id)
                )

            if should_cancel_turn:
                if not self._safe_to_cancel_stale_turn(
                    target_conversation_id,
                    stale_active_turn_id,
                ):
                    skipped_active += 1
                    logger.warning(
                        "managed conversation retention skipped mismatched active turn",
                        extra={
                            "extra_data": {
                                "link_id": candidate.link_id,
                                "target_conversation_id": target_conversation_id,
                                "active_turn_id": stale_active_turn_id,
                            }
                        },
                    )
                    continue
                cancelled = await self._turn_scheduler.cancel_turn(target_conversation_id)
                if cancelled:
                    cancelled_turns += 1
                await self._wait_for_cancelled_turn(target_conversation_id)
                if self._turn_scheduler.has_active_turn(target_conversation_id):
                    skipped_active += 1
                    logger.warning(
                        "managed conversation retention skipped active stale turn",
                        extra={
                            "extra_data": {
                                "link_id": candidate.link_id,
                                "target_conversation_id": target_conversation_id,
                            }
                        },
                    )
                    continue

            async with self._session_factory() as session:
                row = await close_managed_conversation_link_for_retention(
                    session,
                    candidate.link_id,
                    reason=reason,
                    closed_at=now,
                    older_than=None if should_cancel_turn else cutoff,
                    expected_active_turn_id=stale_active_turn_id if should_cancel_turn else None,
                )
                if row is not None and row.conversation_state == "closed":
                    closed += 1
                    await session.commit()

        if closed or cancelled_turns or skipped_active:
            logger.info(
                "managed conversation maintenance completed",
                extra={
                    "extra_data": {
                        "retention_days": retention_days,
                        "candidates": len(candidates),
                        "closed": closed,
                        "cancelled_turns": cancelled_turns,
                        "skipped_active": skipped_active,
                    }
                },
            )

        return ManagedConversationMaintenanceResult(
            retention_days=retention_days,
            candidates=len(candidates),
            closed=closed,
            cancelled_turns=cancelled_turns,
            skipped_active=skipped_active,
        )

    def _safe_to_cancel_stale_turn(
        self,
        conversation_id: str,
        stale_active_turn_id: str | None,
    ) -> bool:
        checkpoint = self._turn_scheduler.active_turn_checkpoint(conversation_id)
        if checkpoint is None:
            return True
        running_turn_id = checkpoint.get("turn_id")
        return bool(stale_active_turn_id) and running_turn_id == stale_active_turn_id

    async def _wait_for_cancelled_turn(self, conversation_id: str) -> None:
        for _ in range(10):
            if not self._turn_scheduler.has_active_turn(conversation_id):
                return
            await asyncio.sleep(0.1)

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.warning("managed conversation maintenance failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


async def _resolve_retention_days(session: Any) -> int | None:
    """Read managed conversation retention; non-positive values disable cleanup."""

    raw = await get_setting_value(
        session,
        "managed_conversations.cleanup_retention_days",
        _DEFAULT_RETENTION_DAYS,
    )
    if isinstance(raw, bool):
        days = _DEFAULT_RETENTION_DAYS
    elif isinstance(raw, (int, float, str)):
        try:
            days = int(raw)
        except ValueError:
            days = _DEFAULT_RETENTION_DAYS
    else:
        days = _DEFAULT_RETENTION_DAYS
    if days <= 0:
        return None
    return days

"""Durable, idempotent Intaris dispatch for executor pin transition notices."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from cognis.core.events import Event, EventType
from cognis.models.session import SessionEvent
from cognis.store.models import Conversation, ExecutorPinNoticeOutboxRow, ExecutorPinTransitionRow


class ExecutorPinNoticeDispatcher:
    """Drain the outbox using one stable Intaris idempotency key per generation."""

    def __init__(self, *, session_factory: Any, guardrails: Any, event_bus: Any = None) -> None:
        self.session_factory = session_factory
        self.guardrails = guardrails
        self.event_bus = event_bus

    async def dispatch_pending(self, *, limit: int = 50) -> int:
        delivered = 0
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ExecutorPinNoticeOutboxRow)
                        .where(ExecutorPinNoticeOutboxRow.delivered_at.is_(None))
                        .order_by(ExecutorPinNoticeOutboxRow.created_at)
                        .limit(limit)
                    )
                ).all()
            )
        for row in rows:
            if await self.dispatch_one(row.outbox_id):
                delivered += 1
        return delivered

    async def dispatch_one(self, outbox_id: str) -> bool:
        async with self.session_factory() as session:
            row = await session.get(ExecutorPinNoticeOutboxRow, outbox_id)
            if row is None or row.delivered_at is not None:
                return row is not None
            transition = await session.get(ExecutorPinTransitionRow, row.transition_id)
            if transition is None:
                return False
            conversation = await session.get(Conversation, row.conversation_id)
            if conversation is None:
                return False
            intaris_session_id = getattr(row, "intaris_session_id", None)
            if not intaris_session_id:
                return False
            payload = dict(row.payload or {})
            payload.setdefault("session_id", intaris_session_id)
            payload.setdefault("message", payload.get("text") or "")
            payload.setdefault("text", payload.get("message") or "")
            event = SessionEvent(type="lifecycle", data=payload)
            key = getattr(row, "idempotency_key", None) or (
                f"{intaris_session_id}:executor_failover:{transition.notice_id}"
            )
            try:
                result = await self.guardrails.record_events(
                    session_id=intaris_session_id,
                    events=[event],
                    source="cognis_executor_failover",
                    idempotency_key=key,
                    user_email=getattr(row, "user_email", None),
                    agent_id=getattr(row, "agent_id", None),
                )
                if not getattr(result, "ok", True):
                    return False
            except Exception:
                return False
            now = datetime.now(UTC)
            updated = await session.execute(
                update(ExecutorPinNoticeOutboxRow)
                .where(
                    ExecutorPinNoticeOutboxRow.outbox_id == outbox_id,
                    ExecutorPinNoticeOutboxRow.delivered_at.is_(None),
                )
                .values(delivered_at=now)
            )
            await session.execute(
                update(ExecutorPinTransitionRow)
                .where(
                    ExecutorPinTransitionRow.transition_id == row.transition_id,
                    ExecutorPinTransitionRow.notice_appended_at.is_(None),
                )
                .values(notice_appended_at=now)
            )
            await session.commit()
            if updated.rowcount:
                cluster_signals = getattr(self, "cluster_signals", None)
                if cluster_signals is not None:
                    if transition.scope_type == "task":
                        await cluster_signals.publish_task_change(transition.scope_id)
                    elif transition.scope_type == "conversation":
                        await cluster_signals.publish_chat_change(
                            transition.scope_id,
                            session_id=intaris_session_id,
                            revision=transition.generation,
                        )
                await self._publish_ui_notice(row.conversation_id, payload)
            return True

    async def _publish_ui_notice(self, conversation_id: str, payload: dict[str, Any]) -> None:
        if self.event_bus is None:
            return
        with contextlib.suppress(Exception):
            await self.event_bus.publish(
                Event(
                    type=EventType.SYSTEM_NOTICE,
                    data={"conversation_id": conversation_id, **payload},
                )
            )

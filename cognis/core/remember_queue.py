"""Bounded async retry queue for failed Mnemory remember() calls."""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import sqlalchemy as sa
from prometheus_client import Counter, Gauge

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.core.attachment_utils import merge_content_and_attachment_note
from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.session import SessionEvent, with_session_events_turn_id
from cognis.runtime_context import scoped_runtime_context
from cognis.store.models import Agent, RememberQueueRow, Session

logger = get_logger(__name__)

_QUEUE_ONLY_PAYLOAD_FIELDS = frozenset(
    {
        "originating_memory_backend",
        "originating_agent_profile_id",
        "memory_policy_fingerprint",
    }
)

QUEUE_DEPTH = Gauge("cognis_remember_queue_depth", "Current remember queue depth")
QUEUE_DROPPED = Counter("cognis_remember_queue_dropped_total", "Dropped remember queue items")
QUEUE_FAILED = Counter("cognis_remember_queue_failed_total", "Failed remember queue items")
QUEUE_SUCCESS = Counter("cognis_remember_queue_success_total", "Successful remember queue items")
QUEUE_REPLAYED = Counter(
    "cognis_remember_queue_replayed_total",
    "Durably persisted remember queue items replayed after restart or retry",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@dataclass(slots=True)
class RememberQueueItem:
    payload: dict[str, Any]
    attempts: int = 0
    next_retry_at: float = field(default_factory=monotonic)
    item_id: str | None = None
    lease_token: str | None = None
    created_at: datetime | None = None


class RememberRetryQueue:
    """Retry queue with durable DB-backed mode and in-memory fallback.

    Production app wiring passes a SQLAlchemy ``session_factory`` so queued
    remember work survives restart. Narrow unit tests may omit it; in that case,
    the queue falls back to the original in-memory behavior.
    """

    def __init__(
        self,
        worker: Any,
        session_factory: Callable[[], Any] | None = None,
        event_reader: Any | None = None,
        event_bus: EventBus | None = None,
        max_depth: int = 100,
        max_concurrent: int = 5,
    ) -> None:
        self.worker = worker
        self._session_factory = session_factory
        self._event_reader = event_reader
        self._event_bus = event_bus
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.max_retries = 5
        self.backoff_max = 60.0
        self.lease_seconds = 60
        self._items: deque[RememberQueueItem] = deque()
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started_at = _utcnow()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()

    async def enqueue(self, payload: dict[str, Any]) -> None:
        if self._session_factory is None:
            await self._enqueue_in_memory(payload)
            return
        await self._enqueue_durable(payload)

    async def _enqueue_in_memory(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            if len(self._items) >= self.max_depth:
                self._items.popleft()
                QUEUE_DROPPED.inc()
                logger.warning("Remember queue overflow; dropped oldest item")
            self._items.append(RememberQueueItem(payload=payload))
            QUEUE_DEPTH.set(len(self._items))

    async def _enqueue_durable(self, payload: dict[str, Any]) -> None:
        now = _utcnow()
        durable_payload = self._durable_payload(payload)
        row = RememberQueueRow(
            item_id=f"rq_{uuid.uuid4().hex}",
            session_id=str(durable_payload.get("session_id") or ""),
            user_email=str(durable_payload.get("user_email") or ""),
            agent_id=(
                str(durable_payload.get("agent_id"))
                if durable_payload.get("agent_id") is not None
                else None
            ),
            payload=durable_payload,
            status="pending",
            attempts=0,
            next_retry_at=now,
        )
        async with self._session_factory() as session:
            await self._trim_durable_overflow(session)
            session.add(row)
            await session.commit()
            await self._update_durable_depth_metric(session)

    async def _trim_durable_overflow(self, session: Any) -> None:
        count = await session.scalar(sa.select(sa.func.count()).select_from(RememberQueueRow))
        if not isinstance(count, int) or count < self.max_depth:
            return
        overflow = count - self.max_depth + 1
        rows = (
            (
                await session.execute(
                    sa.select(RememberQueueRow)
                    .where(RememberQueueRow.status.in_(["pending", "failed"]))
                    .order_by(RememberQueueRow.created_at.asc())
                    .limit(max(overflow, 1))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await session.delete(row)
            QUEUE_DROPPED.inc()
        if rows:
            logger.warning(
                "Remember queue overflow; dropped oldest persisted items",
                extra={"extra_data": {"dropped": len(rows)}},
            )

    async def _drain_loop(self) -> None:
        semaphore = asyncio.Semaphore(self.max_concurrent)
        while True:
            if self._stop_event.is_set():
                if self._session_factory is None:
                    if not self._items:
                        break
                elif not await self._has_durable_work():
                    break

            ready = (
                await self._claim_due_durable_items(self.max_concurrent)
                if self._session_factory is not None
                else await self._collect_ready_in_memory()
            )
            if not ready:
                await asyncio.sleep(0.1)
                continue
            await asyncio.gather(*(self._process(item, semaphore) for item in ready))

    async def _collect_ready_in_memory(self) -> list[RememberQueueItem]:
        ready: list[RememberQueueItem] = []
        async with self._lock:
            now = monotonic()
            remaining: deque[RememberQueueItem] = deque()
            while self._items:
                item = self._items.popleft()
                if item.next_retry_at <= now:
                    ready.append(item)
                else:
                    remaining.append(item)
            self._items = remaining
            QUEUE_DEPTH.set(len(self._items))
        return ready

    async def _claim_due_durable_items(self, limit: int) -> list[RememberQueueItem]:
        if self._session_factory is None:
            return []
        now = _utcnow()
        claimed: list[RememberQueueItem] = []
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(RememberQueueRow)
                        .where(
                            sa.or_(
                                sa.and_(
                                    RememberQueueRow.status == "pending",
                                    RememberQueueRow.next_retry_at <= now,
                                ),
                                sa.and_(
                                    RememberQueueRow.status == "leased",
                                    RememberQueueRow.lease_expires_at.is_not(None),
                                    RememberQueueRow.lease_expires_at <= now,
                                ),
                            )
                        )
                        .order_by(
                            RememberQueueRow.next_retry_at.asc(), RememberQueueRow.created_at.asc()
                        )
                        .limit(limit * 4)
                    )
                )
                .scalars()
                .all()
            )

            for row in rows:
                lease_token = uuid.uuid4().hex
                updated = await session.execute(
                    sa.update(RememberQueueRow)
                    .execution_options(synchronize_session=False)
                    .where(
                        RememberQueueRow.item_id == row.item_id,
                        sa.or_(
                            sa.and_(
                                RememberQueueRow.status == "pending",
                                RememberQueueRow.next_retry_at <= now,
                            ),
                            sa.and_(
                                RememberQueueRow.status == "leased",
                                RememberQueueRow.lease_expires_at.is_not(None),
                                RememberQueueRow.lease_expires_at <= now,
                            ),
                        ),
                    )
                    .values(
                        status="leased",
                        lease_token=lease_token,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                        updated_at=now,
                    )
                )
                if not updated.rowcount:
                    continue
                claimed.append(
                    RememberQueueItem(
                        item_id=row.item_id,
                        payload=dict(row.payload or {}),
                        attempts=row.attempts,
                        lease_token=lease_token,
                        created_at=row.created_at,
                    )
                )
                created_at = _normalize_utc(row.created_at)
                if (created_at is not None and created_at < self._started_at) or row.attempts > 0:
                    QUEUE_REPLAYED.inc()
                if len(claimed) >= limit:
                    break
            await session.commit()
            await self._update_durable_depth_metric(session)
        return claimed

    async def _process(self, item: RememberQueueItem, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            try:
                if await self._hard_memory_disable_applies(item.payload):
                    QUEUE_SUCCESS.inc()
                    if self._session_factory is not None and item.item_id is not None:
                        async with self._session_factory() as session:
                            await session.execute(
                                sa.delete(RememberQueueRow)
                                .where(
                                    RememberQueueRow.item_id == item.item_id,
                                    RememberQueueRow.lease_token == item.lease_token,
                                )
                                .execution_options(synchronize_session=False)
                            )
                            await session.commit()
                            await self._update_durable_depth_metric(session)
                    return
                resolved_payload = await self._resolve_payload(item.payload)
                await self.worker.remember(**resolved_payload)
                QUEUE_SUCCESS.inc()
                if self._session_factory is None or item.item_id is None:
                    return
                async with self._session_factory() as session:
                    await session.execute(
                        sa.delete(RememberQueueRow)
                        .where(
                            RememberQueueRow.item_id == item.item_id,
                            RememberQueueRow.lease_token == item.lease_token,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    await session.commit()
                    await self._update_durable_depth_metric(session)
            except Exception as exc:
                item.attempts += 1
                if self._session_factory is None or item.item_id is None:
                    if item.attempts >= self.max_retries:
                        QUEUE_FAILED.inc()
                        logger.exception(
                            "Remember queue item failed permanently",
                            extra={
                                "extra_data": {
                                    "item_id": item.item_id,
                                    "session_id": item.payload.get("session_id"),
                                    "user_email": item.payload.get("user_email"),
                                    "attempts": item.attempts,
                                    "last_error": self._sanitize_failure_detail(exc),
                                }
                            },
                        )
                        return
                    item.next_retry_at = monotonic() + min(2**item.attempts, self.backoff_max)
                    async with self._lock:
                        self._items.append(item)
                        QUEUE_DEPTH.set(len(self._items))
                    return

                next_retry_at = _utcnow() + timedelta(
                    seconds=min(2**item.attempts, self.backoff_max)
                )
                status = "failed" if item.attempts >= self.max_retries else "pending"
                if status == "failed":
                    QUEUE_FAILED.inc()
                    last_error = self._sanitize_failure_detail(exc)
                    logger.exception(
                        "Remember queue item failed permanently",
                        extra={
                            "extra_data": {
                                "item_id": item.item_id,
                                "session_id": item.payload.get("session_id"),
                                "user_email": item.payload.get("user_email"),
                                "attempts": item.attempts,
                                "last_error": last_error,
                            }
                        },
                    )
                else:
                    last_error = self._sanitize_failure_detail(exc)
                async with self._session_factory() as session:
                    await session.execute(
                        sa.update(RememberQueueRow)
                        .execution_options(synchronize_session=False)
                        .where(
                            RememberQueueRow.item_id == item.item_id,
                            RememberQueueRow.lease_token == item.lease_token,
                        )
                        .values(
                            status=status,
                            attempts=item.attempts,
                            next_retry_at=next_retry_at,
                            lease_token=None,
                            lease_expires_at=None,
                            last_error=last_error,
                            updated_at=_utcnow(),
                        )
                    )
                    await session.commit()
                    await self._update_durable_depth_metric(session)
                if status == "failed":
                    await self._record_failure_notice(item, last_error)

    async def _hard_memory_disable_applies(self, payload: dict[str, Any]) -> bool:
        """Recheck current hard backend/profile vetoes before queued execution."""

        if self._session_factory is None:
            return False
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return False
        async with self._session_factory() as session:
            row = (
                await session.execute(sa.select(Agent).where(Agent.agent_id == agent_id).limit(1))
            ).scalar_one_or_none()
        if row is None:
            return False
        capabilities = row.capabilities if isinstance(row.capabilities, dict) else {}
        backend_id = capabilities.get("memory_backend", "mnemory")
        if backend_id == "none":
            return True
        if not isinstance(backend_id, str):
            return True
        from cognis.providers.backends import get_backend

        try:
            get_backend("memory", backend_id)
        except ValueError:
            return True
        profile_id = payload.get("originating_agent_profile_id")
        profiles = row.agent_profiles if isinstance(row.agent_profiles, dict) else {}
        profile = profiles.get(profile_id) if isinstance(profile_id, str) else None
        return isinstance(profile, dict) and profile.get("memory_enabled") is False

    async def _has_durable_work(self) -> bool:
        if self._session_factory is None:
            return False
        async with self._session_factory() as session:
            count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(RememberQueueRow)
                .where(RememberQueueRow.status.in_(["pending", "leased"]))
            )
            return bool(count)

    async def _update_durable_depth_metric(self, session: Any) -> None:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(RememberQueueRow)
            .where(RememberQueueRow.status.in_(["pending", "leased"]))
        )
        QUEUE_DEPTH.set(int(count or 0))

    @staticmethod
    def _durable_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if "messages" not in payload:
            return dict(payload)
        durable_payload = dict(payload)
        durable_payload.pop("messages", None)
        return durable_payload

    async def _resolve_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "messages" in payload:
            return {
                key: value
                for key, value in payload.items()
                if key not in _QUEUE_ONLY_PAYLOAD_FIELDS
            }
        if self._event_reader is None:
            raise RuntimeError("Remember queue replay requires an Intaris event reader")

        intaris_session_id = str(
            payload.get("intaris_session_id") or payload.get("source_intaris_session_id") or ""
        ).strip()
        mnemory_session_id = str(payload.get("session_id") or "").strip()
        if not intaris_session_id or not mnemory_session_id:
            raise RuntimeError("Remember queue item is missing session references")

        user_email = payload.get("user_email")
        agent_id = payload.get("agent_id")
        agent_owner_email = payload.get("agent_owner_email")
        include_user_message = bool(payload.get("include_user_message", True))
        user_event_seq = payload.get("user_event_seq")
        assistant_event_seq = payload.get("assistant_event_seq")
        requested_seqs = [
            int(seq)
            for seq in (user_event_seq, assistant_event_seq)
            if isinstance(seq, int) and seq > 0
        ]
        after_seq = max(0, min(requested_seqs) - 1) if requested_seqs else 0

        with scoped_runtime_context(
            user_email=user_email,
            agent_id=agent_id,
            agent_owner_email=str(agent_owner_email) if agent_owner_email else None,
        ):
            event_read = await self._read_replay_events(
                intaris_session_id=intaris_session_id,
                requested_seqs=requested_seqs,
                after_seq=after_seq,
            )

        messages: list[dict[str, str]] = []
        if include_user_message:
            for event in reversed(event_read.events):
                event_type, event_seq, event_data = self._normalize_replay_event(event)
                if event_type != "user_message":
                    continue
                if isinstance(user_event_seq, int) and event_seq != user_event_seq:
                    continue
                content = merge_content_and_attachment_note(
                    str(event_data.get("content", "")),
                    [a for a in event_data.get("attachments", []) if isinstance(a, dict)],
                ).strip()
                if content:
                    messages.append({"role": "user", "content": content[:5000]})
                    break

        for event in reversed(event_read.events):
            event_type, event_seq, event_data = self._normalize_replay_event(event)
            if event_type != "assistant_message":
                continue
            if isinstance(assistant_event_seq, int) and event_seq != assistant_event_seq:
                continue
            content = merge_content_and_attachment_note(
                str(event_data.get("content", "")),
                [a for a in event_data.get("attachments", []) if isinstance(a, dict)],
            ).strip()
            if content:
                messages.append({"role": "assistant", "content": content[:5000]})
                break

        if not messages or all(message["role"] != "assistant" for message in messages):
            raise RuntimeError("Could not reconstruct remember payload from Intaris events")

        return {
            "session_id": mnemory_session_id,
            "messages": messages,
            "user_email": user_email,
            "agent_id": agent_id,
            "agent_owner_email": agent_owner_email,
        }

    async def _read_replay_events(
        self,
        *,
        intaris_session_id: str,
        requested_seqs: list[int],
        after_seq: int,
    ) -> Any:
        if requested_seqs:
            try:
                return await self._event_reader.read_events(
                    session_id=intaris_session_id,
                    seqs=requested_seqs,
                    types=["user_message", "assistant_message"],
                    allow_missing_stream=True,
                )
            except TypeError as exc:
                if "seqs" not in str(exc):
                    raise

        return await self._event_reader.read_events(
            session_id=intaris_session_id,
            after_seq=after_seq,
            limit=max(20, len(requested_seqs) + 4),
            types=["user_message", "assistant_message"],
            allow_missing_stream=True,
        )

    @staticmethod
    def _normalize_replay_event(event: Any) -> tuple[str, int | None, dict[str, Any]]:
        """Return ``(type, seq, data)`` for dict or object-shaped Intaris events."""
        if isinstance(event, dict):
            event_type = str(event.get("type") or "")
            raw_seq = event.get("seq")
            event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
            return event_type, raw_seq if isinstance(raw_seq, int) else None, event_data

        event_type = str(getattr(event, "type", "") or "")
        raw_seq = getattr(event, "seq", None)
        raw_data = getattr(event, "data", None)
        event_data = raw_data if isinstance(raw_data, dict) else {}
        return event_type, raw_seq if isinstance(raw_seq, int) else None, event_data

    @staticmethod
    def _sanitize_failure_detail(error: Exception) -> str:
        """Return a short safe error detail for logs and user-facing notices."""
        return sanitize_client_error_detail(error, fallback="Memory provider unavailable")[:500]

    async def _record_failure_notice(self, item: RememberQueueItem, last_error: str) -> None:
        """Record a session-scoped system notice for permanent remember failure."""
        session_ref = await self._resolve_session_notice_context(item.payload)
        if session_ref is None:
            return

        message = (
            "Background memory save failed after several retries. "
            "The assistant may not remember some details from this session. "
            f"Reason: {last_error}"
        )
        user_email = str(item.payload.get("user_email") or "") or None
        agent_id = str(item.payload.get("agent_id") or "") or None
        try:
            if self._event_reader is not None and hasattr(self._event_reader, "record_events"):
                with scoped_runtime_context(user_email=user_email, agent_id=agent_id):
                    await self._event_reader.record_events(
                        session_id=session_ref["intaris_session_id"],
                        events=with_session_events_turn_id(
                            [
                                SessionEvent(
                                    type="lifecycle",
                                    data={
                                        "event": "system_notice",
                                        "message": message,
                                        "source": "remember_queue",
                                        "item_id": item.item_id,
                                    },
                                )
                            ],
                            None,
                        ),
                        source="cognis",
                        idempotency_key=f"remember-failed:{item.item_id}",
                    )
            if self._event_bus is not None:
                await self._event_bus.publish(
                    Event(
                        type=EventType.SYSTEM_NOTICE,
                        data={
                            "conversation_id": session_ref["conversation_id"],
                            "session_id": session_ref["session_id"],
                            "message": message,
                            "source": "remember_queue",
                            "item_id": item.item_id,
                        },
                    )
                )
        except Exception:
            logger.exception(
                "Failed to record remember queue failure notice",
                extra={
                    "extra_data": {
                        "item_id": item.item_id,
                        "session_id": session_ref["session_id"],
                        "conversation_id": session_ref["conversation_id"],
                    }
                },
            )

    async def _resolve_session_notice_context(
        self, payload: dict[str, Any]
    ) -> dict[str, str] | None:
        """Resolve Cognis + Intaris session ids and conversation id for notices."""
        if self._session_factory is None:
            return None
        cognis_session_id = str(payload.get("cognis_session_id") or "").strip()
        intaris_session_id = str(
            payload.get("intaris_session_id") or payload.get("source_intaris_session_id") or ""
        ).strip()
        if not cognis_session_id and not intaris_session_id:
            return None

        async with self._session_factory() as session:
            stmt = sa.select(Session)
            if cognis_session_id:
                stmt = stmt.where(Session.session_id == cognis_session_id)
            else:
                stmt = stmt.where(
                    sa.or_(
                        Session.intaris_session_id == intaris_session_id,
                        Session.session_id == intaris_session_id,
                    )
                )
            row = (await session.execute(stmt.limit(1))).scalar_one_or_none()
        if row is None:
            return None
        resolved_intaris_id = str(row.intaris_session_id or row.session_id or "").strip()
        if not resolved_intaris_id:
            return None
        return {
            "session_id": row.session_id,
            "conversation_id": row.conversation_id,
            "intaris_session_id": resolved_intaris_id,
        }

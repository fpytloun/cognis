"""Bounded async retry queue for failed Mnemory remember() calls."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Literal

import sqlalchemy as sa
from prometheus_client import Counter, Gauge
from sqlalchemy.exc import IntegrityError

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.core.attachment_utils import merge_content_and_attachment_note
from cognis.core.events import Event, EventBus, EventType
from cognis.core.trusted_evidence import (
    EVIDENCE_POLICY_MISMATCH,
    EVIDENCE_POLICY_MISMATCH_TOTAL,
    EVIDENCE_QUEUE_KIND,
    ORDINARY_ASSISTANT_QUEUE_KIND,
    ORDINARY_USER_QUEUE_KIND,
    TERMINAL_EVIDENCE_OUTCOMES,
    TRUSTED_EVIDENCE_ADMISSION_KEY,
    deserialize_evidence_admission,
    deterministic_queue_id,
    event_hash,
    evidence_admission_authorizes,
    marker_for_event,
    marker_is_valid,
    marker_matches_event,
    observe_outcome,
    observe_queue_age,
    serialize_evidence_admission,
    sha256_hex,
)
from cognis.logging import get_logger
from cognis.models.session import SessionEvent, with_session_events_turn_id
from cognis.providers.memory.evidence import (
    EvidenceRememberRequest,
    TrustedEventRejectedError,
    UserEventRememberRequest,
)
from cognis.providers.memory.protocol import RememberOutcomeUnknownError
from cognis.runtime_context import scoped_runtime_context
from cognis.store.coordination import database_now
from cognis.store.models import Agent, Conversation, RememberQueueRow, Session

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
EVIDENCE_CAPACITY_UNAVAILABLE = Counter(
    "cognis_trusted_evidence_capacity_unavailable_total",
    "Trusted evidence events terminalized because active queue capacity was full.",
)


def _queue_identity(payload: dict[str, Any]) -> tuple[str | None, ...]:
    return (
        str(payload.get("queue_kind")) if payload.get("queue_kind") is not None else None,
        str(payload.get("event_hash")) if payload.get("event_hash") is not None else None,
        (
            str(payload.get("assistant_event_hash"))
            if payload.get("assistant_event_hash") is not None
            else None
        ),
    )


def _identity_fingerprint(payload: dict[str, Any]) -> str:
    identity = "\x1f".join(value or "<missing>" for value in _queue_identity(payload))
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def _value_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _parse_marker_time(marker: dict[str, Any]) -> datetime | None:
    raw = marker.get("admitted_at") or marker.get("marker_admitted_at")
    if not isinstance(raw, str):
        return None
    try:
        return _normalize_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


@dataclass(slots=True)
class RememberQueueItem:
    payload: dict[str, Any]
    attempts: int = 0
    next_retry_at: float = field(default_factory=monotonic)
    item_id: str | None = None
    lease_token: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _EvidenceSigningInputs:
    body: EvidenceRememberRequest
    token: str


@dataclass(frozen=True, slots=True)
class _ReconciliationSession:
    session_id: str
    intaris_session_id: str
    mnemory_session_id: str | None
    conversation_id: str
    user_email: str
    agent_id: str
    agent_profile_id: str | None
    owner_email: str | None


@dataclass(slots=True)
class _SessionReconciliationProgress:
    after_seq: int = 0
    pending_markers: dict[str, tuple[dict[str, Any], str, int, str]] = field(default_factory=dict)
    assistant_ready_turns: deque[str] = field(default_factory=deque)


@dataclass(slots=True)
class _EvidenceReconciliationState:
    terminal_cursor: str | None = None
    rotation_repair_cursor: str | None = None
    session_cursor: str | None = None
    session_progress: dict[str, _SessionReconciliationProgress] = field(default_factory=dict)
    pass_ok: bool = True


class _EvidenceUnavailableError(RuntimeError):
    """Evidence authority changed or is no longer selected for minting."""


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
        recovery_interval_seconds: float = 30.0,
        trusted_evidence_enabled: bool = False,
        trusted_evidence_owner_allowlist: tuple[str, ...] = (),
        trusted_evidence_max_attempts: int = 8,
        trusted_evidence_max_age_seconds: int = 3600,
        trusted_evidence_policy_fingerprint: str = "",
        trusted_evidence_admission_key: bytes = b"",
    ) -> None:
        self.worker = worker
        self._session_factory = session_factory
        self._event_reader = event_reader
        self._event_bus = event_bus
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.recovery_interval_seconds = recovery_interval_seconds
        self.max_retries = 5
        self.backoff_max = 60.0
        self.lease_seconds = 60
        self.trusted_evidence_enabled = trusted_evidence_enabled
        self._trusted_evidence_owner_allowlist = tuple(trusted_evidence_owner_allowlist)
        self.trusted_evidence_max_attempts = trusted_evidence_max_attempts
        self.trusted_evidence_max_age_seconds = trusted_evidence_max_age_seconds
        self.trusted_evidence_policy_fingerprint = trusted_evidence_policy_fingerprint
        self._trusted_evidence_admission_key = trusted_evidence_admission_key
        self._policy_mismatch_active = False
        self._policy_mismatch_logged = False
        self._durable_active_depth = 0
        self._items: deque[RememberQueueItem] = deque()
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._wake_generation = 0
        self._task: asyncio.Task[None] | None = None
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._ledger_repair_task: asyncio.Task[None] | None = None
        self._reconciliation_lock = asyncio.Lock()
        self._ledger_repair_lock = asyncio.Lock()
        self._ledger_wake_event = asyncio.Event()
        self._ledger_wake_generation = 0
        self._reconciliation_state = _EvidenceReconciliationState()
        self._reconciliation_ledger_batch_size = 50
        self._reconciliation_session_batch_size = 8
        self._reconciliation_page_batch_size = 4
        self._rotation_repair_batch_size = 20
        self._reconciliation_read_timeout_seconds = 2.0
        self._reconciliation_stage_timeout_seconds = 2.25
        self._reconciliation_cycle_timeout_seconds = 5.0
        self._ledger_repair_min_interval_seconds = 0.1
        self._prefer_evidence_claim = False
        self._started_at = _utcnow()
        self._reconciliation_pass_ok = False

    @property
    def trusted_evidence_owner_allowlist(self) -> tuple[str, ...]:
        """Return the process-start owner selection without a runtime setter."""
        return self._trusted_evidence_owner_allowlist

    @property
    def policy_mismatch_active(self) -> bool:
        """Return whether active evidence rows use another admission policy."""
        return self._policy_mismatch_active

    @property
    def active_depth(self) -> int:
        """Return the last database-backed active queue depth."""
        return self._durable_active_depth if self._session_factory is not None else len(self._items)

    async def start(self) -> None:
        if self._task is None:
            self._stop_event.clear()
            self._signal_wake()
            self._task = asyncio.create_task(self._drain_loop())
            if self._session_factory is not None:
                self._signal_ledger_repair()
                self._ledger_repair_task = asyncio.create_task(self._ledger_repair_loop())
                self._reconciliation_task = asyncio.create_task(self._reconciliation_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._session_factory is None:
            async with self._lock:
                for item in self._items:
                    item.next_retry_at = 0
        self._signal_wake()
        if self._reconciliation_task is not None:
            self._reconciliation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconciliation_task
            self._reconciliation_task = None
        if self._ledger_repair_task is not None:
            self._ledger_repair_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ledger_repair_task
            self._ledger_repair_task = None
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None

    async def enqueue(self, payload: dict[str, Any]) -> None:
        if self._session_factory is None:
            await self._enqueue_in_memory(payload)
        else:
            await self._enqueue_durable(payload)
        self._signal_wake()

    async def enqueue_after_user_append(
        self,
        *,
        session: Any,
        agent: Any,
        conversation_id: str,
        turn_id: str,
        event_seq: int,
        event_hash_value: str,
        owner_email: str,
        intaris_session_id_override: str | None = None,
        marker_admitted_at: str | None = None,
        evidence_admission: dict[str, Any] | None = None,
    ) -> None:
        """Atomically hand off append-owned evidence and user work."""
        admission = deserialize_evidence_admission(evidence_admission)
        if not evidence_admission_authorizes(
            admission,
            key=self._trusted_evidence_admission_key,
            owner_id=owner_email,
        ):
            return
        assert admission is not None
        common = {
            "session_id": getattr(session, "mnemory_session_id", None),
            "cognis_session_id": getattr(session, "session_id", None),
            "intaris_session_id": intaris_session_id_override
            or getattr(session, "intaris_session_id", None),
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "event_seq": event_seq,
            "event_hash": event_hash_value,
            "marker_admitted_at": marker_admitted_at,
            "user_email": getattr(session, "user_email", None),
            "owner_email": owner_email,
            "agent_owner_email": owner_email,
            "agent_id": getattr(agent, "agent_id", None),
            "policy_agent_id": getattr(agent, "agent_id", None),
            "originating_memory_backend": None,
            "originating_agent_profile_id": getattr(session, "agent_profile_id", None),
            "memory_policy_fingerprint": None,
            TRUSTED_EVIDENCE_ADMISSION_KEY: evidence_admission,
            "admission_event_id": admission.event_binding["event_id"],
        }
        evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash_value)
        if self._session_factory is None:
            await self.enqueue(
                {**common, "queue_kind": EVIDENCE_QUEUE_KIND, "item_id": evidence_id}
            )
            await self.enqueue(
                {
                    **common,
                    "queue_kind": ORDINARY_USER_QUEUE_KIND,
                    "item_id": deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, event_hash_value),
                    "depends_on": evidence_id,
                }
            )
            return
        await self._enqueue_durable_group(
            [
                {**common, "queue_kind": EVIDENCE_QUEUE_KIND, "item_id": evidence_id},
                {
                    **common,
                    "queue_kind": ORDINARY_USER_QUEUE_KIND,
                    "item_id": deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, event_hash_value),
                    "depends_on": evidence_id,
                },
            ]
        )

    async def _enqueue_durable_group(self, payloads: list[dict[str, Any]]) -> None:
        """Insert one evidence/user group in one transaction."""
        assert self._session_factory is not None
        async with self._session_factory() as session:
            now = await database_now(session)
            await self._serialize_capacity_transaction(session)
            evidence_payload = next(
                (
                    payload
                    for payload in payloads
                    if payload.get("queue_kind") == EVIDENCE_QUEUE_KIND
                ),
                None,
            )
            if evidence_payload is not None:
                admission_event_id = evidence_payload.get("admission_event_id")
                if not isinstance(admission_event_id, str) or not admission_event_id:
                    raise ValueError("evidence admission event identity is missing")
                existing_canonical = (
                    await session.execute(
                        sa.select(RememberQueueRow)
                        .where(
                            RememberQueueRow.payload["queue_kind"].as_string()
                            == EVIDENCE_QUEUE_KIND,
                            RememberQueueRow.payload["admission_event_id"].as_string()
                            == admission_event_id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing_canonical is not None:
                    canonical_payload = dict(existing_canonical.payload or {})
                    canonical_event_hash = canonical_payload.get("event_hash")
                    if isinstance(canonical_event_hash, str) and canonical_event_hash:
                        ordinary_id = deterministic_queue_id(
                            ORDINARY_USER_QUEUE_KIND,
                            canonical_event_hash,
                        )
                        ordinary_exists = await session.get(RememberQueueRow, ordinary_id)
                        if ordinary_exists is None:
                            if await self._active_depth(session) >= self.max_depth:
                                existing_canonical.payload = {
                                    **canonical_payload,
                                    "ordinary_work_needed": True,
                                }
                                existing_canonical.updated_at = now
                            else:
                                await self._insert_row(
                                    session,
                                    {
                                        **{
                                            k: v
                                            for k, v in canonical_payload.items()
                                            if k not in {"trusted_rejection", "rejected_source"}
                                        },
                                        "item_id": ordinary_id,
                                        "queue_kind": ORDINARY_USER_QUEUE_KIND,
                                        "depends_on": existing_canonical.item_id,
                                    },
                                    now,
                                    status="pending",
                                )
                                existing_canonical.payload = {
                                    **canonical_payload,
                                    "ordinary_work_needed": False,
                                }
                                existing_canonical.updated_at = now
                            await session.commit()
                            await self._update_durable_depth_metric(session)
                        elif bool(canonical_payload.get("ordinary_work_needed")):
                            existing_canonical.payload = {
                                **canonical_payload,
                                "ordinary_work_needed": False,
                            }
                            existing_canonical.updated_at = now
                            await session.commit()
                    return
            ids = [str(payload["item_id"]) for payload in payloads]
            existing = (
                (
                    await session.execute(
                        sa.select(RememberQueueRow).where(RememberQueueRow.item_id.in_(ids))
                    )
                )
                .scalars()
                .all()
            )
            if len(existing) == len(payloads):
                return
            if existing:
                existing_by_id = {row.item_id: row for row in existing}
                evidence_payload = payloads[0]
                ordinary_payload = payloads[1]
                evidence_row = existing_by_id.get(str(evidence_payload["item_id"]))
                ordinary_row = existing_by_id.get(str(ordinary_payload["item_id"]))
                if (
                    evidence_row is None
                    or ordinary_row is not None
                    or evidence_row.status != "unavailable"
                    or not bool((evidence_row.payload or {}).get("ordinary_work_needed"))
                    or _queue_identity(evidence_row.payload or {})
                    != _queue_identity(evidence_payload)
                ):
                    raise ValueError("partial trusted evidence queue group exists")
                if await self._active_depth(session) >= self.max_depth:
                    return
                await self._insert_row(session, ordinary_payload, now, status="pending")
                evidence_row.payload = {
                    **(evidence_row.payload or {}),
                    "ordinary_work_needed": False,
                }
                await session.commit()
                await self._update_durable_depth_metric(session)
                return
            active_count = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(RememberQueueRow)
                    .where(RememberQueueRow.status.in_(["pending", "leased", "dispatching"]))
                )
                or 0
            )
            active_new_count = sum(
                str(payload.get("status_override", "pending"))
                in {"pending", "leased", "dispatching"}
                for payload in payloads
            )
            if active_count + active_new_count > self.max_depth:
                evidence_payload = payloads[0]
                evidence_payload = {
                    **evidence_payload,
                    "capacity_fallback": True,
                    "ordinary_work_needed": True,
                }
                payloads = [evidence_payload, {**payloads[1], "depends_on": None}]
                await self._insert_row(
                    session,
                    evidence_payload,
                    now,
                    status="unavailable",
                )
                await session.commit()
                EVIDENCE_CAPACITY_UNAVAILABLE.inc()
                await self._update_durable_depth_metric(session)
                return
            rows = []
            for payload in payloads:
                status = str(payload.get("status_override", "pending"))
                rows.append(
                    RememberQueueRow(
                        item_id=str(payload["item_id"]),
                        session_id=str(payload.get("session_id") or ""),
                        user_email=str(payload.get("user_email") or ""),
                        agent_id=payload.get("agent_id"),
                        payload=self._durable_payload(dict(payload)),
                        status=status,
                        attempts=0,
                        next_retry_at=now,
                        last_error=(
                            "Trusted evidence queue capacity exhausted"
                            if status == "unavailable"
                            else None
                        ),
                    )
                )
            session.add_all(rows)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return
            await self._update_durable_depth_metric(session)

    @staticmethod
    async def _serialize_capacity_transaction(session: Any) -> None:
        dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
        if dialect == "postgresql":
            await session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext('cognis_trusted_evidence_queue'))")
            )
        elif dialect == "sqlite":
            await session.execute(sa.text("BEGIN IMMEDIATE"))

    @staticmethod
    async def _insert_row(
        session: Any,
        payload: dict[str, Any],
        now: datetime,
        *,
        status: str,
    ) -> None:
        session.add(
            RememberQueueRow(
                item_id=str(payload["item_id"]),
                session_id=str(payload.get("session_id") or ""),
                user_email=str(payload.get("user_email") or ""),
                agent_id=payload.get("agent_id"),
                payload=RememberRetryQueue._durable_payload(dict(payload)),
                status=status,
                attempts=0,
                next_retry_at=now,
                last_error=(
                    "Trusted evidence queue capacity exhausted" if status == "unavailable" else None
                ),
            )
        )

    def _signal_wake(self) -> None:
        self._wake_generation += 1
        self._wake_event.set()

    def _signal_ledger_repair(self) -> None:
        self._ledger_wake_generation += 1
        self._ledger_wake_event.set()

    async def _enqueue_in_memory(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            if len(self._items) >= self.max_depth:
                self._items.popleft()
                QUEUE_DROPPED.inc()
                logger.warning("Remember queue overflow; dropped oldest item")
            self._items.append(RememberQueueItem(payload=payload))
            QUEUE_DEPTH.set(len(self._items))

    async def _enqueue_durable(self, payload: dict[str, Any]) -> None:
        session_factory = self._session_factory
        if session_factory is None:
            raise RuntimeError("Durable remember queue requires a session factory")
        requested_status = str(payload.get("status_override", "pending"))
        durable_payload = self._durable_payload(payload)
        if durable_payload.get("queue_kind") == EVIDENCE_QUEUE_KIND:
            admission = deserialize_evidence_admission(
                durable_payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY)
            )
            if admission is not None:
                durable_payload.setdefault(
                    "admission_event_id",
                    admission.event_binding["event_id"],
                )
                durable_payload.setdefault("marker_admitted_at", admission.admitted_at)
        requested_item_id = durable_payload.get("item_id")
        item_id = (
            str(requested_item_id)
            if isinstance(requested_item_id, str) and requested_item_id
            else f"rq_{uuid.uuid4().hex}"
        )
        async with session_factory() as session:
            now = await database_now(session)
            await self._serialize_capacity_transaction(session)
            capacity_unavailable = False
            commit_succeeded = False
            admission_event_id = durable_payload.get("admission_event_id")
            if durable_payload.get("queue_kind") == EVIDENCE_QUEUE_KIND and isinstance(
                admission_event_id, str
            ):
                existing_canonical = (
                    await session.execute(
                        sa.select(RememberQueueRow.item_id)
                        .where(
                            RememberQueueRow.payload["queue_kind"].as_string()
                            == EVIDENCE_QUEUE_KIND,
                            RememberQueueRow.payload["admission_event_id"].as_string()
                            == admission_event_id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing_canonical is not None and existing_canonical != item_id:
                    return
            row = RememberQueueRow(
                item_id=item_id,
                session_id=str(durable_payload.get("session_id") or ""),
                user_email=str(durable_payload.get("user_email") or ""),
                agent_id=(
                    str(durable_payload.get("agent_id"))
                    if durable_payload.get("agent_id") is not None
                    else None
                ),
                payload=durable_payload,
                status=requested_status,
                attempts=0,
                next_retry_at=now,
            )
            if requested_item_id:
                existing = await session.get(RememberQueueRow, item_id)
                if existing is not None:
                    if _queue_identity(existing.payload or {}) != _queue_identity(durable_payload):
                        if await self._repair_failed_assistant_identity(
                            session,
                            existing=existing,
                            incoming_payload=durable_payload,
                            now=now,
                        ):
                            await session.commit()
                            await self._update_durable_depth_metric(session)
                            self._signal_wake()
                            return
                        logger.warning(
                            "Remember queue deterministic identity conflict",
                            extra={
                                "extra_data": {
                                    "conflict_class": "unknown_mismatch",
                                    "item_id_hash": _value_fingerprint(item_id),
                                    "existing_identity_hash": _identity_fingerprint(
                                        existing.payload or {}
                                    ),
                                    "incoming_identity_hash": _identity_fingerprint(
                                        durable_payload
                                    ),
                                }
                            },
                        )
                        raise ValueError("deterministic queue identity payload conflict")
                    if (
                        existing.status in TERMINAL_EVIDENCE_OUTCOMES
                        or existing.status == "completed"
                    ):
                        return
                    await self._update_durable_depth_metric(session)
                    return
            queue_kind = durable_payload.get("queue_kind")
            if (
                isinstance(requested_item_id, str)
                and durable_payload.get("queue_kind")
                in {
                    EVIDENCE_QUEUE_KIND,
                    ORDINARY_USER_QUEUE_KIND,
                    ORDINARY_ASSISTANT_QUEUE_KIND,
                }
                and await self._active_depth(session) >= self.max_depth
            ):
                if durable_payload.get("queue_kind") == EVIDENCE_QUEUE_KIND:
                    durable_payload = {
                        **durable_payload,
                        "ordinary_work_needed": True,
                    }
                    row.payload = durable_payload
                    row.status = "unavailable"
                    row.last_error = "Trusted evidence queue capacity exhausted"
                    capacity_unavailable = True
                else:
                    return
            elif queue_kind not in {
                EVIDENCE_QUEUE_KIND,
                ORDINARY_USER_QUEUE_KIND,
                ORDINARY_ASSISTANT_QUEUE_KIND,
            }:
                await self._trim_durable_overflow(session)
            session.add(row)
            try:
                await session.commit()
                commit_succeeded = True
            except IntegrityError:
                await session.rollback()
                if requested_item_id is None:
                    raise
            if capacity_unavailable and commit_succeeded:
                EVIDENCE_CAPACITY_UNAVAILABLE.inc()
            await self._update_durable_depth_metric(session)
        asyncio.get_running_loop().call_later(1.0, self._signal_wake)

    async def _trim_durable_overflow(self, session: Any) -> None:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(RememberQueueRow)
            .where(RememberQueueRow.status.in_(["pending", "leased", "dispatching"]))
        )
        if not isinstance(count, int) or count < self.max_depth:
            return
        overflow = count - self.max_depth + 1
        candidates = (
            (
                await session.execute(
                    sa.select(RememberQueueRow)
                    .where(RememberQueueRow.status == "pending")
                    .order_by(RememberQueueRow.created_at.asc())
                    .limit(max(overflow * 4, 4))
                )
            )
            .scalars()
            .all()
        )
        rows = [
            row
            for row in candidates
            if (row.payload or {}).get("queue_kind") not in {EVIDENCE_QUEUE_KIND}
            and not (row.payload or {}).get("depends_on")
        ][: max(overflow, 1)]
        for row in rows:
            await session.delete(row)
            QUEUE_DROPPED.inc()
        if rows:
            logger.warning(
                "Remember queue overflow; dropped oldest persisted items",
                extra={"extra_data": {"dropped": len(rows)}},
            )

    async def _active_depth(self, session: Any) -> int:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(RememberQueueRow)
            .where(RememberQueueRow.status.in_(["pending", "leased", "dispatching"]))
        )
        return int(count or 0)

    async def _drain_loop(self) -> None:
        semaphore = asyncio.Semaphore(self.max_concurrent)
        while True:
            generation = self._wake_generation
            ready = (
                await self._claim_due_durable_items(self.max_concurrent)
                if self._session_factory is not None
                else await self._collect_ready_in_memory()
            )
            if not ready:
                if self._stop_event.is_set():
                    break
                if self._wake_generation != generation:
                    continue
                self._wake_event.clear()
                if self._wake_generation != generation:
                    continue
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=self.recovery_interval_seconds,
                    )
                continue
            await asyncio.gather(*(self._process(item, semaphore) for item in ready))

    async def _reconciliation_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._run_scheduled_reconciliation_cycle()
            if self._stop_event.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.recovery_interval_seconds,
                )

    async def _ledger_repair_loop(self) -> None:
        while not self._stop_event.is_set():
            generation = self._ledger_wake_generation
            cycle_started = monotonic()
            await self._run_scheduled_ledger_repair_cycle()
            if self._stop_event.is_set():
                break
            minimum_delay = self._ledger_repair_min_interval_seconds - (monotonic() - cycle_started)
            if minimum_delay > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=minimum_delay,
                    )
                if self._stop_event.is_set():
                    break
            if self._ledger_wake_generation != generation:
                continue
            self._ledger_wake_event.clear()
            if self._ledger_wake_generation != generation:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._ledger_wake_event.wait(),
                    timeout=self.recovery_interval_seconds,
                )

    async def _run_scheduled_reconciliation_cycle(self) -> None:
        try:
            async with asyncio.timeout(self._reconciliation_cycle_timeout_seconds):
                async with self._reconciliation_lock:
                    await self._run_scheduled_reconciliation_stage(
                        self._reconcile_rotated_assistant_batch(
                            self._reconciliation_state,
                            self._rotation_repair_batch_size,
                        ),
                        stage="assistant_rotation",
                    )
                    await self._run_scheduled_reconciliation_stage(
                        self._reconcile_intaris_evidence_batch(
                            self._reconciliation_state,
                            session_limit=self._reconciliation_session_batch_size,
                            page_limit=self._reconciliation_page_batch_size,
                        ),
                        stage="intaris",
                    )
        except TimeoutError:
            self._reconciliation_state.pass_ok = False
            self._reconciliation_pass_ok = False
            logger.warning(
                "Trusted evidence reconciliation cycle exceeded its wall-clock budget",
                extra={"extra_data": {"queue": "trusted_evidence"}},
            )
        except asyncio.CancelledError:
            raise

    async def _run_scheduled_ledger_repair_cycle(self) -> None:
        try:
            async with asyncio.timeout(self._reconciliation_cycle_timeout_seconds):
                async with self._ledger_repair_lock:
                    await self._run_scheduled_reconciliation_stage(
                        self._reconcile_terminal_evidence_ledger_batch(
                            self._reconciliation_state,
                            self._reconciliation_ledger_batch_size,
                        ),
                        stage="terminal_ledger",
                    )
        except TimeoutError:
            self._reconciliation_state.pass_ok = False
            self._reconciliation_pass_ok = False
            logger.warning(
                "Trusted evidence ledger repair cycle exceeded its wall-clock budget",
                extra={"extra_data": {"queue": "trusted_evidence"}},
            )
        except asyncio.CancelledError:
            raise

    async def _run_scheduled_reconciliation_stage(
        self,
        operation: Awaitable[Any],
        *,
        stage: str,
    ) -> None:
        try:
            async with asyncio.timeout(self._reconciliation_stage_timeout_seconds):
                await operation
        except TimeoutError:
            self._reconciliation_state.pass_ok = False
            self._reconciliation_pass_ok = False
            logger.warning(
                "Trusted evidence reconciliation stage exceeded its wall-clock budget",
                extra={"extra_data": {"queue": "trusted_evidence", "stage": stage}},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._reconciliation_state.pass_ok = False
            self._reconciliation_pass_ok = False
            logger.warning(
                "Trusted evidence reconciliation stage failed",
                extra={"extra_data": {"queue": "trusted_evidence", "stage": stage}},
                exc_info=True,
            )

    async def reconcile_trusted_evidence(self) -> int:
        """Run one complete evidence repair sweep on demand."""
        if self._session_factory is None:
            return 0
        state = _EvidenceReconciliationState()
        rebuilt = 0
        terminal_complete = False
        intaris_complete = False
        async with self._ledger_repair_lock, self._reconciliation_lock:
            while not terminal_complete or not intaris_complete or bool(state.session_progress):
                if not terminal_complete:
                    previous_terminal_cursor = state.terminal_cursor
                    count, terminal_complete = await self._reconcile_terminal_evidence_ledger_batch(
                        state,
                        self._reconciliation_ledger_batch_size,
                    )
                    rebuilt += count
                    if (
                        not terminal_complete
                        and count == 0
                        and state.terminal_cursor == previous_terminal_cursor
                    ):
                        terminal_complete = True
                if not intaris_complete or state.session_progress:
                    count, intaris_complete = await self._reconcile_intaris_evidence_batch(
                        state,
                        session_limit=self._reconciliation_session_batch_size,
                        page_limit=self._reconciliation_page_batch_size,
                    )
                    rebuilt += count
        return rebuilt

    async def _reconcile_intaris_evidence_batch(
        self,
        state: _EvidenceReconciliationState,
        *,
        session_limit: int,
        page_limit: int,
    ) -> tuple[int, bool]:
        if self._session_factory is None:
            return 0, True
        rebuilt = 0
        sessions_touched = 0
        pages_read = 0
        reconcile_now = await self._database_clock()
        while sessions_touched < session_limit and pages_read < page_limit:
            cognis_session = await self._load_next_reconciliation_session(state.session_cursor)
            if cognis_session is None:
                await self._prune_reconciliation_progress(state)
                self._reconciliation_pass_ok = state.pass_ok and not state.session_progress
                state.session_cursor = None
                state.pass_ok = True
                return rebuilt, True
            sessions_touched += 1
            progress = state.session_progress.setdefault(
                cognis_session.session_id,
                _SessionReconciliationProgress(),
            )

            if not cognis_session.intaris_session_id or self._event_reader is None:
                await self._finish_reconciliation_session(progress)
                state.session_progress.pop(cognis_session.session_id, None)
                state.session_cursor = cognis_session.session_id
                continue
            if cognis_session.owner_email is None:
                state.pass_ok = False
                state.session_progress.pop(cognis_session.session_id, None)
                state.session_cursor = cognis_session.session_id
                continue

            try:
                with scoped_runtime_context(
                    user_email=cognis_session.user_email,
                    agent_id=cognis_session.agent_id,
                    agent_owner_email=cognis_session.owner_email,
                ):
                    async with asyncio.timeout(self._reconciliation_read_timeout_seconds):
                        read = await self._event_reader.read_events(
                            session_id=cognis_session.intaris_session_id,
                            after_seq=progress.after_seq,
                            limit=100,
                            types=["user_message", "assistant_message"],
                            allow_missing_stream=False,
                        )
            except TimeoutError:
                state.pass_ok = False
                logger.warning(
                    "Trusted evidence session reconciliation read timed out",
                    extra={"extra_data": {"queue": "trusted_evidence"}},
                )
                state.session_cursor = cognis_session.session_id
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                state.pass_ok = False
                logger.warning(
                    "Trusted evidence session reconciliation failed",
                    extra={"extra_data": {"queue": "trusted_evidence"}},
                    exc_info=True,
                )
                state.session_cursor = cognis_session.session_id
                continue

            pages_read += 1
            events = list(read.events)
            if not events:
                await self._finish_reconciliation_session(progress)
                state.session_progress.pop(cognis_session.session_id, None)
                state.session_cursor = cognis_session.session_id
                continue
            page_after_seq = progress.after_seq
            rebuilt += await self._reconcile_intaris_event_page(
                progress,
                cognis_session,
                events,
                reconcile_now=reconcile_now,
            )
            seqs = [
                seq
                for event in events
                if isinstance((seq := self._normalize_replay_event(event)[1]), int)
            ]
            if len(events) < 100 or not seqs:
                await self._finish_reconciliation_session(progress)
                state.session_progress.pop(cognis_session.session_id, None)
                state.session_cursor = cognis_session.session_id
                continue
            next_after_seq = max(seqs)
            if next_after_seq <= page_after_seq:
                state.pass_ok = False
                logger.warning(
                    "Trusted evidence session reconciliation made no page progress",
                    extra={"extra_data": {"queue": "trusted_evidence"}},
                )
                await self._finish_reconciliation_session(progress)
                state.session_progress.pop(cognis_session.session_id, None)
                state.session_cursor = cognis_session.session_id
                continue
            progress.after_seq = max(progress.after_seq, next_after_seq)
            state.session_cursor = cognis_session.session_id
        return rebuilt, False

    async def _load_next_reconciliation_session(
        self,
        after_session_id: str | None,
    ) -> _ReconciliationSession | None:
        assert self._session_factory is not None
        async with self._session_factory() as session:
            query = sa.select(Session).order_by(Session.session_id.asc()).limit(1)
            if after_session_id is not None:
                query = query.where(Session.session_id > after_session_id)
            row = (await session.execute(query)).scalar_one_or_none()
            if row is None:
                return None
            agent = await session.get(Agent, row.agent_id)
            return self._reconciliation_session_snapshot(row, agent)

    async def _reconcile_rotated_assistant_batch(
        self,
        state: _EvidenceReconciliationState,
        limit: int,
    ) -> tuple[int, bool]:
        if self._session_factory is None or self._event_reader is None:
            return 0, True
        async with self._session_factory() as session:
            query = (
                sa.select(RememberQueueRow.item_id)
                .where(
                    RememberQueueRow.status == "failed",
                    RememberQueueRow.attempts == 1,
                    RememberQueueRow.last_error == "ordinary remember assertion conflict",
                    RememberQueueRow.lease_token.is_(None),
                    RememberQueueRow.lease_expires_at.is_(None),
                    RememberQueueRow.payload["queue_kind"].as_string()
                    == ORDINARY_ASSISTANT_QUEUE_KIND,
                    RememberQueueRow.payload["event_hash"].as_string().is_(None),
                )
                .order_by(RememberQueueRow.item_id.asc())
                .limit(limit)
            )
            if state.rotation_repair_cursor is not None:
                query = query.where(RememberQueueRow.item_id > state.rotation_repair_cursor)
            item_ids = list((await session.execute(query)).scalars().all())
        if not item_ids:
            state.rotation_repair_cursor = None
            return 0, True

        repaired = 0
        for item_id in item_ids:
            candidate = await self._load_rotated_assistant_candidate(item_id)
            if candidate is None:
                logger.info(
                    "Assistant rotation repair candidate was not proved",
                    extra={
                        "extra_data": {
                            "repair_class": "predecessor_rotation_unproved",
                            "item_id_hash": _value_fingerprint(item_id),
                        }
                    },
                )
                state.rotation_repair_cursor = item_id
                continue
            assistant_payload, evidence_payload, owner_email = candidate
            try:
                with scoped_runtime_context(
                    user_email=str(assistant_payload["user_email"]),
                    agent_id=str(assistant_payload["agent_id"]),
                    agent_owner_email=owner_email,
                ):
                    async with asyncio.timeout(self._reconciliation_read_timeout_seconds):
                        evidence_read, assistant_read = await asyncio.gather(
                            self._read_replay_events(
                                intaris_session_id=str(evidence_payload["intaris_session_id"]),
                                requested_seqs=[int(evidence_payload["event_seq"])],
                                after_seq=max(int(evidence_payload["event_seq"]) - 1, 0),
                            ),
                            self._read_replay_events(
                                intaris_session_id=str(assistant_payload["intaris_session_id"]),
                                requested_seqs=[int(assistant_payload["assistant_event_seq"])],
                                after_seq=max(
                                    int(assistant_payload["assistant_event_seq"]) - 1,
                                    0,
                                ),
                            ),
                        )
            except TimeoutError:
                state.pass_ok = False
                logger.warning(
                    "Assistant rotation repair source read timed out",
                    extra={
                        "extra_data": {
                            "repair_class": "predecessor_rotation",
                            "item_id_hash": _value_fingerprint(item_id),
                        }
                    },
                )
                return repaired, False
            except asyncio.CancelledError:
                raise
            except Exception:
                state.pass_ok = False
                logger.warning(
                    "Assistant rotation repair source read failed",
                    extra={
                        "extra_data": {
                            "repair_class": "predecessor_rotation",
                            "item_id_hash": _value_fingerprint(item_id),
                        }
                    },
                    exc_info=True,
                )
                return repaired, False

            evidence_events = list(evidence_read.events)
            assistant_events = list(assistant_read.events)
            if not (
                len(evidence_events) == 1
                and len(assistant_events) == 1
                and self._rotation_source_event_matches(
                    evidence_events[0],
                    session_id=str(evidence_payload["intaris_session_id"]),
                    seq=int(evidence_payload["event_seq"]),
                    turn_id=str(evidence_payload["turn_id"]),
                    event_type="user_message",
                    expected_hash=str(evidence_payload["event_hash"]),
                )
                and self._rotation_source_event_matches(
                    assistant_events[0],
                    session_id=str(assistant_payload["intaris_session_id"]),
                    seq=int(assistant_payload["assistant_event_seq"]),
                    turn_id=str(assistant_payload["turn_id"]),
                    event_type="assistant_message",
                    expected_hash=str(assistant_payload["assistant_event_hash"]),
                )
            ):
                state.pass_ok = False
                logger.warning(
                    "Assistant rotation repair source identity did not match",
                    extra={
                        "extra_data": {
                            "repair_class": "predecessor_rotation_source_mismatch",
                            "item_id_hash": _value_fingerprint(item_id),
                        }
                    },
                )
                state.rotation_repair_cursor = item_id
                continue
            if await self._commit_rotated_assistant_repair(
                item_id,
                assistant_payload=assistant_payload,
                evidence_payload=evidence_payload,
            ):
                repaired += 1
            state.rotation_repair_cursor = item_id
        return repaired, len(item_ids) < limit

    async def _load_rotated_assistant_candidate(
        self,
        item_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str] | None:
        assert self._session_factory is not None
        async with self._session_factory() as session:
            assistant = await session.get(RememberQueueRow, item_id)
            if assistant is None:
                return None
            assistant_payload = dict(assistant.payload or {})
            dependency_id = assistant_payload.get("depends_on")
            if not isinstance(dependency_id, str):
                return None
            evidence = await session.get(RememberQueueRow, dependency_id)
            successor = await session.get(
                Session,
                str(assistant_payload.get("cognis_session_id") or ""),
            )
            evidence_payload = dict(evidence.payload or {}) if evidence is not None else {}
            predecessor = await session.get(
                Session,
                str(evidence_payload.get("cognis_session_id") or ""),
            )
            agent = await session.get(Agent, str(assistant_payload.get("agent_id") or ""))
            if not self._rotated_assistant_identity_matches(
                assistant,
                assistant_payload,
                evidence,
                evidence_payload,
                successor,
                predecessor,
                agent,
            ):
                return None
            return assistant_payload, evidence_payload, str(agent.owner_email)

    @staticmethod
    def _rotation_source_event_matches(
        raw_event: Any,
        *,
        session_id: str,
        seq: int,
        turn_id: str,
        event_type: str,
        expected_hash: str,
    ) -> bool:
        normalized_type, normalized_seq, event_data = RememberRetryQueue._normalize_replay_event(
            raw_event
        )
        if normalized_seq != seq or normalized_type != event_type:
            return False
        return (
            event_data.get("turn_id") == turn_id
            and event_hash(session_id, seq, raw_event) == expected_hash
        )

    @staticmethod
    def _rotated_assistant_identity_matches(
        assistant: RememberQueueRow,
        assistant_payload: dict[str, Any],
        evidence: RememberQueueRow | None,
        evidence_payload: dict[str, Any],
        successor: Session | None,
        predecessor: Session | None,
        agent: Agent | None,
    ) -> bool:
        evidence_hash_value = evidence_payload.get("event_hash")
        assistant_hash = assistant_payload.get("assistant_event_hash")
        dependency_id = assistant_payload.get("depends_on")
        immutable_fields = (
            "conversation_id",
            "turn_id",
            "user_email",
            "owner_email",
            "agent_owner_email",
            "agent_id",
            "policy_agent_id",
        )
        return bool(
            evidence is not None
            and successor is not None
            and predecessor is not None
            and agent is not None
            and assistant.status == "failed"
            and assistant.attempts == 1
            and assistant.last_error == "ordinary remember assertion conflict"
            and assistant.lease_token is None
            and assistant.lease_expires_at is None
            and assistant_payload.get("queue_kind") == ORDINARY_ASSISTANT_QUEUE_KIND
            and "event_hash" not in assistant_payload
            and isinstance(evidence_hash_value, str)
            and bool(evidence_hash_value)
            and isinstance(assistant_hash, str)
            and bool(assistant_hash)
            and isinstance(dependency_id, str)
            and dependency_id == evidence.item_id
            and dependency_id == deterministic_queue_id(EVIDENCE_QUEUE_KIND, evidence_hash_value)
            and assistant.item_id
            == deterministic_queue_id(
                ORDINARY_ASSISTANT_QUEUE_KIND,
                evidence_hash_value,
                assistant_hash,
            )
            and evidence.status in TERMINAL_EVIDENCE_OUTCOMES
            and evidence_payload.get("queue_kind") == EVIDENCE_QUEUE_KIND
            and isinstance(evidence_payload.get("event_seq"), int)
            and evidence_payload["event_seq"] > 0
            and isinstance(assistant_payload.get("assistant_event_seq"), int)
            and assistant_payload["assistant_event_seq"] > 0
            and assistant_payload.get("include_user_message") is False
            and assistant_payload.get("user_event_seq") is None
            and all(
                isinstance(assistant_payload.get(field), str) and bool(assistant_payload[field])
                for field in immutable_fields
            )
            and successor.previous_session_id == predecessor.session_id
            and successor.session_id == assistant_payload.get("cognis_session_id")
            and successor.intaris_session_id == assistant_payload.get("intaris_session_id")
            and successor.mnemory_session_id == assistant_payload.get("session_id")
            and predecessor.session_id == evidence_payload.get("cognis_session_id")
            and predecessor.intaris_session_id == evidence_payload.get("intaris_session_id")
            and predecessor.mnemory_session_id == evidence_payload.get("session_id")
            and successor.conversation_id == predecessor.conversation_id
            and successor.conversation_id == assistant_payload.get("conversation_id")
            and successor.user_email == predecessor.user_email
            and successor.user_email == assistant_payload.get("user_email")
            and successor.agent_id == predecessor.agent_id
            and successor.agent_id == assistant_payload.get("agent_id")
            and agent.agent_id == successor.agent_id
            and agent.owner_email == assistant_payload.get("owner_email")
            and agent.owner_email == assistant_payload.get("agent_owner_email")
            and all(
                assistant_payload.get(field) == evidence_payload.get(field)
                for field in immutable_fields
            )
            and assistant.session_id == assistant_payload.get("session_id")
            and assistant.user_email == assistant_payload.get("user_email")
            and assistant.agent_id == assistant_payload.get("agent_id")
            and evidence.session_id == evidence_payload.get("session_id")
            and evidence.user_email == evidence_payload.get("user_email")
            and evidence.agent_id == evidence_payload.get("agent_id")
        )

    async def _commit_rotated_assistant_repair(
        self,
        item_id: str,
        *,
        assistant_payload: dict[str, Any],
        evidence_payload: dict[str, Any],
    ) -> bool:
        assert self._session_factory is not None
        async with self._session_factory() as session:
            assistant = (
                await session.execute(
                    sa.select(RememberQueueRow)
                    .where(RememberQueueRow.item_id == item_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if assistant is None:
                return False
            current_payload = dict(assistant.payload or {})
            dependency = await session.get(
                RememberQueueRow,
                str(current_payload.get("depends_on") or ""),
                with_for_update=True,
            )
            successor = await session.get(
                Session,
                str(current_payload.get("cognis_session_id") or ""),
            )
            current_evidence_payload = (
                dict(dependency.payload or {}) if dependency is not None else {}
            )
            predecessor = await session.get(
                Session,
                str(current_evidence_payload.get("cognis_session_id") or ""),
            )
            agent = await session.get(Agent, str(current_payload.get("agent_id") or ""))
            if not (
                current_payload == assistant_payload
                and current_evidence_payload == evidence_payload
                and self._rotated_assistant_identity_matches(
                    assistant,
                    current_payload,
                    dependency,
                    current_evidence_payload,
                    successor,
                    predecessor,
                    agent,
                )
            ):
                return False
            now = await database_now(session)
            assistant.payload = {
                **current_payload,
                "event_hash": current_evidence_payload["event_hash"],
            }
            assistant.status = "pending"
            assistant.next_retry_at = now
            assistant.last_error = None
            assistant.updated_at = now
            await session.commit()
            await self._update_durable_depth_metric(session)
        self._signal_wake()
        logger.info(
            "Repaired failed predecessor-rotation assistant queue identity",
            extra={
                "extra_data": {
                    "repair_class": "predecessor_rotation",
                    "item_id_hash": _value_fingerprint(item_id),
                }
            },
        )
        return True

    async def _prune_reconciliation_progress(
        self,
        state: _EvidenceReconciliationState,
    ) -> None:
        if not state.session_progress:
            return
        assert self._session_factory is not None
        async with self._session_factory() as session:
            existing = set(
                (
                    await session.execute(
                        sa.select(Session.session_id).where(
                            Session.session_id.in_(state.session_progress)
                        )
                    )
                )
                .scalars()
                .all()
            )
        for session_id in set(state.session_progress) - existing:
            state.session_progress.pop(session_id, None)

    @staticmethod
    def _reconciliation_session_snapshot(
        session: Session,
        agent: Agent | None,
    ) -> _ReconciliationSession:
        return _ReconciliationSession(
            session_id=session.session_id,
            intaris_session_id=str(session.intaris_session_id or ""),
            mnemory_session_id=session.mnemory_session_id,
            conversation_id=session.conversation_id,
            user_email=session.user_email,
            agent_id=session.agent_id,
            agent_profile_id=session.agent_profile_id,
            owner_email=agent.owner_email if agent is not None else None,
        )

    async def _reconcile_intaris_event_page(
        self,
        progress: _SessionReconciliationProgress,
        cognis_session: _ReconciliationSession,
        events: list[Any],
        *,
        reconcile_now: datetime,
    ) -> int:
        rebuilt = 0
        for raw_event in events:
            rebuilt += await self._reconcile_intaris_event(
                progress,
                cognis_session,
                raw_event,
                reconcile_now=reconcile_now,
            )
            seq = self._normalize_replay_event(raw_event)[1]
            if isinstance(seq, int) and seq > progress.after_seq:
                progress.after_seq = seq
        return rebuilt

    async def _reconcile_intaris_event(
        self,
        progress: _SessionReconciliationProgress,
        cognis_session: _ReconciliationSession,
        raw_event: Any,
        *,
        reconcile_now: datetime,
    ) -> int:
        event_type, seq, event_data = self._normalize_replay_event(raw_event)
        if event_type == "assistant_message":
            turn_id = str(event_data.get("turn_id") or "")
            pending = progress.pending_markers.get(turn_id)
            if pending is not None and isinstance(seq, int) and seq > pending[2]:
                first_assistant = pending[2] <= 0
                progress.pending_markers[turn_id] = (
                    pending[0],
                    pending[1],
                    seq,
                    event_hash(cognis_session.intaris_session_id, seq, raw_event),
                )
                if first_assistant:
                    progress.assistant_ready_turns.append(turn_id)
            return 0
        if event_type != "user_message":
            return 0
        await self._flush_completed_reconciliation_handoffs(
            progress,
            before_turn_id=str(event_data.get("turn_id") or ""),
        )
        marker = marker_for_event(raw_event)
        if (
            marker is None
            or not marker_is_valid(
                marker,
                admission_key=self._trusted_evidence_admission_key,
            )
            or not marker_matches_event(
                marker,
                admission_key=self._trusted_evidence_admission_key,
                intaris_session_id=cognis_session.intaris_session_id,
                event_data=event_data,
            )
        ):
            return 0
        admission = deserialize_evidence_admission(marker.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
        marker_owner = str(marker.get("owner_id") or "")
        if not evidence_admission_authorizes(
            admission,
            key=self._trusted_evidence_admission_key,
            owner_id=marker_owner,
        ):
            return 0
        assert admission is not None
        marker_time = _parse_marker_time(marker)
        if marker_time is None or not isinstance(seq, int) or seq <= 0:
            return 0
        authority_matches = bool(
            marker_owner == cognis_session.owner_email
            and marker.get("user_id") == cognis_session.user_email
            and marker.get("cognis_session_id") == cognis_session.session_id
            and marker.get("conversation_id") == cognis_session.conversation_id
        )
        marker_expired = (
            observe_queue_age(marker_time, now=reconcile_now) > admission.max_age_seconds
        )
        turn_id = str(marker["turn_id"])
        evidence_event_hash = event_hash(
            cognis_session.intaris_session_id,
            seq,
            raw_event,
        )
        evidence_id = deterministic_queue_id(
            EVIDENCE_QUEUE_KIND,
            evidence_event_hash,
        )
        common = {
            "session_id": cognis_session.mnemory_session_id,
            "cognis_session_id": cognis_session.session_id,
            "intaris_session_id": cognis_session.intaris_session_id,
            "conversation_id": cognis_session.conversation_id,
            "turn_id": turn_id,
            "user_email": str(marker["user_id"]),
            "owner_email": marker_owner,
            "agent_owner_email": marker_owner,
            "agent_id": cognis_session.agent_id,
            "policy_agent_id": cognis_session.agent_id,
            "originating_memory_backend": None,
            "originating_agent_profile_id": cognis_session.agent_profile_id,
            "memory_policy_fingerprint": None,
            "queue_kind": EVIDENCE_QUEUE_KIND,
            "item_id": evidence_id,
            "event_seq": seq,
            "event_hash": evidence_event_hash,
            "marker_admitted_at": marker["admitted_at"],
            "admission_event_id": admission.event_binding["event_id"],
            TRUSTED_EVIDENCE_ADMISSION_KEY: serialize_evidence_admission(admission),
        }
        unavailable = marker_expired or not authority_matches
        await self._enqueue_durable_group(
            [
                {**common, "status_override": "unavailable"} if unavailable else common,
                {
                    **common,
                    "queue_kind": ORDINARY_USER_QUEUE_KIND,
                    "item_id": deterministic_queue_id(
                        ORDINARY_USER_QUEUE_KIND,
                        evidence_event_hash,
                    ),
                    "depends_on": None if unavailable else evidence_id,
                },
            ]
        )
        if turn_id in progress.pending_markers:
            return 0
        assert self._session_factory is not None
        async with self._session_factory() as canonical_session:
            canonical = (
                await canonical_session.execute(
                    sa.select(RememberQueueRow)
                    .where(
                        RememberQueueRow.payload["queue_kind"].as_string() == EVIDENCE_QUEUE_KIND,
                        RememberQueueRow.payload["admission_event_id"].as_string()
                        == admission.event_binding["event_id"],
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if canonical is not None:
            progress.pending_markers[turn_id] = (
                dict(canonical.payload or {}),
                canonical.item_id,
                0,
                "",
            )
        return 1

    async def _finish_reconciliation_session(
        self,
        progress: _SessionReconciliationProgress,
    ) -> None:
        queued_turns = set(progress.assistant_ready_turns)
        progress.assistant_ready_turns.extend(
            turn_id
            for turn_id, pending in progress.pending_markers.items()
            if turn_id not in queued_turns and pending[2] > 0 and pending[3]
        )
        await self._flush_completed_reconciliation_handoffs(progress)
        progress.pending_markers.clear()

    async def _flush_completed_reconciliation_handoffs(
        self,
        progress: _SessionReconciliationProgress,
        *,
        before_turn_id: str | None = None,
    ) -> None:
        pending_count = len(progress.assistant_ready_turns)
        for _ in range(pending_count):
            turn_id = progress.assistant_ready_turns[0]
            if turn_id == before_turn_id:
                progress.assistant_ready_turns.rotate(-1)
                continue
            pending = progress.pending_markers.get(turn_id)
            if pending is None:
                progress.assistant_ready_turns.popleft()
                continue
            common, evidence_id, assistant_seq, assistant_hash = pending
            await self.enqueue(
                {
                    **{
                        k: v
                        for k, v in common.items()
                        if k not in {"trusted_rejection", "rejected_source"}
                    },
                    "queue_kind": ORDINARY_ASSISTANT_QUEUE_KIND,
                    "item_id": deterministic_queue_id(
                        ORDINARY_ASSISTANT_QUEUE_KIND,
                        common["event_hash"],
                        assistant_hash,
                    ),
                    "depends_on": evidence_id,
                    "include_user_message": False,
                    "user_event_seq": None,
                    "assistant_event_seq": assistant_seq,
                    "assistant_event_hash": assistant_hash,
                }
            )
            progress.assistant_ready_turns.popleft()
            progress.pending_markers.pop(turn_id, None)

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
        claimed: list[RememberQueueItem] = []
        async with self._session_factory() as session:
            now = await database_now(session)
            dependency = RememberQueueRow.__table__.alias("remember_queue_dependency")
            queue_kind = RememberQueueRow.payload["queue_kind"].as_string()
            dependency_id = RememberQueueRow.payload["depends_on"].as_string()
            dependency_is_terminal = sa.exists(
                sa.select(1)
                .select_from(dependency)
                .where(
                    dependency.c.item_id == dependency_id,
                    dependency.c.status.in_(TERMINAL_EVIDENCE_OUTCOMES),
                )
            )
            ordinary_is_ready = sa.and_(
                sa.or_(queue_kind.is_(None), queue_kind != EVIDENCE_QUEUE_KIND),
                sa.or_(dependency_id.is_(None), dependency_is_terminal),
            )
            evidence_is_ready = queue_kind == EVIDENCE_QUEUE_KIND
            claim_priority = (
                sa.case(
                    (evidence_is_ready, 0),
                    (ordinary_is_ready, 1),
                    else_=2,
                )
                if self._prefer_evidence_claim
                else sa.case(
                    (ordinary_is_ready, 0),
                    (evidence_is_ready, 1),
                    else_=2,
                )
            )
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
                                    RememberQueueRow.status.in_(["leased", "dispatching"]),
                                    RememberQueueRow.lease_expires_at.is_not(None),
                                    RememberQueueRow.lease_expires_at <= now,
                                ),
                            )
                        )
                        .order_by(
                            claim_priority,
                            RememberQueueRow.next_retry_at.asc(),
                            RememberQueueRow.created_at.asc(),
                        )
                        .limit(limit * 4)
                    )
                )
                .scalars()
                .all()
            )

            for row in rows:
                if row.status == "dispatching":
                    evidence_item = (row.payload or {}).get("queue_kind") == EVIDENCE_QUEUE_KIND
                    await session.execute(
                        sa.update(RememberQueueRow)
                        .execution_options(synchronize_session=False)
                        .where(
                            RememberQueueRow.item_id == row.item_id,
                            RememberQueueRow.status == "dispatching",
                            RememberQueueRow.lease_expires_at.is_not(None),
                            RememberQueueRow.lease_expires_at <= now,
                        )
                        .values(
                            status="pending" if evidence_item else "ambiguous",
                            next_retry_at=now if evidence_item else row.next_retry_at,
                            lease_token=None,
                            lease_expires_at=None,
                            last_error=(
                                "Trusted evidence dispatch lease expired; retrying"
                                if evidence_item
                                else "Remember worker ownership expired while provider outcome "
                                "was unknown; automatic retry is disabled"
                            ),
                            updated_at=now,
                        )
                    )
                    continue
                if row.status == "leased":
                    await session.execute(
                        sa.update(RememberQueueRow)
                        .execution_options(synchronize_session=False)
                        .where(
                            RememberQueueRow.item_id == row.item_id,
                            RememberQueueRow.status == "leased",
                            RememberQueueRow.lease_expires_at.is_not(None),
                            RememberQueueRow.lease_expires_at <= now,
                        )
                        .values(
                            status="pending",
                            lease_token=None,
                            lease_expires_at=None,
                            next_retry_at=now,
                            updated_at=now,
                        )
                    )
                    continue
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
                                RememberQueueRow.status.in_(["leased", "dispatching"]),
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
        if claimed:
            self._prefer_evidence_claim = not self._prefer_evidence_claim
        return claimed

    async def _process(self, item: RememberQueueItem, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            lease_lost = asyncio.Event()
            renewal_task: asyncio.Task[None] | None = None
            if self._session_factory is not None and item.item_id is not None:
                renewal_task = asyncio.create_task(self._renew_durable_lease(item, lease_lost))
            try:
                if item.payload.get("queue_kind") == EVIDENCE_QUEUE_KIND:
                    await self._process_evidence_item(item)
                    return
                if await self._hard_memory_disable_applies(item.payload):
                    QUEUE_SUCCESS.inc()
                    if self._session_factory is not None and item.item_id is not None:
                        async with self._session_factory() as session:
                            now = await database_now(session)
                            if item.payload.get("queue_kind") in {
                                ORDINARY_USER_QUEUE_KIND,
                                ORDINARY_ASSISTANT_QUEUE_KIND,
                            }:
                                await session.execute(
                                    sa.update(RememberQueueRow)
                                    .where(
                                        RememberQueueRow.item_id == item.item_id,
                                        RememberQueueRow.lease_token == item.lease_token,
                                        RememberQueueRow.status == "leased",
                                    )
                                    .values(
                                        status="completed",
                                        lease_token=None,
                                        lease_expires_at=None,
                                        updated_at=now,
                                    )
                                )
                            else:
                                await session.execute(
                                    sa.delete(RememberQueueRow)
                                    .where(
                                        RememberQueueRow.item_id == item.item_id,
                                        RememberQueueRow.lease_token == item.lease_token,
                                        RememberQueueRow.status == "leased",
                                        RememberQueueRow.lease_expires_at > now,
                                    )
                                    .execution_options(synchronize_session=False)
                                )
                            await session.commit()
                            await self._update_durable_depth_metric(session)
                    return
                if item.payload.get("queue_kind") in {
                    ORDINARY_USER_QUEUE_KIND,
                    ORDINARY_ASSISTANT_QUEUE_KIND,
                }:
                    await self._validate_ordinary_item(item)
                dependency_id = item.payload.get("depends_on")
                if isinstance(dependency_id, str) and not await self._dependency_is_terminal(
                    dependency_id
                ):
                    await self._release_pending(item)
                    return
                user_event_body: UserEventRememberRequest | None = None
                resolved_payload: dict[str, Any] | None = None
                if item.payload.get("queue_kind") == ORDINARY_USER_QUEUE_KIND:
                    try:
                        evidence_body = await self._resolve_evidence_body(item.payload)
                        user_event_body = UserEventRememberRequest.model_validate(
                            evidence_body.model_dump(mode="json")
                        )
                    except ValueError as exc:
                        raise RuntimeError(
                            "Trusted user-event handoff assertion is invalid"
                        ) from exc
                else:
                    resolved_payload = await self._resolve_payload(item.payload)
                if self._session_factory is not None and item.item_id is not None:
                    async with self._session_factory() as session:
                        now = await database_now(session)
                        dispatched = await session.execute(
                            sa.update(RememberQueueRow)
                            .execution_options(synchronize_session=False)
                            .where(
                                RememberQueueRow.item_id == item.item_id,
                                RememberQueueRow.lease_token == item.lease_token,
                                RememberQueueRow.status == "leased",
                                RememberQueueRow.lease_expires_at > now,
                            )
                            .values(status="dispatching", updated_at=now)
                        )
                        await session.commit()
                    if not dispatched.rowcount:
                        return
                if user_event_body is not None:
                    signing_inputs = await self._revalidate_evidence_signing_inputs(
                        item,
                        user_event_body,
                        token_kind="user_event",
                    )
                    await self.worker.remember_user_event(
                        signing_inputs.body,
                        signing_inputs.token,
                    )
                else:
                    assert resolved_payload is not None
                    await self.worker.remember(**resolved_payload)
                QUEUE_SUCCESS.inc()
                if self._session_factory is None or item.item_id is None:
                    return
                async with self._session_factory() as session:
                    now = await database_now(session)
                    if item.payload.get("queue_kind") in {
                        ORDINARY_USER_QUEUE_KIND,
                        ORDINARY_ASSISTANT_QUEUE_KIND,
                    }:
                        await session.execute(
                            sa.update(RememberQueueRow)
                            .where(
                                RememberQueueRow.item_id == item.item_id,
                                RememberQueueRow.lease_token == item.lease_token,
                                RememberQueueRow.status == "dispatching",
                            )
                            .values(
                                status="completed",
                                lease_token=None,
                                lease_expires_at=None,
                                updated_at=now,
                            )
                        )
                    else:
                        await session.execute(
                            sa.delete(RememberQueueRow)
                            .where(
                                RememberQueueRow.item_id == item.item_id,
                                RememberQueueRow.lease_token == item.lease_token,
                                RememberQueueRow.status == "dispatching",
                                RememberQueueRow.lease_expires_at > now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                    await session.commit()
                    await self._update_durable_depth_metric(session)
            except ValueError:
                if item.payload.get("queue_kind") in {
                    ORDINARY_USER_QUEUE_KIND,
                    ORDINARY_ASSISTANT_QUEUE_KIND,
                }:
                    return
                raise
            except RememberOutcomeUnknownError as exc:
                if self._session_factory is None or item.item_id is None:
                    QUEUE_FAILED.inc()
                    logger.exception("Remember queue outcome is ambiguous; retry disabled")
                    return
                item.attempts += 1
                last_error = self._sanitize_failure_detail(exc)
                async with self._session_factory() as session:
                    now = await database_now(session)
                    result = await session.execute(
                        sa.update(RememberQueueRow)
                        .execution_options(synchronize_session=False)
                        .where(
                            RememberQueueRow.item_id == item.item_id,
                            RememberQueueRow.lease_token == item.lease_token,
                            RememberQueueRow.status == "dispatching",
                            RememberQueueRow.lease_expires_at > now,
                        )
                        .values(
                            status="ambiguous",
                            attempts=item.attempts,
                            next_retry_at=now,
                            lease_token=None,
                            lease_expires_at=None,
                            last_error=last_error,
                            updated_at=now,
                        )
                    )
                    await session.commit()
                    await self._update_durable_depth_metric(session)
                if result.rowcount:
                    QUEUE_FAILED.inc()
                    logger.exception(
                        "Remember queue outcome is ambiguous; retry disabled",
                        extra={"extra_data": {"item_id": item.item_id, "attempts": item.attempts}},
                    )
                    await self._record_failure_notice(item, last_error)
            except TrustedEventRejectedError as exc:
                await self._fail_trusted_rejection(item, exc)
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
                    retry_delay = min(2**item.attempts, self.backoff_max)
                    item.next_retry_at = (
                        monotonic() if self._stop_event.is_set() else monotonic() + retry_delay
                    )
                    async with self._lock:
                        self._items.append(item)
                        QUEUE_DEPTH.set(len(self._items))
                    if not self._stop_event.is_set():
                        asyncio.get_running_loop().call_later(retry_delay, self._signal_wake)
                    return

                retry_delay = min(2**item.attempts, self.backoff_max)
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
                    now = await database_now(session)
                    next_retry_at = now + timedelta(seconds=retry_delay)
                    await session.execute(
                        sa.update(RememberQueueRow)
                        .execution_options(synchronize_session=False)
                        .where(
                            RememberQueueRow.item_id == item.item_id,
                            RememberQueueRow.lease_token == item.lease_token,
                            RememberQueueRow.status.in_(["leased", "dispatching"]),
                            RememberQueueRow.lease_expires_at > now,
                        )
                        .values(
                            status=status,
                            attempts=item.attempts,
                            next_retry_at=next_retry_at,
                            lease_token=None,
                            lease_expires_at=None,
                            last_error=last_error,
                            updated_at=now,
                        )
                    )
                    await session.commit()
                    await self._update_durable_depth_metric(session)
                if status == "pending":
                    asyncio.get_running_loop().call_later(retry_delay, self._signal_wake)
                if status == "failed":
                    await self._record_failure_notice(item, last_error)
            finally:
                lease_lost.set()
                if renewal_task is not None:
                    renewal_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await renewal_task

    async def _dependency_is_terminal(self, item_id: str) -> bool:
        if self._session_factory is None:
            return True
        async with self._session_factory() as session:
            row = await session.get(RememberQueueRow, item_id)
        return row is not None and row.status in TERMINAL_EVIDENCE_OUTCOMES

    async def _release_pending(self, item: RememberQueueItem) -> None:
        if self._session_factory is None or item.item_id is None:
            return
        async with self._session_factory() as session:
            now = await database_now(session)
            await session.execute(
                sa.update(RememberQueueRow)
                .where(
                    RememberQueueRow.item_id == item.item_id,
                    RememberQueueRow.lease_token == item.lease_token,
                    RememberQueueRow.status == "leased",
                )
                .values(
                    status="pending",
                    lease_token=None,
                    lease_expires_at=None,
                    next_retry_at=now + timedelta(seconds=1),
                    updated_at=now,
                )
            )
            await session.commit()
            await self._update_durable_depth_metric(session)
        asyncio.get_running_loop().call_later(1.0, self._signal_wake)

    def _validate_evidence_item(self, item: RememberQueueItem) -> bool:
        payload = item.payload
        event_hash_value = payload.get("event_hash")
        admission = deserialize_evidence_admission(payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
        return (
            isinstance(event_hash_value, str)
            and bool(event_hash_value)
            and item.item_id == deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash_value)
            and isinstance(payload.get("event_seq"), int)
            and payload["event_seq"] > 0
            and all(
                isinstance(payload.get(key), str) and payload[key]
                for key in (
                    "cognis_session_id",
                    "intaris_session_id",
                    "conversation_id",
                    "turn_id",
                    "user_email",
                    "owner_email",
                    "agent_owner_email",
                    "agent_id",
                    "policy_agent_id",
                )
            )
            and payload["agent_owner_email"] == payload["owner_email"]
            and payload["policy_agent_id"] == payload["agent_id"]
            and admission is not None
            and payload.get("admission_event_id") == admission.event_binding.get("event_id")
            and payload.get("marker_admitted_at") == admission.admitted_at
            and evidence_admission_authorizes(
                admission,
                key=self._trusted_evidence_admission_key,
                owner_id=payload.get("owner_email"),
            )
        )

    async def _validate_ordinary_item(self, item: RememberQueueItem) -> None:
        payload = item.payload
        event_hash_value = payload.get("event_hash")
        kind = payload.get("queue_kind")
        expected_dependency = (
            deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash_value)
            if isinstance(event_hash_value, str)
            else None
        )
        expected_item = (
            deterministic_queue_id(
                str(kind),
                event_hash_value,
                str(payload.get("assistant_event_hash"))
                if kind == ORDINARY_ASSISTANT_QUEUE_KIND
                else None,
            )
            if isinstance(kind, str) and isinstance(event_hash_value, str)
            else None
        )
        valid = (
            expected_item == item.item_id
            and payload.get("depends_on") in {expected_dependency, None}
            and (
                (
                    kind == ORDINARY_USER_QUEUE_KIND
                    and isinstance(payload.get("event_seq"), int)
                    and payload["event_seq"] > 0
                )
                or (
                    kind == ORDINARY_ASSISTANT_QUEUE_KIND
                    and isinstance(payload.get("assistant_event_seq"), int)
                    and payload["assistant_event_seq"] > 0
                    and isinstance(payload.get("assistant_event_hash"), str)
                    and bool(payload["assistant_event_hash"])
                )
            )
            and isinstance(payload.get("policy_agent_id"), str)
            and payload["policy_agent_id"] == payload.get("agent_id")
            and isinstance(payload.get("agent_owner_email"), str)
            and payload["agent_owner_email"] == payload.get("owner_email")
        )
        if valid and self._session_factory is not None:
            async with self._session_factory() as session:
                cognis_session = await session.get(
                    Session, str(payload.get("cognis_session_id") or "")
                )
                agent = await session.get(Agent, str(payload.get("policy_agent_id") or ""))
                dependency = await session.get(RememberQueueRow, expected_dependency)
            valid = bool(
                cognis_session is not None
                and agent is not None
                and (
                    payload.get("depends_on") == expected_dependency
                    or (
                        payload.get("depends_on") is None
                        and dependency is not None
                        and dependency.status in TERMINAL_EVIDENCE_OUTCOMES
                    )
                )
                and cognis_session.mnemory_session_id == payload.get("session_id")
                and cognis_session.conversation_id == payload.get("conversation_id")
                and cognis_session.user_email == payload.get("user_email")
                and cognis_session.agent_id == payload.get("policy_agent_id")
                and agent.owner_email == payload.get("owner_email")
                and agent.owner_email == payload.get("agent_owner_email")
                and (
                    payload.get("originating_agent_profile_id") is None
                    or cognis_session.agent_profile_id
                    == payload.get("originating_agent_profile_id")
                )
            )
        if not valid:
            await self._fail_tampered_ordinary(item)
            raise ValueError("ordinary remember queue assertion conflict")

    async def _fail_tampered_ordinary(self, item: RememberQueueItem) -> None:
        if self._session_factory is None or item.item_id is None:
            return
        async with self._session_factory() as session:
            now = await database_now(session)
            await session.execute(
                sa.update(RememberQueueRow)
                .where(
                    RememberQueueRow.item_id == item.item_id,
                    RememberQueueRow.lease_token == item.lease_token,
                    RememberQueueRow.status == "leased",
                )
                .values(
                    status="failed",
                    attempts=item.attempts + 1,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error="ordinary remember assertion conflict",
                    updated_at=now,
                )
            )
            await session.commit()
            await self._update_durable_depth_metric(session)

    async def _repair_failed_assistant_identity(
        self,
        session: Any,
        *,
        existing: RememberQueueRow,
        incoming_payload: dict[str, Any],
        now: datetime,
    ) -> bool:
        existing_payload = dict(existing.payload or {})
        event_hash_value = incoming_payload.get("event_hash")
        assistant_event_hash = incoming_payload.get("assistant_event_hash")
        dependency_id = incoming_payload.get("depends_on")
        if not (
            incoming_payload.get("queue_kind") == ORDINARY_ASSISTANT_QUEUE_KIND
            and isinstance(event_hash_value, str)
            and event_hash_value
            and isinstance(assistant_event_hash, str)
            and assistant_event_hash
            and isinstance(dependency_id, str)
            and dependency_id
            and existing.item_id
            == deterministic_queue_id(
                ORDINARY_ASSISTANT_QUEUE_KIND,
                event_hash_value,
                assistant_event_hash,
            )
            and existing_payload.get("queue_kind") == ORDINARY_ASSISTANT_QUEUE_KIND
            and "event_hash" not in existing_payload
            and existing_payload.get("assistant_event_hash") == assistant_event_hash
            and existing.status == "failed"
            and existing.attempts == 1
            and existing.last_error == "ordinary remember assertion conflict"
            and existing.lease_token is None
            and existing.lease_expires_at is None
            and existing.session_id == existing_payload.get("session_id")
            and existing.user_email == existing_payload.get("user_email")
            and existing.agent_id == existing_payload.get("agent_id")
        ):
            return False

        matching_fields = (
            "session_id",
            "cognis_session_id",
            "intaris_session_id",
            "conversation_id",
            "turn_id",
            "user_email",
            "owner_email",
            "agent_owner_email",
            "agent_id",
            "policy_agent_id",
            "depends_on",
            "include_user_message",
            "user_event_seq",
            "assistant_event_seq",
            "assistant_event_hash",
        )
        if any(
            existing_payload.get(field) != incoming_payload.get(field) for field in matching_fields
        ):
            return False

        expected_dependency_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash_value)
        if dependency_id != expected_dependency_id:
            return False
        dependency = await session.get(RememberQueueRow, dependency_id)
        dependency_payload = dict(dependency.payload or {}) if dependency is not None else {}
        dependency_fields = (
            "cognis_session_id",
            "intaris_session_id",
            "conversation_id",
            "turn_id",
            "user_email",
            "owner_email",
            "agent_owner_email",
            "agent_id",
            "policy_agent_id",
        )
        if not (
            dependency is not None
            and dependency.status in TERMINAL_EVIDENCE_OUTCOMES
            and dependency.user_email == dependency_payload.get("user_email")
            and dependency.agent_id == dependency_payload.get("agent_id")
            and dependency_payload.get("queue_kind") == EVIDENCE_QUEUE_KIND
            and dependency_payload.get("event_hash") == event_hash_value
            and all(
                dependency_payload.get(field) == incoming_payload.get(field)
                for field in dependency_fields
            )
        ):
            return False

        repaired_payload = {**existing_payload, "event_hash": event_hash_value}
        result = await session.execute(
            sa.update(RememberQueueRow)
            .execution_options(synchronize_session=False)
            .where(
                RememberQueueRow.item_id == existing.item_id,
                RememberQueueRow.status == "failed",
                RememberQueueRow.attempts == 1,
                RememberQueueRow.last_error == "ordinary remember assertion conflict",
                RememberQueueRow.lease_token.is_(None),
                RememberQueueRow.lease_expires_at.is_(None),
            )
            .values(
                payload=repaired_payload,
                status="pending",
                next_retry_at=now,
                last_error=None,
                updated_at=now,
            )
        )
        if not result.rowcount:
            return False
        logger.info(
            "Repaired failed pre-dispatch assistant queue identity",
            extra={
                "extra_data": {
                    "repair_class": "missing_assistant_event_source_hash",
                    "item_id_hash": _value_fingerprint(existing.item_id),
                    "identity_hash": _identity_fingerprint(repaired_payload),
                }
            },
        )
        return True

    async def _begin_evidence_attempt(self, item: RememberQueueItem) -> bool:
        """Apply age/attempt bounds and persist the exact increment first."""
        if self._session_factory is None or item.item_id is None:
            return False
        admission = deserialize_evidence_admission(item.payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
        if admission is None:
            await self._terminalize_evidence(
                item,
                "conflict",
                None,
                ordinary_work_needed=True,
            )
            return False
        created_at = datetime.fromisoformat(admission.admitted_at.replace("Z", "+00:00"))
        age = observe_queue_age(created_at, now=await self._database_clock())
        next_attempt = item.attempts + 1
        if age >= admission.max_age_seconds:
            item.attempts = next_attempt
            await self._terminalize_evidence(item, "unavailable", None)
            return False
        if item.attempts >= admission.max_attempts:
            item.attempts = next_attempt
            await self._terminalize_evidence(item, "abandoned", None)
            return False
        async with self._session_factory() as session:
            now = await database_now(session)
            result = await session.execute(
                sa.update(RememberQueueRow)
                .where(
                    RememberQueueRow.item_id == item.item_id,
                    RememberQueueRow.lease_token == item.lease_token,
                    RememberQueueRow.status == "leased",
                )
                .values(attempts=next_attempt, updated_at=now)
            )
            await session.commit()
        if not result.rowcount:
            return False
        item.attempts = next_attempt
        return True

    async def _evidence_dispatch_is_available(self, item: RememberQueueItem) -> bool:
        admission = deserialize_evidence_admission(item.payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
        if admission is None:
            await self._terminalize_evidence(
                item,
                "conflict",
                None,
                ordinary_work_needed=True,
            )
            return False
        created_at = datetime.fromisoformat(admission.admitted_at.replace("Z", "+00:00"))
        age = observe_queue_age(created_at, now=await self._database_clock())
        if age >= admission.max_age_seconds:
            await self._terminalize_evidence(item, "unavailable", None, ordinary_work_needed=True)
            return False
        if item.attempts > admission.max_attempts:
            await self._terminalize_evidence(item, "abandoned", None, ordinary_work_needed=True)
            return False
        return True

    async def _process_evidence_item(self, item: RememberQueueItem) -> None:
        """Dispatch evidence with fresh authority and retain its terminal ledger."""
        if self._session_factory is None or item.item_id is None:
            return
        try:
            if not self._validate_evidence_item(item):
                await self._terminalize_evidence(
                    item,
                    "conflict",
                    None,
                    ordinary_work_needed=True,
                )
                return
            if await self._hard_memory_disable_applies(item.payload):
                await self._terminalize_evidence(item, "skipped", None, ordinary_work_needed=True)
                return
            if not await self._begin_evidence_attempt(item):
                return
            body = await self._resolve_evidence_body(item.payload)
            if not await self._evidence_dispatch_is_available(item):
                return
            self._observe_policy_mismatch(item.payload)
            async with self._session_factory() as session:
                now = await database_now(session)
                dispatched = await session.execute(
                    sa.update(RememberQueueRow)
                    .where(
                        RememberQueueRow.item_id == item.item_id,
                        RememberQueueRow.lease_token == item.lease_token,
                        RememberQueueRow.status == "leased",
                        RememberQueueRow.lease_expires_at > now,
                    )
                    .values(status="dispatching", updated_at=now)
                )
                await session.commit()
            if not dispatched.rowcount:
                return

            try:
                signing_inputs = await self._revalidate_evidence_signing_inputs(item, body)
            except _EvidenceUnavailableError:
                await self._terminalize_evidence(
                    item, "unavailable", None, ordinary_work_needed=True
                )
                return
            result = await self.worker.remember_evidence(signing_inputs.body, signing_inputs.token)
            outcome = (
                result.status if result.status in TERMINAL_EVIDENCE_OUTCOMES else "unavailable"
            )
            if outcome in {"accepted", "replayed", "recovered", "skipped", "rejected", "conflict"}:
                await self._terminalize_evidence(item, outcome, result.status_code)
                return

            created_at = _normalize_utc(item.created_at) or _utcnow()
            age = observe_queue_age(created_at, now=await self._database_clock())
            admission = deserialize_evidence_admission(
                item.payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY)
            )
            max_attempts = admission.max_attempts if admission is not None else 1
            max_age_seconds = admission.max_age_seconds if admission is not None else 60
            if item.attempts >= max_attempts or age >= max_age_seconds:
                await self._terminalize_evidence(
                    item,
                    "unavailable" if age >= max_age_seconds else "abandoned",
                    result.status_code,
                )
            else:
                await self._retry_evidence(item, item.attempts, result.status_code)
        except TrustedEventRejectedError as exc:
            await self._fail_trusted_rejection(item, exc)
        except ValueError as exc:
            await self._terminalize_evidence(
                item,
                "conflict",
                None,
                error=exc,
                ordinary_work_needed=True,
            )
        except Exception as exc:
            created_at = _normalize_utc(item.created_at) or _utcnow()
            age = observe_queue_age(created_at, now=await self._database_clock())
            admission = deserialize_evidence_admission(
                item.payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY)
            )
            max_attempts = admission.max_attempts if admission is not None else 1
            max_age_seconds = admission.max_age_seconds if admission is not None else 60
            if item.attempts >= max_attempts or age >= max_age_seconds:
                await self._terminalize_evidence(
                    item,
                    "unavailable" if age >= max_age_seconds else "abandoned",
                    None,
                    error=exc,
                )
            else:
                await self._retry_evidence(item, item.attempts, None, error=exc)

    async def _fail_trusted_rejection(
        self, item: RememberQueueItem, error: TrustedEventRejectedError
    ) -> None:
        """Retain the complete signed source in the existing terminal queue row."""
        if self._session_factory is None or item.item_id is None:
            return
        payload = {
            **item.payload,
            "trusted_rejection": error.rejection.model_dump(mode="json"),
            "rejected_source": error.source,
        }
        evidence = payload.get("queue_kind") == EVIDENCE_QUEUE_KIND
        async with self._session_factory() as session:
            now = await database_now(session)
            result = await session.execute(
                sa.update(RememberQueueRow)
                .where(
                    RememberQueueRow.item_id == item.item_id,
                    RememberQueueRow.lease_token == item.lease_token,
                    RememberQueueRow.status.in_(["leased", "dispatching"]),
                )
                .values(
                    status="rejected" if evidence else "failed",
                    payload=payload,
                    last_error=str(error),
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                    next_retry_at=now,
                )
            )
            await session.commit()
            await self._update_durable_depth_metric(session)
        if result.rowcount:
            QUEUE_FAILED.inc()
            self._signal_wake()
            await self._record_failure_notice(item, str(error))

    async def _terminalize_evidence(
        self,
        item: RememberQueueItem,
        outcome: str,
        status_code: int | None,
        *,
        error: Exception | None = None,
        ordinary_work_needed: bool = False,
    ) -> None:
        if self._session_factory is None or item.item_id is None:
            return
        async with self._session_factory() as session:
            now = await database_now(session)
            payload = dict(item.payload)
            if ordinary_work_needed:
                payload["ordinary_work_needed"] = True
            result = await session.execute(
                sa.update(RememberQueueRow)
                .where(
                    RememberQueueRow.item_id == item.item_id,
                    RememberQueueRow.lease_token == item.lease_token,
                    RememberQueueRow.status.in_(["leased", "dispatching"]),
                )
                .values(
                    status=outcome,
                    payload=payload,
                    attempts=item.attempts,
                    lease_token=None,
                    lease_expires_at=None,
                    next_retry_at=now,
                    last_error=(
                        f"HTTP {status_code}"
                        if status_code is not None
                        else (self._sanitize_failure_detail(error) if error is not None else None)
                    ),
                    updated_at=now,
                )
            )
            await session.commit()
            await self._update_durable_depth_metric(session)
        if result.rowcount:
            observe_outcome(outcome)
            QUEUE_SUCCESS.inc()
            self._signal_wake()

    async def _database_clock(self) -> datetime:
        """Read the shared clock used by durable queue age and lease decisions."""
        assert self._session_factory is not None
        async with self._session_factory() as session:
            return await database_now(session)

    async def _reconcile_terminal_evidence_ledger_batch(
        self,
        state: _EvidenceReconciliationState,
        limit: int,
    ) -> tuple[int, bool]:
        """Materialize a bounded page of deferred ordinary work."""
        if self._session_factory is None:
            return 0, True
        rebuilt = 0
        async with self._session_factory() as session:
            now = await database_now(session)
            await self._serialize_capacity_transaction(session)
            query = (
                sa.select(RememberQueueRow)
                .where(RememberQueueRow.status.in_(TERMINAL_EVIDENCE_OUTCOMES))
                .order_by(RememberQueueRow.item_id.asc())
                .limit(limit)
            )
            if state.terminal_cursor is not None:
                query = query.where(RememberQueueRow.item_id > state.terminal_cursor)
            rows = (await session.execute(query)).scalars().all()
            if not rows:
                state.terminal_cursor = None
                return 0, True
            active_depth = await self._active_depth(session)
            last_scanned_item_id = state.terminal_cursor
            capacity_blocked = False
            for evidence_row in rows:
                payload = dict(evidence_row.payload or {})
                if payload.get("queue_kind") != EVIDENCE_QUEUE_KIND or not payload.get(
                    "ordinary_work_needed"
                ):
                    last_scanned_item_id = evidence_row.item_id
                    continue
                event_hash_value = payload.get("event_hash")
                if not isinstance(event_hash_value, str) or not event_hash_value:
                    last_scanned_item_id = evidence_row.item_id
                    continue
                ordinary_id = deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, event_hash_value)
                ordinary_row = await session.get(RememberQueueRow, ordinary_id)
                if ordinary_row is not None:
                    evidence_row.payload = {**payload, "ordinary_work_needed": False}
                    rebuilt += 1
                    last_scanned_item_id = evidence_row.item_id
                    continue
                if active_depth >= self.max_depth:
                    capacity_blocked = True
                    break
                ordinary_payload = {
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {
                        "item_id",
                        "queue_kind",
                        "status_override",
                        "trusted_rejection",
                        "rejected_source",
                    }
                }
                ordinary_payload.update(
                    {
                        "item_id": ordinary_id,
                        "queue_kind": ORDINARY_USER_QUEUE_KIND,
                        "depends_on": None,
                    }
                )
                await self._insert_row(
                    session,
                    ordinary_payload,
                    now,
                    status="pending",
                )
                evidence_row.payload = {**payload, "ordinary_work_needed": False}
                active_depth += 1
                rebuilt += 1
                last_scanned_item_id = evidence_row.item_id
            if rebuilt:
                await session.commit()
                await self._update_durable_depth_metric(session)
                self._signal_wake()
            state.terminal_cursor = last_scanned_item_id
            if capacity_blocked:
                return rebuilt, False
            if len(rows) < limit:
                state.terminal_cursor = None
                return rebuilt, True
        return rebuilt, False

    async def _revalidate_evidence_signing_inputs(
        self,
        item: RememberQueueItem,
        body: EvidenceRememberRequest,
        *,
        token_kind: Literal["evidence", "user_event"] = "evidence",
    ) -> _EvidenceSigningInputs:
        """Re-read policy immediately before signing, with no await after return."""
        assert self._session_factory is not None
        assert item.item_id is not None
        payload = item.payload
        async with self._session_factory() as session, session.begin():
            cognis_session = (
                await session.execute(
                    sa.select(Session)
                    .where(Session.session_id == str(payload.get("cognis_session_id") or ""))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            conversation = (
                (
                    await session.execute(
                        sa.select(Conversation)
                        .where(Conversation.conversation_id == cognis_session.conversation_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if cognis_session is not None
                else None
            )
            agent = (
                (
                    await session.execute(
                        sa.select(Agent)
                        .where(Agent.agent_id == cognis_session.agent_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if cognis_session is not None
                else None
            )
            if cognis_session is None or conversation is None or agent is None:
                raise ValueError("Trusted evidence authority is missing")
            if (
                agent.owner_email != body.actor.owner_id
                or agent.owner_email != str(payload.get("owner_email") or "")
                or not evidence_admission_authorizes(
                    payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY),
                    key=self._trusted_evidence_admission_key,
                    owner_id=agent.owner_email,
                )
            ):
                raise _EvidenceUnavailableError("Trusted evidence admission authority changed")
            admission = deserialize_evidence_admission(payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
            if admission is None or payload.get(
                "admission_event_id"
            ) != admission.event_binding.get("event_id"):
                raise _EvidenceUnavailableError("Trusted evidence canonical event identity changed")
            if payload.get("marker_admitted_at") != admission.admitted_at:
                raise _EvidenceUnavailableError("Trusted evidence admission timestamp changed")
            if (
                cognis_session.intaris_session_id != payload.get("intaris_session_id")
                or cognis_session.conversation_id != payload.get("conversation_id")
                or cognis_session.user_email != payload.get("user_email")
                or cognis_session.agent_id != payload.get("policy_agent_id")
                or cognis_session.agent_id != payload.get("agent_id")
                or conversation.user_email != cognis_session.user_email
                or conversation.agent_id != cognis_session.agent_id
                or (
                    payload.get("originating_agent_profile_id") is not None
                    and cognis_session.agent_profile_id
                    != payload.get("originating_agent_profile_id")
                )
            ):
                raise _EvidenceUnavailableError("Trusted evidence policy drift detected")
            if (
                body.event.event_hash != payload.get("event_hash")
                or body.event.cognis_session_id != cognis_session.session_id
                or body.event.conversation_id != cognis_session.conversation_id
                or body.event.turn_id != payload.get("turn_id")
                or body.actor.user_id != cognis_session.user_email
                or body.event.id
                != f"{payload.get('intaris_session_id')}:{payload.get('event_seq')}"
            ):
                raise ValueError("Trusted evidence body binding drift detected")
            signed_body = body.model_copy(deep=True)
            token = (
                self.worker.auth_provider.sign_user_event_jwt(signed_body)
                if token_kind == "user_event"
                else self.worker.auth_provider.sign_evidence_jwt(signed_body)
            )
            return _EvidenceSigningInputs(body=signed_body, token=token)

    async def _retry_evidence(
        self,
        item: RememberQueueItem,
        attempts: int,
        status_code: int | None,
        *,
        error: Exception | None = None,
    ) -> None:
        if self._session_factory is None or item.item_id is None:
            return
        delay = min(2**attempts, self.backoff_max)
        async with self._session_factory() as session:
            now = await database_now(session)
            result = await session.execute(
                sa.update(RememberQueueRow)
                .where(
                    RememberQueueRow.item_id == item.item_id,
                    RememberQueueRow.lease_token == item.lease_token,
                    RememberQueueRow.status == "dispatching",
                )
                .values(
                    status="pending",
                    attempts=attempts,
                    next_retry_at=now + timedelta(seconds=delay),
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=(
                        f"HTTP {status_code}"
                        if status_code is not None
                        else (
                            self._sanitize_failure_detail(error)
                            if error is not None
                            else "Trusted evidence unavailable"
                        )
                    ),
                    updated_at=now,
                )
            )
            await session.commit()
            await self._update_durable_depth_metric(session)
        if result.rowcount:
            QUEUE_FAILED.inc()
            asyncio.get_running_loop().call_later(delay, self._signal_wake)

    async def _resolve_evidence_body(self, payload: dict[str, Any]) -> EvidenceRememberRequest:
        """Re-read exact Intaris data and authoritative Cognis ownership."""
        if self._session_factory is None or self._event_reader is None:
            raise RuntimeError("Trusted evidence replay requires durable authority")
        cognis_session_id = str(payload.get("cognis_session_id") or "")
        intaris_session_id = str(payload.get("intaris_session_id") or "")
        seq = payload.get("event_seq")
        expected_hash = str(payload.get("event_hash") or "")
        if not cognis_session_id or not intaris_session_id or not isinstance(seq, int) or seq <= 0:
            raise ValueError("Trusted evidence assertion is incomplete")
        async with self._session_factory() as session:
            cognis_session = await session.get(Session, cognis_session_id)
            conversation = (
                await session.get(Conversation, cognis_session.conversation_id)
                if cognis_session is not None
                else None
            )
            agent = (
                await session.get(Agent, cognis_session.agent_id)
                if cognis_session is not None
                else None
            )
        if cognis_session is None or conversation is None or agent is None:
            raise ValueError("Trusted evidence authority is missing")
        if (
            cognis_session.intaris_session_id != intaris_session_id
            or cognis_session.conversation_id != str(payload.get("conversation_id") or "")
            or cognis_session.user_email != str(payload.get("user_email") or "")
            or cognis_session.agent_id != str(payload.get("policy_agent_id") or "")
            or cognis_session.agent_id != str(payload.get("agent_id") or "")
            or agent.owner_email != str(payload.get("owner_email") or "")
            or conversation.user_email != cognis_session.user_email
            or conversation.agent_id != cognis_session.agent_id
        ):
            raise ValueError("Trusted evidence authority drift detected")
        with scoped_runtime_context(
            user_email=cognis_session.user_email,
            agent_id=cognis_session.agent_id,
            agent_owner_email=agent.owner_email,
        ):
            read = await self._event_reader.read_events(
                session_id=intaris_session_id,
                seqs=[seq],
                types=["user_message"],
                allow_missing_stream=False,
            )
        if len(read.events) != 1:
            raise ValueError("Trusted evidence event assertion is not exact")
        event = read.events[0]
        event_type, event_seq, event_data = self._normalize_replay_event(event)
        if event_type != "user_message" or event_seq != seq:
            raise ValueError("Trusted evidence event type or sequence drift detected")
        marker = marker_for_event(event)
        if (
            marker is None
            or not marker_is_valid(
                marker,
                admission_key=self._trusted_evidence_admission_key,
            )
            or not marker_matches_event(
                marker,
                admission_key=self._trusted_evidence_admission_key,
                intaris_session_id=intaris_session_id,
                event_data=event_data,
            )
        ):
            raise ValueError("Trusted evidence marker is invalid")
        if (
            marker["user_id"] != cognis_session.user_email
            or marker["owner_id"] != agent.owner_email
            or marker["cognis_session_id"] != cognis_session_id
            or marker["conversation_id"] != cognis_session.conversation_id
            or marker["turn_id"] != str(payload.get("turn_id") or "")
        ):
            raise ValueError("Trusted evidence marker binding drift detected")
        marker_admission = deserialize_evidence_admission(
            marker.get(TRUSTED_EVIDENCE_ADMISSION_KEY)
        )
        payload_admission = deserialize_evidence_admission(
            payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY)
        )
        if (
            marker_admission is None
            or payload_admission is None
            or serialize_evidence_admission(marker_admission)
            != serialize_evidence_admission(payload_admission)
            or not evidence_admission_authorizes(
                marker_admission,
                key=self._trusted_evidence_admission_key,
                owner_id=agent.owner_email,
            )
        ):
            raise ValueError("Trusted evidence admission binding drift detected")
        if (
            event_data.get("source") != marker["source"]
            or event_data.get("role") != marker["role"]
            or event_data.get("prompt_visibility") != marker["prompt_visibility"]
            or event_data.get("prompt_provenance") != marker["prompt_provenance"]
            or event_data.get("attachments", []) != marker["attachment_refs"]
            or event_data.get("turn_id") != marker["turn_id"]
        ):
            raise ValueError("Trusted evidence event provenance drift detected")
        content = event_data.get("user_visible_content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Trusted evidence user-visible content is missing")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != marker["content_hash"]:
            raise ValueError("Trusted evidence content hash mismatch")
        marker_without_hash = {key: value for key, value in marker.items() if key != "marker_hash"}
        if sha256_hex({**marker_without_hash, "content": content}) != marker["marker_hash"]:
            raise ValueError("Trusted evidence marker hash mismatch")
        actual_hash = event_hash(intaris_session_id, seq, event)
        if actual_hash != expected_hash:
            raise ValueError("Trusted evidence event hash mismatch")
        if actual_hash != str(marker.get("event_hash") or expected_hash):
            raise ValueError("Trusted evidence marker event hash mismatch")
        return EvidenceRememberRequest.model_validate(
            {
                "version": 1,
                "actor": {"user_id": cognis_session.user_email, "owner_id": agent.owner_email},
                "event": {
                    "id": f"{intaris_session_id}:{seq}",
                    "event_hash": actual_hash,
                    "cognis_session_id": cognis_session_id,
                    "conversation_id": cognis_session.conversation_id,
                    "turn_id": marker["turn_id"],
                },
                "messages": [{"role": "user", "content": content}],
            }
        )

    async def _renew_durable_lease(
        self, item: RememberQueueItem, lease_lost: asyncio.Event
    ) -> None:
        assert self._session_factory is not None
        assert item.item_id is not None
        while not lease_lost.is_set():
            await asyncio.sleep(max(self.lease_seconds / 3, 0.01))
            async with self._session_factory() as session:
                now = await database_now(session)
                result = await session.execute(
                    sa.update(RememberQueueRow)
                    .execution_options(synchronize_session=False)
                    .where(
                        RememberQueueRow.item_id == item.item_id,
                        RememberQueueRow.lease_token == item.lease_token,
                        RememberQueueRow.status.in_(["leased", "dispatching"]),
                        RememberQueueRow.lease_expires_at > now,
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                        updated_at=now,
                    )
                )
                await session.commit()
            if not result.rowcount:
                lease_lost.set()
                return

    async def _hard_memory_disable_applies(self, payload: dict[str, Any]) -> bool:
        """Recheck current hard backend/profile vetoes before queued execution."""

        if self._session_factory is None:
            return False
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return False
        async with self._session_factory() as session:
            cognis_session = await session.get(Session, str(payload.get("cognis_session_id") or ""))
            row = (
                await session.execute(sa.select(Agent).where(Agent.agent_id == agent_id).limit(1))
            ).scalar_one_or_none()
        if row is None or cognis_session is None or cognis_session.agent_id != agent_id:
            if payload.get("queue_kind") is not None:
                return payload.get("queue_kind") == EVIDENCE_QUEUE_KIND
            return row is not None
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
        profile_id = cognis_session.agent_profile_id
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
                .where(RememberQueueRow.status.in_(["pending", "leased", "dispatching"]))
            )
            return bool(count)

    async def _update_durable_depth_metric(self, session: Any) -> None:
        previous_depth = self._durable_active_depth
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(RememberQueueRow)
            .where(RememberQueueRow.status.in_(["pending", "leased", "dispatching"]))
        )
        self._durable_active_depth = int(count or 0)
        if self._durable_active_depth < previous_depth:
            self._signal_ledger_repair()
        QUEUE_DEPTH.set(self._durable_active_depth)
        oldest = await session.scalar(
            sa.select(sa.func.min(RememberQueueRow.created_at)).where(
                RememberQueueRow.payload["queue_kind"].as_string() == EVIDENCE_QUEUE_KIND,
                RememberQueueRow.status.in_(["pending", "leased", "dispatching"]),
            )
        )
        normalized_oldest = _normalize_utc(oldest)
        age = (
            observe_queue_age(normalized_oldest, now=await database_now(session))
            if normalized_oldest is not None
            else 0.0
        )
        from cognis.core.trusted_evidence import EVIDENCE_QUEUE_AGE_SECONDS

        EVIDENCE_QUEUE_AGE_SECONDS.set(age)
        await self._refresh_policy_mismatch_status(session)

    def _observe_policy_mismatch(self, payload: dict[str, Any]) -> bool:
        admission = deserialize_evidence_admission(payload.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
        mismatch = bool(
            admission is not None
            and admission.admitted
            and admission.policy_fingerprint != self.trusted_evidence_policy_fingerprint
        )
        if mismatch and not self._policy_mismatch_logged:
            EVIDENCE_POLICY_MISMATCH_TOTAL.inc()
            logger.warning(
                "Trusted evidence worker observed another admission policy",
                extra={"extra_data": {"queue": EVIDENCE_QUEUE_KIND}},
            )
            self._policy_mismatch_logged = True
        if mismatch:
            self._policy_mismatch_active = True
            EVIDENCE_POLICY_MISMATCH.set(1)
        return mismatch

    async def _refresh_policy_mismatch_status(self, session: Any) -> None:
        active_payloads = (
            await session.execute(
                sa.select(RememberQueueRow.payload).where(
                    RememberQueueRow.payload["queue_kind"].as_string() == EVIDENCE_QUEUE_KIND,
                    RememberQueueRow.status.in_(["pending", "leased", "dispatching"]),
                )
            )
        ).scalars()
        active = any(
            self._observe_policy_mismatch(dict(payload or {})) for payload in active_payloads
        )
        self._policy_mismatch_active = active
        if not active:
            self._policy_mismatch_logged = False
        EVIDENCE_POLICY_MISMATCH.set(1 if active else 0)

    @staticmethod
    def _durable_payload(payload: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in payload.items() if key != "status_override"}
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
                if not isinstance(event_seq, int):
                    continue
                if payload.get("event_hash") is not None and payload.get(
                    "event_hash"
                ) != event_hash(intaris_session_id, event_seq, event):
                    raise RuntimeError("Remember user event hash drift detected")
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
            if not isinstance(event_seq, int):
                continue
            if payload.get("assistant_event_hash") is not None and payload.get(
                "assistant_event_hash"
            ) != event_hash(intaris_session_id, event_seq, event):
                raise RuntimeError("Remember assistant event hash drift detected")
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
        event_reader = self._event_reader
        if event_reader is None:
            raise RuntimeError("Remember queue replay requires an Intaris event reader")
        if requested_seqs:
            try:
                return await event_reader.read_events(
                    session_id=intaris_session_id,
                    seqs=requested_seqs,
                    types=["user_message", "assistant_message"],
                    allow_missing_stream=True,
                )
            except TypeError as exc:
                if "seqs" not in str(exc):
                    raise

        return await event_reader.read_events(
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
            raw_data = event.get("data")
            event_data = raw_data if isinstance(raw_data, dict) else {}
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

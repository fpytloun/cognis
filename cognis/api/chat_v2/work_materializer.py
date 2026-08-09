"""Durable PostgreSQL materialization for the Work read model."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import posixpath
import random
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

from prometheus_client import Counter, Gauge
from pydantic import TypeAdapter
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.api.chat_v2.background_event_reads import BackgroundEventReadAdmission
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventStore
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    AssistantDeliverableTimelineItem,
    FileDiffRef,
    TimelineItem,
    ToolCallTimelineItem,
)
from cognis.api.chat_v2.work_projection import is_work_evidence_item, work_item_category
from cognis.logging import get_logger
from cognis.models.tool import ToolDefinition
from cognis.providers.guardrails.events import EventAppendNotification, EventStoreAuthority
from cognis.store.models import (
    Agent,
    Session,
    WorkRecordFileRow,
    WorkRecordRow,
    WorkSessionProjectionRow,
)

logger = get_logger(__name__)

WORK_MATERIALIZER_VERSION = "work-v4"
WORK_RECORD_MAX_BYTES = 128 * 1024
WORK_REPAIR_PAGE_SIZE = 500
WORK_REPAIR_CONCURRENCY = 2
WORK_APPEND_WORKERS = 2
WORK_APPEND_MAX_PENDING_SESSIONS = 1024
WORK_APPEND_MAX_PENDING_EVENTS = 4096
WORK_APPEND_MAX_PENDING_BYTES = 8 * 1024 * 1024
WORK_APPEND_MAX_SESSION_EVENTS = 1000
WORK_APPEND_MAX_SESSION_BYTES = 2 * 1024 * 1024
WORK_APPEND_RETRY_INITIAL_SECONDS = 0.25
WORK_APPEND_RETRY_MAX_SECONDS = 30.0
WORK_APPEND_RETRY_JITTER_RATIO = 0.2
_LEASE_SECONDS = 30
_TIMELINE_ADAPTER: TypeAdapter[TimelineItem] = TypeAdapter(TimelineItem)
_TERMINAL_TOOL_STATUSES = {
    "complete",
    "failed",
    "cancelled",
    "denied",
    "compacted",
    "skipped",
}


def _merged_tool_status(previous: str | None, current: str | None) -> str | None:
    """Advance to terminal state without allowing terminal replay regression."""

    if previous in _TERMINAL_TOOL_STATUSES:
        return previous
    if current in _TERMINAL_TOOL_STATUSES:
        return current
    return current or previous


WORK_APPEND_PENDING = Gauge(
    "cognis_work_append_pending",
    "Pending controller-local session-coalesced Work append batches.",
)
WORK_APPEND_PENDING_BYTES = Gauge(
    "cognis_work_append_pending_bytes",
    "Retained event payload bytes in the controller-local Work append queue.",
)
WORK_APPEND_OUTCOMES = Counter(
    "cognis_work_append_queue_total",
    "Controller-local Work append queue outcomes.",
    ["outcome"],
)


@dataclass(slots=True)
class _PendingWorkAppend:
    authority: EventStoreAuthority
    session_id: str
    first_seq: int
    last_seq: int
    target_seq: int
    events: tuple[Any, ...]
    payload_bytes: int
    repair_required: bool = False
    retry_count: int = 0
    retry_not_before: float = 0.0

    @property
    def retained_events(self) -> int:
        return len(self.events)


async def lock_work_projection_state(db: AsyncSession, session_id: str) -> None:
    """Serialize deterministic projection-state creation across replicas."""

    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"cognis-work-projection:{session_id}:{WORK_MATERIALIZER_VERSION}"},
        )


def _record_id(owner: str, source_session_id: str, seq: int, ordinal: int) -> str:
    value = f"{owner}\0{source_session_id}\0{seq}\0{ordinal}\0{WORK_MATERIALIZER_VERSION}"
    return f"wrk_{hashlib.sha256(value.encode()).hexdigest()[:40]}"


def _projection_id(session_id: str) -> str:
    return f"wsp_{hashlib.sha256(f'{session_id}:{WORK_MATERIALIZER_VERSION}'.encode()).hexdigest()[:40]}"


def _bounded_item(item: TimelineItem) -> dict[str, Any]:
    payload = item.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) <= WORK_RECORD_MAX_BYTES:
        return payload
    if isinstance(item, ToolCallTimelineItem):
        payload["result_preview"] = "[Work evidence truncated]"
        payload["streamed_output"] = None
        payload["attachments"] = []
        payload["arguments"] = {}
        payload["arguments_preview"] = None
        payload["truncated"] = True
        payload["has_full_output"] = bool(item.has_full_output or item.tool_output_artifact_id)
        payload["file_diffs"] = []
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > WORK_RECORD_MAX_BYTES:
        raise ValueError("projected Work TimelineItem exceeds the storage bound")
    return payload


def _diff_counts(diff: FileDiffRef) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in diff.diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return (
        diff.additions if diff.additions is not None else additions,
        diff.deletions if diff.deletions is not None else deletions,
    )


def _file_path_id(diff: FileDiffRef, *, workdir: str | None) -> str:
    if diff.path_id:
        return diff.path_id
    if diff.root_id and diff.relative_path is not None:
        return f"{diff.root_id}:{diff.relative_path}"
    raw_path = (diff.relative_path or diff.path).replace("\\", "/")
    raw_workdir = (workdir or "").replace("\\", "/")
    normalized = posixpath.normpath(raw_path)
    normalized_workdir = posixpath.normpath(raw_workdir).rstrip("/") if raw_workdir else ""
    casefold = bool(
        len(normalized_workdir) >= 2
        and normalized_workdir[1] == ":"
        or len(normalized) >= 2
        and normalized[1] == ":"
    )
    comparable = normalized.lower() if casefold else normalized
    comparable_workdir = normalized_workdir.lower() if casefold else normalized_workdir
    if comparable_workdir and (
        comparable == comparable_workdir or comparable.startswith(f"{comparable_workdir}/")
    ):
        relative = normalized[len(normalized_workdir) :].removeprefix("/")
        root_id = hashlib.sha256(f"root\0{comparable_workdir}".encode()).hexdigest()[:24]
        return f"{root_id}:{relative}"
    relative = normalized
    is_drive_absolute = len(normalized) >= 3 and normalized[1:3] == ":/"
    if normalized_workdir and not normalized.startswith("/") and not is_drive_absolute:
        root_id = hashlib.sha256(f"root\0{comparable_workdir}".encode()).hexdigest()[:24]
        return f"{root_id}:{relative}"
    return f"unbound:{hashlib.sha256(relative.encode()).hexdigest()[:24]}"


def _record_file_rows(
    *,
    work_record_id: str,
    item: TimelineItem,
) -> list[WorkRecordFileRow]:
    if not isinstance(item, ToolCallTimelineItem):
        return []
    workdir = item.arguments.get("workdir") if isinstance(item.arguments, dict) else None
    workdir = workdir if isinstance(workdir, str) else None
    rows: list[WorkRecordFileRow] = []
    for ordinal, diff in enumerate(item.file_diffs):
        additions, deletions = _diff_counts(diff)
        digest = hashlib.sha256(f"{work_record_id}\0{ordinal}".encode()).hexdigest()[:40]
        rows.append(
            WorkRecordFileRow(
                work_record_file_id=f"wrf_{digest}",
                work_record_id=work_record_id,
                file_ordinal=ordinal,
                path=diff.path,
                path_id=_file_path_id(diff, workdir=workdir),
                additions=additions,
                deletions=deletions,
            )
        )
    return rows


def _record_metadata(
    item: TimelineItem,
    definitions: Mapping[str, ToolDefinition],
) -> dict[str, Any]:
    entity_id = None
    file_path_ids: list[str] = []
    additions = 0
    deletions = 0
    if isinstance(item, ArtifactTimelineItem):
        entity_id = item.artifact_id
    elif isinstance(item, AssistantDeliverableTimelineItem):
        entity_id = item.deliverable_id
    elif isinstance(item, ToolCallTimelineItem):
        for diff in item.file_diffs:
            added, deleted = _diff_counts(diff)
            additions += added
            deletions += deleted
    return {
        "category": work_item_category(item, definitions),
        "entity_id": entity_id,
        "file_path_ids": file_path_ids,
        "additions": additions,
        "deletions": deletions,
    }


def _raw_events(
    source_session_id: str,
    first_seq: int,
    events: Sequence[Any],
) -> list[RawSessionEvent]:
    result: list[RawSessionEvent] = []
    for offset, event in enumerate(events):
        data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
        event_type = data.get("type")
        event_type = getattr(event_type, "value", event_type)
        result.append(
            RawSessionEvent(
                store_id="intaris",
                session_id=source_session_id,
                seq=first_seq + offset,
                type=str(event_type),
                data=dict(data.get("data") or {}),
                timestamp=data.get("timestamp"),
            )
        )
    return result


class WorkMaterializer:
    """Materialize live appends and repair missing streams in the background."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        event_store: Any,
        tool_definitions: Callable[[], Mapping[str, ToolDefinition]],
        worker_id: str | None = None,
        event_read_admission: BackgroundEventReadAdmission | None = None,
        on_projection_caught_up: Callable[[str], None] | None = None,
        append_worker_count: int = WORK_APPEND_WORKERS,
        append_max_pending_sessions: int = WORK_APPEND_MAX_PENDING_SESSIONS,
        append_max_pending_events: int = WORK_APPEND_MAX_PENDING_EVENTS,
        append_max_pending_bytes: int = WORK_APPEND_MAX_PENDING_BYTES,
        append_max_session_events: int = WORK_APPEND_MAX_SESSION_EVENTS,
        append_max_session_bytes: int = WORK_APPEND_MAX_SESSION_BYTES,
        append_retry_initial_seconds: float = WORK_APPEND_RETRY_INITIAL_SECONDS,
        append_retry_max_seconds: float = WORK_APPEND_RETRY_MAX_SECONDS,
        append_retry_jitter_ratio: float = WORK_APPEND_RETRY_JITTER_RATIO,
        clock: Callable[[], float] = monotonic,
        retry_random: Callable[[], float] = random.random,
    ) -> None:
        if append_worker_count < 1 or append_worker_count > 16:
            raise ValueError("append_worker_count must be in 1..16")
        for name, value, maximum in (
            ("append_max_pending_sessions", append_max_pending_sessions, 4096),
            ("append_max_pending_events", append_max_pending_events, 100_000),
            ("append_max_pending_bytes", append_max_pending_bytes, 64 * 1024 * 1024),
            ("append_max_session_events", append_max_session_events, 10_000),
            ("append_max_session_bytes", append_max_session_bytes, 16 * 1024 * 1024),
        ):
            if value < 1 or value > maximum:
                raise ValueError(f"{name} must be in 1..{maximum}")
        if append_retry_initial_seconds <= 0:
            raise ValueError("append_retry_initial_seconds must be positive")
        if append_retry_max_seconds < append_retry_initial_seconds:
            raise ValueError(
                "append_retry_max_seconds must be greater than or equal to initial retry"
            )
        if not 0 <= append_retry_jitter_ratio <= 1:
            raise ValueError("append_retry_jitter_ratio must be in 0..1")
        self._session_factory = session_factory
        self._event_store = event_store
        self._tool_definitions = tool_definitions
        self._worker_id = worker_id or str(uuid.uuid4())
        self._event_read_admission = event_read_admission
        self._on_projection_caught_up = on_projection_caught_up
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._repair_slots = asyncio.Semaphore(WORK_REPAIR_CONCURRENCY)
        self._append_worker_count = append_worker_count
        self._append_max_pending_sessions = append_max_pending_sessions
        self._append_max_pending_events = append_max_pending_events
        self._append_max_pending_bytes = append_max_pending_bytes
        self._append_max_session_events = append_max_session_events
        self._append_max_session_bytes = append_max_session_bytes
        self._append_retry_initial_seconds = append_retry_initial_seconds
        self._append_retry_max_seconds = append_retry_max_seconds
        self._append_retry_jitter_ratio = append_retry_jitter_ratio
        self._clock = clock
        self._retry_random = retry_random
        self._append_pending: OrderedDict[str, _PendingWorkAppend] = OrderedDict()
        self._append_repair_pending: OrderedDict[str, _PendingWorkAppend] = OrderedDict()
        self._append_pending_events = 0
        self._append_pending_bytes = 0
        self._append_active: dict[str, _PendingWorkAppend] = {}
        self._append_available = asyncio.Event()
        self._append_workers: list[asyncio.Task[None]] = []
        self._append_accepting = False
        self._append_stopping = False
        self._update_append_gauges()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="work-materializer")
        if not self._append_workers:
            self._append_accepting = True
            self._append_stopping = False
            self._append_workers = [
                asyncio.create_task(
                    self._run_append_worker(),
                    name=f"work-append-materializer-{index}",
                )
                for index in range(self._append_worker_count)
            ]

    async def stop(self, *, timeout_seconds: float = 2.0) -> None:
        self._append_accepting = False
        self._append_stopping = True
        self._append_available.set()
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
        tasks = tuple(task for task in (self._task, *self._append_workers) if task is not None)
        timed_out = False
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                timed_out = True
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        if self._append_pending or self._append_repair_pending:
            self._append_outcome(
                "shutdown_repair",
                len(self._append_pending) + len(self._append_repair_pending),
            )
            self._wake.set()
        self._append_workers.clear()
        if timed_out and (self._append_pending or self._append_repair_pending):
            self._update_append_gauges()
            raise TimeoutError("Work append shutdown timed out with retained repair intents")
        self._append_pending.clear()
        self._append_repair_pending.clear()
        self._append_pending_events = 0
        self._append_pending_bytes = 0
        self._append_active.clear()
        self._update_append_gauges()

    async def handle_append(self, notification: EventAppendNotification) -> None:
        """Compatibility shim for callers outside the provider listener."""

        self.enqueue_append(notification)

    def enqueue_append(self, notification: EventAppendNotification) -> bool:
        """Synchronously admit bounded append work without database I/O."""

        if notification.event_count == 0:
            return True
        if not self._append_accepting:
            self._append_outcome("not_accepting")
            self._wake.set()
            return False
        key = self._append_key(notification.authority, notification.session_id)
        item = _PendingWorkAppend(
            authority=notification.authority,
            session_id=notification.session_id,
            first_seq=notification.first_seq,
            last_seq=notification.last_seq,
            target_seq=notification.last_seq,
            events=tuple(notification.events),
            payload_bytes=notification.payload_bytes,
            repair_required=not notification.events,
        )
        repair_pending = self._append_repair_pending.get(key)
        if repair_pending is not None:
            self._enqueue_append_repair(key, item)
            self._append_outcome("coalesced")
            self._append_available.set()
            return True
        active = self._append_active.get(key)
        if active is not None:
            if item.target_seq <= active.target_seq:
                self._append_outcome("coalesced")
                return True
            item.retry_count = active.retry_count
            self._enqueue_append_repair(key, item)
            self._append_outcome("coalesced")
            return True
        if (
            item.retained_events > self._append_max_session_events
            or item.payload_bytes > self._append_max_session_bytes
        ):
            item = self._repair_item(item)
            self._append_outcome("payload_overflow")
        current = self._append_pending.pop(key, None)
        if current is not None:
            self._remove_append_accounting(current)
            item = self._merge_append_items(current, item)
            self._append_outcome("coalesced")
        if item.repair_required:
            self._enqueue_append_repair(key, item)
            self._append_outcome("admitted")
            return True
        if current is None and len(self._append_pending) >= self._append_max_pending_sessions:
            evicted_key, evicted = next(iter(self._append_pending.items()))
            self._enqueue_append_repair(evicted_key, evicted)
            self._append_pending.pop(evicted_key)
            self._remove_append_accounting(evicted)
            self._append_outcome("evicted")
            self._wake.set()
        if (
            self._append_pending_events + item.retained_events > self._append_max_pending_events
            or self._append_pending_bytes + item.payload_bytes > self._append_max_pending_bytes
        ):
            item = self._repair_item(item)
            self._append_outcome("queue_overflow")
            self._enqueue_append_repair(key, item)
            self._append_outcome("admitted")
            return True
        self._append_pending[key] = item
        self._add_append_accounting(item)
        self._append_outcome("admitted")
        self._append_available.set()
        return True

    async def _run_append_worker(self) -> None:
        while not self._append_stopping or self._append_pending or self._append_repair_pending:
            popped = self._pop_append()
            if popped is None:
                self._append_available.clear()
                if (
                    self._append_stopping
                    and not self._append_pending
                    and not self._append_repair_pending
                ):
                    return
                retry_delay = self._next_append_retry_delay()
                if retry_delay is None:
                    await self._append_available.wait()
                else:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(retry_delay):
                            await self._append_available.wait()
                continue
            key, item = popped
            try:
                await self._process_append(item)
                self._append_outcome("processed")
                if item.repair_required:
                    logger.info(
                        "Work append repair persisted",
                        extra={"extra_data": {"target_seq": item.target_seq}},
                    )
            except asyncio.CancelledError:
                self._enqueue_append_repair(key, item)
                self._append_outcome("shutdown_repair")
                self._wake.set()
                raise
            except Exception:
                self._append_outcome("failed")
                logger.warning(
                    "Work append materialization failed; attempting durable repair",
                    exc_info=True,
                )
                repair_persisted = False
                if not item.repair_required:
                    try:
                        repair_persisted = await self.mark_source_pending(
                            owner_email=item.authority.user_email,
                            source_session_id=item.session_id,
                            target_seq=item.target_seq,
                        )
                    except asyncio.CancelledError:
                        self._enqueue_append_repair(key, item)
                        self._append_outcome("shutdown_repair")
                        logger.warning(
                            "Work append repair retained for retry during shutdown",
                            extra={"extra_data": {"target_seq": item.target_seq}},
                        )
                        raise
                    except Exception:
                        logger.warning(
                            "Work append durable repair persistence failed",
                            exc_info=True,
                        )
                    else:
                        logger.info(
                            (
                                "Work append repair persisted"
                                if repair_persisted
                                else "Work append repair source is not available"
                            ),
                            extra={"extra_data": {"target_seq": item.target_seq}},
                        )
                if not repair_persisted:
                    retry = self._retry_append_repair(item)
                    self._enqueue_append_repair(key, retry)
                    self._append_outcome("repair_retry")
                    logger.warning(
                        "Work append repair retained for retry",
                        extra={
                            "extra_data": {
                                "target_seq": retry.target_seq,
                                "retry_count": retry.retry_count,
                            }
                        },
                    )
            finally:
                active = self._append_active.pop(key, None)
                if active is not None:
                    self._remove_append_accounting(active)
                if self._append_pending or self._append_repair_pending:
                    self._append_available.set()

    async def _process_append(self, item: _PendingWorkAppend) -> None:
        if item.repair_required or not item.events:
            persisted = await self.mark_source_pending(
                owner_email=item.authority.user_email,
                source_session_id=item.session_id,
                target_seq=item.target_seq,
            )
            if not persisted:
                raise RuntimeError("Work append repair source session is not available")
            return
        async with self._session_factory() as db:
            row = await db.scalar(
                select(Session).where(
                    Session.user_email == item.authority.user_email,
                    Session.intaris_session_id == item.session_id,
                )
            )
            if row is None:
                raise RuntimeError("Work append source session is not available")
            state = await self._ensure_state(db, row, item.target_seq)
            if state.lease_owner is not None:
                state.target_seq = max(state.target_seq, item.target_seq)
                if state.state == "caught_up":
                    state.state = "repair"
                await db.commit()
                self._wake.set()
                return
            if item.first_seq != state.covered_through_seq + 1:
                state.target_seq = max(state.target_seq, item.target_seq)
                state.state = "repair"
                state.last_error = "noncontiguous live append"
                await db.commit()
                self._wake.set()
                return
            try:
                await self._materialize_batch(
                    db,
                    row=row,
                    state=state,
                    raw_events=_raw_events(
                        item.session_id,
                        item.first_seq,
                        item.events,
                    ),
                    target_seq=item.target_seq,
                )
                await db.commit()
                if state.state == "caught_up":
                    self._notify_projection_caught_up(row.conversation_id)
            except Exception as exc:
                await db.rollback()
                await self._mark_repair(row.session_id, item.target_seq, str(exc))
                raise

    @staticmethod
    def _append_key(authority: EventStoreAuthority, session_id: str) -> str:
        return f"{authority.user_email}\0{session_id}"

    @staticmethod
    def _repair_item(item: _PendingWorkAppend) -> _PendingWorkAppend:
        return _PendingWorkAppend(
            authority=item.authority,
            session_id=item.session_id,
            first_seq=item.first_seq,
            last_seq=item.last_seq,
            target_seq=item.target_seq,
            events=(),
            payload_bytes=0,
            repair_required=True,
            retry_count=item.retry_count,
            retry_not_before=item.retry_not_before,
        )

    def _merge_append_items(
        self,
        current: _PendingWorkAppend,
        candidate: _PendingWorkAppend,
    ) -> _PendingWorkAppend:
        target_seq = max(current.target_seq, candidate.target_seq)
        if (
            current.authority != candidate.authority
            or current.repair_required
            or candidate.repair_required
        ):
            return self._repair_item(
                _PendingWorkAppend(
                    authority=candidate.authority,
                    session_id=candidate.session_id,
                    first_seq=min(current.first_seq, candidate.first_seq),
                    last_seq=max(current.last_seq, candidate.last_seq),
                    target_seq=target_seq,
                    events=(),
                    payload_bytes=0,
                    repair_required=True,
                )
            )
        if current.last_seq + 1 == candidate.first_seq:
            first, last = current, candidate
        elif candidate.last_seq + 1 == current.first_seq:
            first, last = candidate, current
        else:
            self._append_outcome("noncontiguous")
            return self._repair_item(
                _PendingWorkAppend(
                    authority=candidate.authority,
                    session_id=candidate.session_id,
                    first_seq=min(current.first_seq, candidate.first_seq),
                    last_seq=max(current.last_seq, candidate.last_seq),
                    target_seq=target_seq,
                    events=(),
                    payload_bytes=0,
                    repair_required=True,
                )
            )
        merged = _PendingWorkAppend(
            authority=first.authority,
            session_id=first.session_id,
            first_seq=first.first_seq,
            last_seq=last.last_seq,
            target_seq=target_seq,
            events=first.events + last.events,
            payload_bytes=first.payload_bytes + last.payload_bytes,
        )
        if (
            merged.retained_events > self._append_max_session_events
            or merged.payload_bytes > self._append_max_session_bytes
        ):
            self._append_outcome("payload_overflow")
            return self._repair_item(merged)
        return merged

    def _pop_append(self) -> tuple[str, _PendingWorkAppend] | None:
        for pending in (self._append_repair_pending, self._append_pending):
            for key in tuple(pending):
                if key in self._append_active:
                    continue
                if pending[key].retry_not_before > self._clock():
                    continue
                item = pending.pop(key)
                self._append_active[key] = item
                return key, item
        return None

    def _enqueue_append_repair(
        self,
        key: str,
        item: _PendingWorkAppend,
    ) -> None:
        repair = self._repair_item(item)
        current = self._append_repair_pending.get(key)
        if current is not None:
            current.target_seq = max(current.target_seq, repair.target_seq)
            current.first_seq = min(current.first_seq, repair.first_seq)
            current.last_seq = max(current.last_seq, repair.last_seq)
            current.retry_count = max(current.retry_count, repair.retry_count)
            if current.retry_not_before == 0.0:
                current.retry_not_before = repair.retry_not_before
            elif repair.retry_not_before > 0.0:
                current.retry_not_before = min(
                    current.retry_not_before,
                    repair.retry_not_before,
                )
            self._append_repair_pending.move_to_end(key)
        else:
            self._append_repair_pending[key] = repair
        self._append_available.set()
        self._update_append_gauges()

    def _retry_append_repair(self, item: _PendingWorkAppend) -> _PendingWorkAppend:
        retry = self._repair_item(item)
        retry.retry_count = min(item.retry_count + 1, 31)
        delay = min(
            self._append_retry_max_seconds,
            self._append_retry_initial_seconds * (2 ** (retry.retry_count - 1)),
        )
        jitter = 1.0 + (((self._retry_random() * 2.0) - 1.0) * self._append_retry_jitter_ratio)
        retry.retry_not_before = self._clock() + max(0.001, delay * jitter)
        return retry

    def _next_append_retry_delay(self) -> float | None:
        retry_times = [
            item.retry_not_before
            for key, item in self._append_repair_pending.items()
            if key not in self._append_active and item.retry_not_before > 0.0
        ]
        if not retry_times:
            return None
        return max(0.001, min(retry_times) - self._clock())

    def _add_append_accounting(self, item: _PendingWorkAppend) -> None:
        self._append_pending_events += item.retained_events
        self._append_pending_bytes += item.payload_bytes
        self._update_append_gauges()

    def _remove_append_accounting(self, item: _PendingWorkAppend) -> None:
        self._append_pending_events -= item.retained_events
        self._append_pending_bytes -= item.payload_bytes
        self._update_append_gauges()

    def _update_append_gauges(self) -> None:
        with contextlib.suppress(Exception):
            WORK_APPEND_PENDING.set(
                len(self._append_pending)
                + len(self._append_repair_pending)
                + len(self._append_active)
            )
        with contextlib.suppress(Exception):
            WORK_APPEND_PENDING_BYTES.set(self._append_pending_bytes)

    @staticmethod
    def _append_outcome(outcome: str, amount: int = 1) -> None:
        with contextlib.suppress(Exception):
            WORK_APPEND_OUTCOMES.labels(outcome=outcome).inc(amount)

    async def prioritize_sessions(self, rows: Sequence[Session]) -> None:
        async with self._session_factory() as db:
            for row in rows:
                state = await self._ensure_state(db, row, 0)
                state.priority = max(state.priority, 100)
                if state.state == "caught_up" and state.target_seq == 0:
                    state.state = "pending"
            await db.commit()
        self._wake.set()

    async def mark_source_pending(
        self, *, owner_email: str, source_session_id: str, target_seq: int
    ) -> bool:
        async with self._session_factory() as db:
            row = await db.scalar(
                select(Session).where(
                    Session.user_email == owner_email,
                    Session.intaris_session_id == source_session_id,
                )
            )
            if row is None:
                return False
            state = await self._ensure_state(db, row, target_seq)
            state.state = "repair"
            await db.commit()
        self._wake.set()
        return True

    async def _ensure_state(
        self, db: AsyncSession, row: Session, target_seq: int
    ) -> WorkSessionProjectionRow:
        await lock_work_projection_state(db, row.session_id)
        state = await db.scalar(
            select(WorkSessionProjectionRow).where(
                WorkSessionProjectionRow.session_id == row.session_id,
                WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
            )
        )
        source_session_id = row.intaris_session_id or row.session_id
        if state is None:
            await db.execute(
                text(
                    """
                    INSERT INTO work_session_projections (
                        projection_id,
                        owner_email,
                        session_id,
                        source_session_id,
                        materializer_version,
                        target_seq,
                        state
                    ) VALUES (
                        :projection_id,
                        :owner_email,
                        :session_id,
                        :source_session_id,
                        :materializer_version,
                        :target_seq,
                        'pending'
                    )
                    ON CONFLICT (session_id, materializer_version) DO NOTHING
                    """
                ),
                {
                    "projection_id": _projection_id(row.session_id),
                    "owner_email": row.user_email,
                    "session_id": row.session_id,
                    "source_session_id": source_session_id,
                    "materializer_version": WORK_MATERIALIZER_VERSION,
                    "target_seq": target_seq,
                },
            )
            await db.flush()
            state = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == row.session_id,
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                )
            )
            if state is None:
                raise RuntimeError("Work projection state creation failed")
        state.target_seq = max(state.target_seq, target_seq)
        state.source_session_id = source_session_id
        return state

    async def _materialize_batch(
        self,
        db: AsyncSession,
        *,
        row: Session,
        state: WorkSessionProjectionRow,
        raw_events: Sequence[RawSessionEvent],
        target_seq: int,
    ) -> None:
        if not raw_events:
            state.target_seq = max(state.target_seq, target_seq)
            state.state = (
                "caught_up" if state.covered_through_seq >= state.target_seq else "materializing"
            )
            return
        expected = state.covered_through_seq + 1
        if raw_events[0].seq != expected:
            raise ValueError(
                f"Work materialization gap: expected {expected}, got {raw_events[0].seq}"
            )
        timeline = project_timeline(normalize_session_events(raw_events).events).timeline
        definitions = self._tool_definitions()
        for ordinal, item in enumerate(timeline.items):
            if not isinstance(item, ToolCallTimelineItem) and not is_work_evidence_item(
                item, definitions
            ):
                continue
            source_seq = max((ref.seq for ref in item.source_refs), default=raw_events[0].seq)
            existing = None
            call_id = item.call_id if isinstance(item, ToolCallTimelineItem) else None
            if call_id:
                existing = await db.scalar(
                    select(WorkRecordRow)
                    .where(
                        WorkRecordRow.owner_email == row.user_email,
                        WorkRecordRow.session_id == row.session_id,
                        WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
                        WorkRecordRow.call_id == call_id,
                    )
                    .order_by(WorkRecordRow.source_seq.desc())
                    .limit(1)
                )
            if existing is not None and isinstance(item, ToolCallTimelineItem):
                previous = _TIMELINE_ADAPTER.validate_python(existing.timeline_item)
                if isinstance(previous, ToolCallTimelineItem):
                    if not previous.file_diffs:
                        previous_files = (
                            await db.scalars(
                                select(WorkRecordFileRow)
                                .where(WorkRecordFileRow.work_record_id == existing.work_record_id)
                                .order_by(WorkRecordFileRow.file_ordinal)
                            )
                        ).all()
                        previous = previous.model_copy(
                            update={
                                "file_diffs": [
                                    FileDiffRef(
                                        path=file.path,
                                        path_id=file.path_id,
                                        diff="",
                                        additions=file.additions,
                                        deletions=file.deletions,
                                        content_truncated=True,
                                    )
                                    for file in previous_files
                                ]
                            }
                        )
                    item = item.model_copy(
                        update={
                            "sort_key": previous.sort_key,
                            "created_at": previous.created_at,
                            "tool_name": previous.tool_name
                            if item.tool_name == "tool"
                            else item.tool_name,
                            "arguments": item.arguments or previous.arguments,
                            "arguments_preview": item.arguments_preview
                            or previous.arguments_preview,
                            "result_preview": item.result_preview or previous.result_preview,
                            "status": _merged_tool_status(previous.status, item.status),
                            "updated_at": previous.updated_at or item.updated_at,
                            "streamed_output": item.streamed_output or previous.streamed_output,
                            "file_diffs": item.file_diffs or previous.file_diffs,
                            "attachments": item.attachments or previous.attachments,
                            "is_error": item.is_error or previous.is_error,
                            "duration_ms": item.duration_ms or previous.duration_ms,
                            "output_size": item.output_size or previous.output_size,
                            "truncated": item.truncated or previous.truncated,
                            "has_full_output": item.has_full_output or previous.has_full_output,
                            "recovery_call_id": item.recovery_call_id or previous.recovery_call_id,
                            "tool_output_artifact_id": item.tool_output_artifact_id
                            or previous.tool_output_artifact_id,
                        }
                    )
            evidence = is_work_evidence_item(item, definitions)
            work_record_id = _record_id(
                row.user_email, state.source_session_id, source_seq, ordinal
            )
            record = await db.get(WorkRecordRow, work_record_id)
            if existing is not None and existing.work_record_id != work_record_id:
                existing.timeline_item = _bounded_item(item)
                existing.is_evidence = False
                existing.materialized_at = datetime.now(UTC)
            values = {
                "source_event_id": next(
                    (event.event_id for event in raw_events if event.seq == source_seq), None
                ),
                "source_item_id": item.id,
                "occurred_at": next(
                    (
                        event.timestamp
                        for event in raw_events
                        if event.seq == source_seq and event.timestamp is not None
                    ),
                    datetime.now(UTC),
                ),
                "record_type": item.kind,
                "is_evidence": evidence,
                "pairing_key": call_id,
                "call_id": call_id,
                "timeline_item": _bounded_item(item),
                "materialized_at": datetime.now(UTC),
                **_record_metadata(item, definitions),
            }
            if record is None:
                db.add(
                    WorkRecordRow(
                        work_record_id=work_record_id,
                        owner_email=row.user_email,
                        session_id=row.session_id,
                        materializer_version=WORK_MATERIALIZER_VERSION,
                        source_store="intaris",
                        source_session_id=state.source_session_id,
                        source_seq=source_seq,
                        item_ordinal=ordinal,
                        **values,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            await db.flush()
            await db.execute(
                delete(WorkRecordFileRow).where(WorkRecordFileRow.work_record_id == work_record_id)
            )
            db.add_all(_record_file_rows(work_record_id=work_record_id, item=item))
        state.covered_through_seq = raw_events[-1].seq
        state.target_seq = max(state.target_seq, target_seq)
        state.state = (
            "caught_up" if state.covered_through_seq >= state.target_seq else "materializing"
        )
        state.last_error = None
        state.retry_count = 0
        state.materialized_at = datetime.now(UTC)

    async def _mark_repair(self, session_id: str, target_seq: int, error: str) -> None:
        async with self._session_factory() as db:
            state = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == session_id,
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                )
            )
            if state is not None:
                state.target_seq = max(state.target_seq, target_seq)
                state.state = "repair"
                state.last_error = error[:2000]
                await db.commit()
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                states = await self._claim()
                if states:
                    await asyncio.gather(*(self._repair(state.projection_id) for state in states))
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Work materializer loop failed; retrying", exc_info=True)
            if self._wake.is_set():
                continue
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2.0):
                    await self._wake.wait()

    async def _lock_claim_budget(self, db: AsyncSession) -> None:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('cognis-work-materializer-claim'))")
            )

    async def _available_slots(self, db: AsyncSession, now: datetime) -> int:
        active = int(
            await db.scalar(
                select(func.count())
                .select_from(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                    WorkSessionProjectionRow.lease_owner.is_not(None),
                    WorkSessionProjectionRow.lease_expires_at >= now,
                )
            )
            or 0
        )
        return max(0, WORK_REPAIR_CONCURRENCY - active)

    async def _claim(self) -> list[WorkSessionProjectionRow]:
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            await self._lock_claim_budget(db)
            available = await self._available_slots(db, now)
            if available == 0:
                return []
            rows = (
                await db.scalars(
                    select(WorkSessionProjectionRow)
                    .where(
                        WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                        WorkSessionProjectionRow.state.in_(
                            ["pending", "materializing", "repair", "failed"]
                        ),
                        or_(
                            WorkSessionProjectionRow.next_retry_at.is_(None),
                            WorkSessionProjectionRow.next_retry_at <= now,
                        ),
                        or_(
                            WorkSessionProjectionRow.lease_expires_at.is_(None),
                            WorkSessionProjectionRow.lease_expires_at < now,
                        ),
                    )
                    .order_by(
                        WorkSessionProjectionRow.priority.desc(),
                        WorkSessionProjectionRow.updated_at,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(available)
                )
            ).all()
            for row in rows:
                row.state = "materializing"
                row.lease_owner = self._worker_id
                row.lease_fence += 1
                row.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
                row.priority = 0
            await db.commit()
            return list(rows)

    async def _repair(self, projection_id: str) -> None:
        async with self._repair_slots:
            fence: int | None = None
            try:
                async with self._session_factory() as db:
                    state = await db.get(WorkSessionProjectionRow, projection_id)
                    if state is None or state.lease_owner != self._worker_id:
                        return
                    row = await db.get(Session, state.session_id)
                    if row is None:
                        return
                    fence = state.lease_fence
                    source_session_id = state.source_session_id
                    covered_through_seq = state.covered_through_seq
                reader = await self._reader_for(row)
                page = await self._run_event_read(
                    lambda: reader.read_session_events(
                        session_id=source_session_id,
                        after_seq=covered_through_seq,
                        limit=WORK_REPAIR_PAGE_SIZE,
                        direction="forward",
                    )
                )
                async with self._session_factory() as db:
                    state = await db.scalar(
                        select(WorkSessionProjectionRow)
                        .where(
                            WorkSessionProjectionRow.projection_id == projection_id,
                            WorkSessionProjectionRow.lease_owner == self._worker_id,
                            WorkSessionProjectionRow.lease_fence == fence,
                        )
                        .with_for_update()
                    )
                    if state is None:
                        return
                    row = await db.get(Session, state.session_id)
                    if row is None:
                        return
                    target = max(state.target_seq, page.last_seq or 0)
                    await self._materialize_batch(
                        db, row=row, state=state, raw_events=page.events, target_seq=target
                    )
                    incomplete = page.has_more_after or state.covered_through_seq < state.target_seq
                    state.state = "repair" if incomplete else "caught_up"
                    state.lease_owner = None
                    state.lease_expires_at = None
                    await db.commit()
                    if incomplete:
                        self._wake.set()
                    else:
                        self._notify_projection_caught_up(row.conversation_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Work projection repair failed", exc_info=True)
                async with self._session_factory() as db:
                    state = await db.scalar(
                        select(WorkSessionProjectionRow).where(
                            WorkSessionProjectionRow.projection_id == projection_id,
                            WorkSessionProjectionRow.lease_owner == self._worker_id,
                            WorkSessionProjectionRow.lease_fence == fence,
                        )
                    )
                    if state is not None and fence is not None:
                        state.retry_count += 1
                        state.state = "failed" if state.retry_count >= 5 else "repair"
                        state.last_error = str(exc)[:2000]
                        state.next_retry_at = datetime.now(UTC) + timedelta(
                            seconds=min(60, 2**state.retry_count)
                        )
                        state.lease_owner = None
                        state.lease_expires_at = None
                        await db.commit()

    async def _run_event_read(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self._event_read_admission is None:
            return await operation()
        return await self._event_read_admission.run(operation)

    def _notify_projection_caught_up(self, conversation_id: str) -> None:
        if self._on_projection_caught_up is None:
            return
        try:
            self._on_projection_caught_up(conversation_id)
        except Exception:
            logger.warning(
                "Work projection post-commit callback failed",
                exc_info=True,
            )

    async def _reader_for(self, row: Session) -> SessionEventStore:
        agent_owner_email = await self._agent_owner_email(row)
        authority = EventStoreAuthority(
            user_email=row.user_email,
            agent_id=row.agent_id,
            agent_owner_email=agent_owner_email,
        )
        bind = getattr(self._event_store, "bind", None)
        if not callable(bind):
            raise RuntimeError("Work repair event store must support authority binding")
        return cast(SessionEventStore, bind(authority))

    async def _agent_owner_email(self, row: Session) -> str:
        async with self._session_factory() as db:
            owner = await db.scalar(select(Agent.owner_email).where(Agent.agent_id == row.agent_id))
        if not isinstance(owner, str) or not owner:
            raise RuntimeError("Work repair agent authority is unavailable")
        return owner


__all__ = [
    "WORK_MATERIALIZER_VERSION",
    "WORK_RECORD_MAX_BYTES",
    "WORK_REPAIR_CONCURRENCY",
    "WorkMaterializer",
]

"""Durable PostgreSQL materialization for the Work read model."""

from __future__ import annotations

import asyncio
import contextlib
import copy
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
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.api.chat_v2.background_event_reads import BackgroundEventReadAdmission
from cognis.api.chat_v2.event_store import (
    RawSessionEvent,
    SessionEventStore,
    SessionHistoryUnavailableError,
    require_session_history,
)
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    AssistantDeliverableTimelineItem,
    FileDiffRef,
    TimelineItem,
    ToolCallTimelineItem,
)
from cognis.api.chat_v2.work_file_projector import (
    rebuild_session_current_files,
    scrub_expired_current_file_content,
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

WORK_MATERIALIZER_VERSION = "work-v8"
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
WORK_REPAIR_IDLE_CHECK_SECONDS = 30.0
WORK_RETENTION_INITIAL_DELAY_SECONDS = 1.0
WORK_RETENTION_INTERVAL_SECONDS = 24 * 60 * 60
WORK_VISIBLE_REFRESH_PRIORITY = 10_000
WORK_LIVE_APPEND_PRIORITY = 20_000
WORK_URGENT_REPAIR_CONCURRENCY = 1
WORK_MAX_REPAIR_CONCURRENCY = WORK_REPAIR_CONCURRENCY + WORK_URGENT_REPAIR_CONCURRENCY
_URGENT_LEASE_OWNER_PREFIX = "work-urgent:"
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


def _has_unexpired_projection_lease(
    state: WorkSessionProjectionRow,
    *,
    now: datetime,
) -> bool:
    expires_at = state.lease_expires_at
    if state.lease_owner is None or expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at >= now


def _advance_projection_target(
    state: WorkSessionProjectionRow,
    target_seq: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Advance one target while preserving the projection-state invariant."""

    state.target_seq = max(state.target_seq, target_seq)
    if state.covered_through_seq >= state.target_seq:
        return False
    current = now or datetime.now(UTC)
    state.state = (
        "materializing" if _has_unexpired_projection_lease(state, now=current) else "repair"
    )
    state.next_retry_at = None
    return True


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


@dataclass(frozen=True, slots=True)
class _UrgentWorkClaim:
    projection_id: str
    session_id: str
    priority: int
    lease_fence: int


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


def _bounded_item(item: TimelineItem, *, persist_source_preview: bool = False) -> dict[str, Any]:
    payload = item.model_dump(mode="json")
    if isinstance(item, ToolCallTimelineItem) and not persist_source_preview:
        for diff in payload.get("file_diffs", []):
            diff["diff"] = ""
            diff["preview_omitted"] = True
            diff["preview_omission_reason"] = "not_persisted"
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


async def scrub_persisted_source_previews(
    db: AsyncSession, *, materializer_version: str, now: datetime, limit: int = 500
) -> int:
    """Remove source diff bodies from bounded current-version rows."""

    rows = list(
        (
            await db.scalars(
                select(WorkRecordRow)
                .where(
                    WorkRecordRow.materializer_version == materializer_version,
                    or_(
                        WorkRecordRow.source_content_expires_at.is_(None),
                        WorkRecordRow.source_content_expires_at <= now,
                    ),
                    WorkRecordRow.source_content_scrubbed_at.is_(None),
                )
                .order_by(WorkRecordRow.materialized_at, WorkRecordRow.work_record_id)
                .limit(limit)
            )
        ).all()
    )
    for row in rows:
        payload = copy.deepcopy(row.timeline_item)
        file_diffs = payload.get("file_diffs")
        if not isinstance(file_diffs, list):
            continue
        scrubbed = False
        for diff in file_diffs:
            if isinstance(diff, dict) and diff.get("diff"):
                diff["diff"] = ""
                diff["preview_omitted"] = True
                diff["preview_omission_reason"] = "retention_expired"
                scrubbed = True
        if scrubbed:
            row.timeline_item = payload
        row.source_content_scrubbed_at = now
    return len(rows)


def _decode_persisted_work_item(payload: Mapping[str, Any]) -> TimelineItem:
    """Decode a stored Work item while accepting the retired scope field."""

    normalized = dict(payload)
    normalized.pop("activity_scope_id", None)
    return _TIMELINE_ADAPTER.validate_python(normalized)


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


def _old_file_path_id(diff: FileDiffRef, *, workdir: str | None) -> str | None:
    if not diff.old_path:
        return None
    normalized_old = posixpath.normpath(diff.old_path.replace("\\", "/"))
    if normalized_old.startswith("/") or (
        len(normalized_old) >= 3 and normalized_old[1] == ":" and normalized_old[2] == "/"
    ):
        normalized_workdir = posixpath.normpath((workdir or "").replace("\\", "/")).rstrip("/")
        windows_path = len(normalized_old) >= 2 and normalized_old[1] == ":"
        comparable_old = normalized_old.lower() if windows_path else normalized_old
        comparable_workdir = normalized_workdir.lower() if windows_path else normalized_workdir
        if comparable_workdir and (
            comparable_old == comparable_workdir
            or comparable_old.startswith(f"{comparable_workdir}/")
        ):
            relative = normalized_old[len(normalized_workdir) :].removeprefix("/")
            if diff.root_id:
                return f"{diff.root_id}:{relative}"
        return _file_path_id(
            FileDiffRef(path=diff.old_path, diff=""),
            workdir=workdir,
        )
    old = FileDiffRef(
        path=diff.old_path,
        diff="",
        root_id=diff.root_id,
        root_label=diff.root_label,
        relative_path=diff.old_path if diff.root_id else None,
    )
    return _file_path_id(old, workdir=workdir)


def _record_file_rows(
    *,
    work_record_id: str,
    owner_email: str,
    session_id: str,
    source_seq: int,
    item_ordinal: int,
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
                owner_email=owner_email,
                session_id=session_id,
                materializer_version=WORK_MATERIALIZER_VERSION,
                work_record_id=work_record_id,
                file_ordinal=ordinal,
                path=diff.path,
                path_id=_file_path_id(diff, workdir=workdir),
                path_generation_id="",
                source_seq=source_seq,
                item_ordinal=item_ordinal,
                additions=additions,
                deletions=deletions,
                status=diff.status,
                old_path=diff.old_path,
                old_path_id=_old_file_path_id(diff, workdir=workdir),
                binary=diff.binary,
                generated=diff.generated,
                truncated=diff.truncated,
                preview_omitted=diff.preview_omitted,
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


def _published_artifact_item(item: TimelineItem) -> TimelineItem:
    """Project a successful artifact_publish result as first-class Work evidence."""

    if (
        not isinstance(item, ToolCallTimelineItem)
        or item.tool_name != "artifact_publish"
        or item.status != "complete"
        or item.is_error
        or len(item.attachments) != 1
    ):
        return item
    attachment = item.attachments[0]
    return ArtifactTimelineItem(
        id=f"artifact:{attachment.artifact_id}",
        sort_key=item.sort_key,
        source_refs=item.source_refs,
        created_at=item.created_at,
        updated_at=item.updated_at,
        artifact_id=attachment.artifact_id,
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        title=item.display_name,
    )


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
        source_preview_max_lifetime_seconds: int = 0,
        repair_idle_check_seconds: float = WORK_REPAIR_IDLE_CHECK_SECONDS,
        retention_initial_delay_seconds: float = WORK_RETENTION_INITIAL_DELAY_SECONDS,
        retention_interval_seconds: float = WORK_RETENTION_INTERVAL_SECONDS,
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
        if repair_idle_check_seconds <= 0:
            raise ValueError("repair_idle_check_seconds must be positive")
        if retention_initial_delay_seconds < 0:
            raise ValueError("retention_initial_delay_seconds must not be negative")
        if retention_interval_seconds <= 0:
            raise ValueError("retention_interval_seconds must be positive")
        self._session_factory = session_factory
        self._event_store = event_store
        self._tool_definitions = tool_definitions
        self._worker_id = worker_id or str(uuid.uuid4())
        if source_preview_max_lifetime_seconds < 0:
            raise ValueError("source_preview_max_lifetime_seconds must not be negative")
        self._source_preview_max_lifetime_seconds = source_preview_max_lifetime_seconds
        self._event_read_admission = event_read_admission
        self._on_projection_caught_up = on_projection_caught_up
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._repair_idle_check_seconds = repair_idle_check_seconds
        self._retention_initial_delay_seconds = retention_initial_delay_seconds
        self._retention_interval_seconds = retention_interval_seconds
        self._repair_slots = asyncio.Semaphore(WORK_REPAIR_CONCURRENCY)
        self._urgent_repair_slot = asyncio.Semaphore(WORK_URGENT_REPAIR_CONCURRENCY)
        self._urgent_event_read_slot = asyncio.Semaphore(WORK_URGENT_REPAIR_CONCURRENCY)
        self._urgent_worker_id = f"{_URGENT_LEASE_OWNER_PREFIX}{self._worker_id}"
        self._urgent_pending: OrderedDict[str, None] = OrderedDict()
        self._urgent_active_claim: _UrgentWorkClaim | None = None
        self._urgent_task: asyncio.Task[None] | None = None
        self._urgent_accepting = False
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
        self._paused_background_projection_ids: set[str] = set()
        self._background_cycle_lock = asyncio.Lock()
        self._update_append_gauges()

    def start(self) -> None:
        self._urgent_accepting = True
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="work-materializer")
        if self._retention_task is None:
            self._retention_task = asyncio.create_task(
                self._run_retention(),
                name="work-source-retention",
            )
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

    def wake(self) -> None:
        """Wake the durable repair and topology worker."""

        self._wake.set()

    async def pause_background_repair(self, projection_id: str) -> None:
        """Pause one projection after its active normal claim batch completes."""

        self._paused_background_projection_ids.add(projection_id)
        self._wake.set()
        try:
            async with self._background_cycle_lock:
                pass
        except BaseException:
            self._paused_background_projection_ids.discard(projection_id)
            raise

    def resume_background_repair(self, projection_id: str) -> None:
        """Resume normal claims for one projection."""

        self._paused_background_projection_ids.discard(projection_id)
        self._wake.set()

    async def stop(self, *, timeout_seconds: float = 2.0) -> None:
        urgent_claim_to_release = self._urgent_active_claim
        self._urgent_accepting = False
        self._append_accepting = False
        self._append_stopping = True
        self._append_available.set()
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
        if self._retention_task is not None:
            self._retention_task.cancel()
        if self._urgent_task is not None:
            self._urgent_task.cancel()
        tasks = tuple(
            task
            for task in (
                self._task,
                self._retention_task,
                self._urgent_task,
                *self._append_workers,
            )
            if task is not None
        )
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
        self._retention_task = None
        self._urgent_task = None
        if urgent_claim_to_release is not None:
            await self._release_urgent_lease(urgent_claim_to_release)
        self._urgent_pending.clear()
        self._urgent_active_claim = None
        self._paused_background_projection_ids.clear()
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
            now = datetime.now(UTC)
            if _has_unexpired_projection_lease(state, now=now):
                _advance_projection_target(state, item.target_seq, now=now)
                await db.commit()
                self._wake.set()
                return
            state.lease_owner = f"{self._worker_id}:append"
            state.lease_fence += 1
            state.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
            if item.first_seq != state.covered_through_seq + 1:
                _advance_projection_target(state, item.target_seq, now=now)
                state.state = "repair"
                state.priority = max(state.priority, WORK_LIVE_APPEND_PRIORITY)
                state.last_error = "noncontiguous live append"
                state.lease_owner = None
                state.lease_expires_at = None
                await db.commit()
                self._schedule_urgent_claim([state.projection_id])
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
                state.lease_owner = None
                state.lease_expires_at = None
                await db.commit()
                caught_up = state.state == "caught_up"
                row_conversation_id = row.conversation_id
            except Exception as exc:
                await db.rollback()
                await self._mark_repair(row.session_id, item.target_seq, str(exc))
                raise
        if caught_up:
            self._notify_projection_caught_up(row_conversation_id)

    async def _mark_topology_retry(
        self,
        *,
        session_id: str,
        target_seq: int,
        error: str,
    ) -> None:
        """Persist a restart-safe topology retry after projection commit."""

        async with self._session_factory() as db:
            state = await db.scalar(
                select(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.session_id == session_id,
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                )
                .with_for_update()
            )
            if state is None:
                return
            now = datetime.now(UTC)
            _advance_projection_target(state, target_seq, now=now)
            if _has_unexpired_projection_lease(state, now=now):
                await db.commit()
                return
            state.state = "repair"
            state.priority = max(state.priority, 100)
            state.next_retry_at = None
            state.last_error = f"topology refresh pending: {error}"[:2000]
            state.lease_owner = None
            state.lease_expires_at = None
            await db.commit()

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
        now = datetime.now(UTC)
        urgent_projection_ids: list[str] = []
        async with self._session_factory() as db:
            for row in rows:
                state = await self._ensure_state(db, row, 0)
                state.priority = max(state.priority, WORK_VISIBLE_REFRESH_PRIORITY)
                state.next_retry_at = None
                state.next_head_check_at = None
                lease_unexpired = _has_unexpired_projection_lease(state, now=now)
                if not lease_unexpired and state.state in {
                    "caught_up",
                    "failed",
                    "repair",
                    "materializing",
                }:
                    state.state = "repair"
                if not lease_unexpired:
                    urgent_projection_ids.append(state.projection_id)
            await db.commit()
        self._schedule_urgent_claim(urgent_projection_ids)
        self._wake.set()

    def _schedule_urgent_claim(self, projection_ids: Sequence[str]) -> None:
        if not self._urgent_accepting or not projection_ids:
            return
        active_projection_id = (
            self._urgent_active_claim.projection_id
            if self._urgent_active_claim is not None
            else None
        )
        for projection_id in projection_ids:
            if projection_id != active_projection_id:
                self._urgent_pending.setdefault(projection_id, None)
        if self._urgent_task is None or self._urgent_task.done():
            self._urgent_task = asyncio.create_task(
                self._run_urgent_claims(),
                name="work-urgent-materializer",
            )

    async def _run_urgent_claims(self) -> None:
        retry_delay = 0.1
        try:
            while self._urgent_accepting and self._urgent_pending:
                projection_id = next(iter(self._urgent_pending))
                state = await self._claim_urgent(projection_id)
                if state is None:
                    still_urgent = await self._still_urgent_projection_ids([projection_id])
                    if projection_id not in still_urgent:
                        self._urgent_pending.pop(projection_id, None)
                    if not self._urgent_pending:
                        return
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(1.0, retry_delay * 2)
                    continue
                retry_delay = 0.1
                self._urgent_pending.pop(state.projection_id, None)
                self._urgent_active_claim = state
                try:
                    await self._repair(
                        state.projection_id,
                        lease_owner=self._urgent_worker_id,
                        repair_slot=self._urgent_repair_slot,
                    )
                    if await self._continue_urgent_claim(state):
                        self._urgent_pending.setdefault(state.projection_id, None)
                finally:
                    self._urgent_active_claim = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Urgent Work repair failed; retrying normally", exc_info=True)
            self._wake.set()
        finally:
            if self._urgent_task is asyncio.current_task():
                self._urgent_task = None

    async def _still_urgent_projection_ids(
        self,
        projection_ids: Sequence[str],
    ) -> set[str]:
        if not projection_ids:
            return set()
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            return set(
                (
                    await db.scalars(
                        select(WorkSessionProjectionRow.projection_id).where(
                            WorkSessionProjectionRow.projection_id.in_(projection_ids),
                            WorkSessionProjectionRow.materializer_version
                            == WORK_MATERIALIZER_VERSION,
                            WorkSessionProjectionRow.priority >= WORK_VISIBLE_REFRESH_PRIORITY,
                            WorkSessionProjectionRow.state.in_(
                                ["pending", "materializing", "repair"]
                            ),
                            or_(
                                WorkSessionProjectionRow.next_retry_at.is_(None),
                                WorkSessionProjectionRow.next_retry_at <= now,
                            ),
                        )
                    )
                ).all()
            )

    async def _continue_urgent_claim(self, claim: _UrgentWorkClaim) -> bool:
        async with self._session_factory() as db:
            state = await db.scalar(
                select(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.projection_id == claim.projection_id,
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                    WorkSessionProjectionRow.lease_owner.is_(None),
                    WorkSessionProjectionRow.lease_fence == claim.lease_fence,
                )
                .with_for_update()
            )
            if state is None:
                return False
            if state.state == "repair" and state.next_retry_at is None:
                state.priority = max(state.priority, claim.priority)
                await db.commit()
                return True
            if state.state == "caught_up" and state.priority >= WORK_VISIBLE_REFRESH_PRIORITY:
                state.state = "repair"
                state.next_retry_at = None
                await db.commit()
                return True
            if state.priority:
                state.priority = 0
                await db.commit()
            return False

    async def _release_urgent_lease(self, claim: _UrgentWorkClaim) -> None:
        async with self._session_factory() as db:
            state = await db.scalar(
                select(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.projection_id == claim.projection_id,
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                    WorkSessionProjectionRow.lease_owner == self._urgent_worker_id,
                    WorkSessionProjectionRow.lease_fence == claim.lease_fence,
                )
                .with_for_update()
            )
            if state is None:
                return
            state.state = "repair"
            state.priority = max(state.priority, claim.priority)
            state.next_retry_at = None
            state.lease_owner = None
            state.lease_expires_at = None
            await db.commit()

    async def mark_source_pending(
        self, *, owner_email: str, source_session_id: str, target_seq: int
    ) -> bool:
        urgent_projection_ids: list[str] = []
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
            state.priority = max(state.priority, WORK_LIVE_APPEND_PRIORITY)
            state.next_retry_at = None
            if not _has_unexpired_projection_lease(state, now=datetime.now(UTC)):
                urgent_projection_ids.append(state.projection_id)
            await db.commit()
        self._schedule_urgent_claim(urgent_projection_ids)
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
        _advance_projection_target(state, target_seq)
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
            _advance_projection_target(state, target_seq)
            state.state = (
                "caught_up" if state.covered_through_seq >= state.target_seq else "materializing"
            )
            if state.state == "caught_up":
                state.next_head_check_at = None
            return
        expected = state.covered_through_seq + 1
        if raw_events[0].seq != expected:
            raise ValueError(
                f"Work materialization gap: expected {expected}, got {raw_events[0].seq}"
            )
        timeline = project_timeline(normalize_session_events(raw_events).events).timeline
        definitions = self._tool_definitions()
        file_facts_changed = False
        for ordinal, item in enumerate(timeline.items):
            if not isinstance(item, ToolCallTimelineItem) and not is_work_evidence_item(
                item, definitions
            ):
                continue
            source_seq = max((ref.seq for ref in item.source_refs), default=raw_events[0].seq)
            existing = None
            previous_item: TimelineItem | None = None
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
                previous_item = _decode_persisted_work_item(existing.timeline_item)
                if isinstance(previous_item, ToolCallTimelineItem):
                    if not previous_item.file_diffs:
                        previous_files = (
                            await db.scalars(
                                select(WorkRecordFileRow)
                                .where(WorkRecordFileRow.work_record_id == existing.work_record_id)
                                .order_by(WorkRecordFileRow.file_ordinal)
                            )
                        ).all()
                        previous_item = previous_item.model_copy(
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
                            "sort_key": previous_item.sort_key,
                            "created_at": previous_item.created_at,
                            "tool_name": previous_item.tool_name
                            if item.tool_name == "tool"
                            else item.tool_name,
                            "arguments": item.arguments or previous_item.arguments,
                            "arguments_preview": item.arguments_preview
                            or previous_item.arguments_preview,
                            "result_preview": item.result_preview or previous_item.result_preview,
                            "status": _merged_tool_status(previous_item.status, item.status),
                            "updated_at": previous_item.updated_at or item.updated_at,
                            "streamed_output": item.streamed_output
                            or previous_item.streamed_output,
                            "file_diffs": item.file_diffs or previous_item.file_diffs,
                            "attachments": item.attachments or previous_item.attachments,
                            "is_error": item.is_error or previous_item.is_error,
                            "duration_ms": item.duration_ms or previous_item.duration_ms,
                            "output_size": item.output_size or previous_item.output_size,
                            "truncated": item.truncated or previous_item.truncated,
                            "has_full_output": item.has_full_output
                            or previous_item.has_full_output,
                            "recovery_call_id": item.recovery_call_id
                            or previous_item.recovery_call_id,
                            "tool_output_artifact_id": item.tool_output_artifact_id
                            or previous_item.tool_output_artifact_id,
                        }
                    )
            item = _published_artifact_item(item)
            preserve_existing_artifact = (
                isinstance(previous_item, ArtifactTimelineItem)
                and isinstance(item, ToolCallTimelineItem)
                and item.tool_name == "artifact_publish"
            )
            evidence = is_work_evidence_item(item, definitions)
            work_record_id = _record_id(
                row.user_email, state.source_session_id, source_seq, ordinal
            )
            record = await db.get(WorkRecordRow, work_record_id)
            if (
                existing is not None
                and existing.work_record_id != work_record_id
                and not preserve_existing_artifact
            ):
                existing.timeline_item = _bounded_item(
                    item,
                    persist_source_preview=self._source_preview_max_lifetime_seconds > 0,
                )
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
                "timeline_item": _bounded_item(
                    item,
                    persist_source_preview=self._source_preview_max_lifetime_seconds > 0,
                ),
                "source_content_expires_at": (
                    datetime.now(UTC) + timedelta(seconds=self._source_preview_max_lifetime_seconds)
                    if self._source_preview_max_lifetime_seconds > 0
                    else None
                ),
                "source_content_scrubbed_at": (
                    None if self._source_preview_max_lifetime_seconds > 0 else datetime.now(UTC)
                ),
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
            file_rows = _record_file_rows(
                work_record_id=work_record_id,
                owner_email=row.user_email,
                session_id=row.session_id,
                source_seq=source_seq,
                item_ordinal=ordinal,
                item=item,
            )
            file_facts_changed = file_facts_changed or bool(file_rows)
            db.add_all(file_rows)
        await db.flush()
        if file_facts_changed:
            state.file_count = await rebuild_session_current_files(
                db,
                owner_email=row.user_email,
                session_id=row.session_id,
                materializer_version=WORK_MATERIALIZER_VERSION,
            )
        state.covered_through_seq = raw_events[-1].seq
        _advance_projection_target(state, target_seq)
        state.state = (
            "caught_up" if state.covered_through_seq >= state.target_seq else "materializing"
        )
        if state.state == "caught_up":
            state.next_head_check_at = None
        state.last_error = None
        state.retry_count = 0
        state.materialized_at = datetime.now(UTC)
        await self._refresh_projection_counters(db, state=state)

    async def _refresh_projection_counters(
        self,
        db: AsyncSession,
        *,
        state: WorkSessionProjectionRow,
    ) -> None:
        base = (
            WorkRecordRow.owner_email == state.owner_email,
            WorkRecordRow.session_id == state.session_id,
            WorkRecordRow.materializer_version == state.materializer_version,
        )
        evidence = WorkRecordRow.is_evidence.is_(True)
        counters = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(evidence),
                    func.count().filter(evidence, WorkRecordRow.category == "mutations"),
                    func.count().filter(evidence, WorkRecordRow.category == "commands"),
                    func.count().filter(evidence, WorkRecordRow.category == "artifacts"),
                    func.count().filter(evidence, WorkRecordRow.category == "deliverables"),
                    func.coalesce(func.sum(WorkRecordRow.additions).filter(evidence), 0),
                    func.coalesce(func.sum(WorkRecordRow.deletions).filter(evidence), 0),
                ).where(*base)
            )
        ).one()
        state.record_count = int(counters[0])
        state.evidence_record_count = int(counters[1])
        state.mutation_count = int(counters[2])
        state.command_count = int(counters[3])
        state.artifact_count = int(counters[4])
        state.deliverable_count = int(counters[5])
        state.additions = int(counters[6])
        state.deletions = int(counters[7])
        state.expected_record_count = state.record_count
        state.expected_record_file_count = int(
            await db.scalar(
                select(func.count())
                .select_from(WorkRecordFileRow)
                .where(
                    WorkRecordFileRow.owner_email == state.owner_email,
                    WorkRecordFileRow.session_id == state.session_id,
                    WorkRecordFileRow.materializer_version == state.materializer_version,
                )
            )
            or 0
        )

    async def _mark_repair(self, session_id: str, target_seq: int, error: str) -> None:
        async with self._session_factory() as db:
            state = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == session_id,
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                )
            )
            if state is not None:
                _advance_projection_target(state, target_seq)
                state.state = "repair"
                state.last_error = error[:2000]
                await db.commit()
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                async with self._background_cycle_lock:
                    states = await self._claim()
                    if states:
                        await asyncio.gather(
                            *(self._repair(state.projection_id) for state in states)
                        )
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Work materializer loop failed; retrying", exc_info=True)
            if self._wake.is_set():
                continue
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._repair_idle_check_seconds):
                    await self._wake.wait()

    async def _run_retention(self) -> None:
        delay = self._retention_initial_delay_seconds
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(delay):
                    await self._stop.wait()
            if self._stop.is_set():
                return
            try:
                await self._scrub_source_content()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Work source retention failed; retrying tomorrow", exc_info=True)
            delay = self._retention_interval_seconds

    async def _scrub_source_content(self) -> int:
        """Scrub expired source content once per day on one controller."""

        now = datetime.now(UTC)
        bind = self._session_factory.kw.get("bind")
        if bind is None:
            raise RuntimeError("Work retention requires a bound session factory")
        async with (
            bind.connect() as connection,
            AsyncSession(bind=connection, expire_on_commit=False) as db,
        ):
            postgres = connection.dialect.name == "postgresql"
            if postgres:
                acquired = bool(
                    await db.scalar(
                        text(
                            "SELECT pg_try_advisory_lock(hashtext('cognis-work-source-retention'))"
                        )
                    )
                )
                if not acquired:
                    return 0
            try:
                changed = 0
                while True:
                    batch = await scrub_persisted_source_previews(
                        db,
                        materializer_version=WORK_MATERIALIZER_VERSION,
                        now=now,
                    )
                    batch += await scrub_expired_current_file_content(db, now=now)
                    changed += batch
                    await db.commit()
                    if batch == 0:
                        return changed
            except BaseException:
                await db.rollback()
                raise
            finally:
                if postgres:
                    await db.execute(
                        text("SELECT pg_advisory_unlock(hashtext('cognis-work-source-retention'))")
                    )
                    await db.commit()

    async def _lock_claim_budget(self, db: AsyncSession) -> None:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('cognis-work-materializer-claim'))")
            )

    async def _active_lease_counts(
        self,
        db: AsyncSession,
        now: datetime,
    ) -> tuple[int, int]:
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
        urgent = int(
            await db.scalar(
                select(func.count())
                .select_from(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                    WorkSessionProjectionRow.lease_owner.like(f"{_URGENT_LEASE_OWNER_PREFIX}%"),
                    WorkSessionProjectionRow.lease_expires_at >= now,
                )
            )
            or 0
        )
        return active, urgent

    async def _available_slots(self, db: AsyncSession, now: datetime) -> int:
        active, urgent = await self._active_lease_counts(db, now)
        normal = active - urgent
        return max(
            0,
            min(
                WORK_REPAIR_CONCURRENCY - normal,
                WORK_MAX_REPAIR_CONCURRENCY - active,
            ),
        )

    async def _claim(self) -> list[WorkSessionProjectionRow]:
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            await self._lock_claim_budget(db)
            available = await self._available_slots(db, now)
            if available == 0:
                return []
            repair_due = and_(
                WorkSessionProjectionRow.state.in_(["pending", "materializing", "repair"]),
                or_(
                    WorkSessionProjectionRow.next_retry_at.is_(None),
                    WorkSessionProjectionRow.next_retry_at <= now,
                ),
            )
            failed_retry_due = and_(
                WorkSessionProjectionRow.state == "failed",
                WorkSessionProjectionRow.next_retry_at.is_not(None),
                WorkSessionProjectionRow.next_retry_at <= now,
            )
            inconsistent_caught_up = and_(
                WorkSessionProjectionRow.state == "caught_up",
                WorkSessionProjectionRow.covered_through_seq < WorkSessionProjectionRow.target_seq,
            )
            rows = (
                await db.scalars(
                    select(WorkSessionProjectionRow)
                    .where(
                        WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                        or_(
                            repair_due,
                            failed_retry_due,
                            inconsistent_caught_up,
                        ),
                        or_(
                            WorkSessionProjectionRow.lease_expires_at.is_(None),
                            WorkSessionProjectionRow.lease_expires_at < now,
                        ),
                        WorkSessionProjectionRow.projection_id.not_in(
                            self._paused_background_projection_ids
                        ),
                    )
                    .order_by(
                        WorkSessionProjectionRow.priority.desc(),
                        WorkSessionProjectionRow.target_seq.desc(),
                        WorkSessionProjectionRow.updated_at,
                        WorkSessionProjectionRow.projection_id,
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

    async def _claim_urgent(
        self,
        projection_id: str,
    ) -> _UrgentWorkClaim | None:
        if not projection_id:
            return None
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            await self._lock_claim_budget(db)
            active, urgent = await self._active_lease_counts(db, now)
            if active >= WORK_MAX_REPAIR_CONCURRENCY or urgent >= WORK_URGENT_REPAIR_CONCURRENCY:
                return None
            state = await db.scalar(
                select(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.projection_id == projection_id,
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                    WorkSessionProjectionRow.priority >= WORK_VISIBLE_REFRESH_PRIORITY,
                    WorkSessionProjectionRow.state.in_(["pending", "materializing", "repair"]),
                    or_(
                        WorkSessionProjectionRow.next_retry_at.is_(None),
                        WorkSessionProjectionRow.next_retry_at <= now,
                    ),
                    or_(
                        WorkSessionProjectionRow.lease_expires_at.is_(None),
                        WorkSessionProjectionRow.lease_expires_at < now,
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if state is None:
                return None
            claim_priority = state.priority
            state.state = "materializing"
            state.lease_owner = self._urgent_worker_id
            state.lease_fence += 1
            state.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
            state.priority = 0
            await db.commit()
            return _UrgentWorkClaim(
                projection_id=state.projection_id,
                session_id=state.session_id,
                priority=claim_priority,
                lease_fence=state.lease_fence,
            )

    async def _repair(
        self,
        projection_id: str,
        *,
        lease_owner: str | None = None,
        repair_slot: asyncio.Semaphore | None = None,
    ) -> None:
        expected_lease_owner = lease_owner or self._worker_id
        slot = repair_slot or self._repair_slots
        urgent_read = repair_slot is self._urgent_repair_slot
        async with slot:
            fence: int | None = None
            try:
                async with self._session_factory() as db:
                    state = await db.get(WorkSessionProjectionRow, projection_id)
                    if state is None or state.lease_owner != expected_lease_owner:
                        return
                    row = await db.get(Session, state.session_id)
                    if row is None:
                        return
                    fence = state.lease_fence
                    source_session_id = state.source_session_id
                    covered_through_seq = state.covered_through_seq
                    target_seq = state.target_seq
                reader = await self._reader_for(row)
                watermark = await self._run_event_read(
                    lambda: reader.read_session_high_watermark(session_id=source_session_id),
                    urgent=urgent_read,
                )
                if watermark.last_seq <= covered_through_seq:
                    invalidate_session = getattr(self._event_store, "invalidate_session", None)
                    store_id = getattr(self._event_store, "store_id", None)
                    if callable(invalidate_session) and isinstance(store_id, str):
                        await invalidate_session(
                            store_id,
                            source_session_id,
                            source="work_repair_target_ahead",
                        )
                        watermark = await self._run_event_read(
                            lambda: reader.read_session_high_watermark(
                                session_id=source_session_id
                            ),
                            urgent=urgent_read,
                        )
                require_session_history(
                    watermark.availability,
                    from_seq=covered_through_seq + 1,
                    to_seq=max(covered_through_seq + 1, target_seq, watermark.last_seq),
                )
                if watermark.last_seq <= covered_through_seq and watermark.availability is not None:
                    async with self._session_factory() as db:
                        state = await db.scalar(
                            select(WorkSessionProjectionRow)
                            .where(
                                WorkSessionProjectionRow.projection_id == projection_id,
                                WorkSessionProjectionRow.lease_owner == expected_lease_owner,
                                WorkSessionProjectionRow.lease_fence == fence,
                            )
                            .with_for_update()
                        )
                        if state is None:
                            return
                        _advance_projection_target(state, watermark.last_seq)
                        state.state = (
                            "caught_up"
                            if state.covered_through_seq >= state.target_seq
                            else "repair"
                        )
                        state.head_checked_at = datetime.now(UTC)
                        state.next_head_check_at = None
                        state.lease_owner = None
                        state.lease_expires_at = None
                        caught_up = state.state == "caught_up"
                        await db.commit()
                    if caught_up:
                        self._notify_projection_caught_up(row.conversation_id)
                    return
                page = await self._run_event_read(
                    lambda: reader.read_session_events(
                        session_id=source_session_id,
                        after_seq=covered_through_seq,
                        limit=WORK_REPAIR_PAGE_SIZE,
                        direction="forward",
                    ),
                    urgent=urgent_read,
                )
                if not page.events and watermark.last_seq > covered_through_seq:
                    invalidate_session = getattr(self._event_store, "invalidate_session", None)
                    store_id = getattr(self._event_store, "store_id", None)
                    if callable(invalidate_session) and isinstance(store_id, str):
                        await invalidate_session(
                            store_id,
                            source_session_id,
                            source="explicit_refresh",
                        )
                        page = await self._run_event_read(
                            lambda: reader.read_session_events(
                                session_id=source_session_id,
                                after_seq=covered_through_seq,
                                limit=WORK_REPAIR_PAGE_SIZE,
                                direction="forward",
                            ),
                            urgent=urgent_read,
                        )
                    if not page.events:
                        raise RuntimeError(
                            "Work repair source returned no events below its high watermark"
                        )
                require_session_history(
                    page.availability,
                    from_seq=covered_through_seq + 1,
                    to_seq=max(
                        covered_through_seq + 1,
                        watermark.last_seq,
                        page.availability.durable_last_seq if page.availability is not None else 0,
                    ),
                )
                async with self._session_factory() as db:
                    state = await db.scalar(
                        select(WorkSessionProjectionRow)
                        .where(
                            WorkSessionProjectionRow.projection_id == projection_id,
                            WorkSessionProjectionRow.lease_owner == expected_lease_owner,
                            WorkSessionProjectionRow.lease_fence == fence,
                        )
                        .with_for_update()
                    )
                    if state is None:
                        return
                    row = await db.get(Session, state.session_id)
                    if row is None:
                        return
                    target = max(
                        state.target_seq,
                        watermark.last_seq,
                        page.last_seq or 0,
                    )
                    previous_covered = state.covered_through_seq
                    await self._materialize_batch(
                        db, row=row, state=state, raw_events=page.events, target_seq=target
                    )
                    availability_unknown = (
                        watermark.availability is None and page.availability is None
                    )
                    incomplete = (
                        page.has_more_after
                        or state.covered_through_seq < state.target_seq
                        or availability_unknown
                    )
                    state.state = "repair" if incomplete else "caught_up"
                    state.head_checked_at = datetime.now(UTC)
                    state.next_head_check_at = None
                    state.next_retry_at = (
                        datetime.now(UTC) + timedelta(seconds=self._repair_idle_check_seconds)
                        if availability_unknown
                        else None
                    )
                    state.lease_owner = None
                    state.lease_expires_at = None
                    made_progress = state.covered_through_seq > previous_covered
                    await db.commit()
                    if made_progress:
                        self._notify_projection_caught_up(row.conversation_id)
                    if incomplete:
                        if not availability_unknown:
                            self._wake.set()
                    elif not made_progress:
                        self._notify_projection_caught_up(row.conversation_id)
            except asyncio.CancelledError:
                raise
            except SessionHistoryUnavailableError as exc:
                logger.warning("Work projection source history is unavailable")
                if fence is not None:
                    async with self._session_factory() as db:
                        state = await db.scalar(
                            select(WorkSessionProjectionRow)
                            .where(
                                WorkSessionProjectionRow.projection_id == projection_id,
                                WorkSessionProjectionRow.lease_owner == expected_lease_owner,
                                WorkSessionProjectionRow.lease_fence == fence,
                            )
                            .with_for_update()
                        )
                        if state is not None:
                            state.last_error = str(exc)[:2000]
                            if state.priority:
                                state.state = "repair"
                                state.retry_count = 0
                                state.next_retry_at = None
                            else:
                                state.state = "failed"
                                state.next_retry_at = None
                            state.lease_owner = None
                            state.lease_expires_at = None
                            await db.commit()
            except Exception as exc:
                logger.warning("Work projection repair failed", exc_info=True)
                async with self._session_factory() as db:
                    state = await db.scalar(
                        select(WorkSessionProjectionRow)
                        .where(
                            WorkSessionProjectionRow.projection_id == projection_id,
                            WorkSessionProjectionRow.lease_owner == expected_lease_owner,
                            WorkSessionProjectionRow.lease_fence == fence,
                        )
                        .with_for_update()
                    )
                    if state is not None and fence is not None:
                        state.retry_count += 1
                        state.last_error = str(exc)[:2000]
                        if state.priority:
                            state.state = "repair"
                            state.retry_count = 0
                            state.next_retry_at = None
                        elif state.retry_count >= 5:
                            state.state = "failed"
                            state.next_retry_at = None
                        else:
                            state.state = "repair"
                            state.next_retry_at = datetime.now(UTC) + timedelta(
                                seconds=min(60, 2**state.retry_count)
                            )
                        state.lease_owner = None
                        state.lease_expires_at = None
                        await db.commit()

    async def _run_event_read(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        urgent: bool = False,
    ) -> Any:
        if urgent:
            async with self._urgent_event_read_slot:
                return await operation()
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

    async def _read_authoritative_watermark(self, row: Session) -> int:
        reader = await self._reader_for(row)
        source_session_id = row.intaris_session_id or row.session_id
        watermark = await self._run_event_read(
            lambda: reader.read_session_high_watermark(session_id=source_session_id)
        )
        return int(watermark.last_seq)

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

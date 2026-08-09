"""Evidence-aware Intaris backfill for Work projections."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Histogram

from cognis.api.chat_v2.cursors import (
    ChatCursorError,
    InternalChatCursorPayload,
    encode_cursor,
    validate_cursor,
)
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventPage, SessionEventStore
from cognis.api.chat_v2.schemas import TimelineBackfillResponse, TimelineItem, TimelineScope
from cognis.api.chat_v2.sync import (
    CURSOR_TTL,
    ChatV2SyncError,
    ConversationSessionRef,
    EventPostProcessor,
    _hydrate_window_pairings,
    _project_window,
    _reader_for_ref,
    _tag_events,
    current_projection_version,
    validate_backfill_limit,
)
from cognis.logging import get_logger

WORK_SCAN_CHUNK_SIZE = 200
WORK_SCAN_MAX_PAGES = 96
WORK_SCAN_MAX_EVENTS = 12_000
WORK_SCAN_MAX_HEADS = 256
WORK_SCAN_MAX_SECONDS = 10.0
WORK_WATERMARK_MAX_SECONDS = 5.0
WORK_UPSTREAM_MAX_CONCURRENCY = 4
WORK_SCAN_UNKNOWN_FRONTIER = 2**63 - 1

logger = get_logger(__name__)
WORK_SCAN_REQUESTS = Counter(
    "cognis_chat_work_scan_requests_total",
    "Evidence-aware Work scans.",
    labelnames=("outcome",),
)
WORK_SCAN_PAGES = Histogram(
    "cognis_chat_work_scan_pages",
    "Canonical event-store pages read by one Work scan.",
)
WORK_SCAN_EVENTS = Histogram(
    "cognis_chat_work_scan_events",
    "Canonical events read by one Work scan.",
)
WORK_SCAN_EVIDENCE = Histogram(
    "cognis_chat_work_scan_evidence",
    "Evidence items yielded by one Work scan.",
)
WORK_SCAN_LATENCY = Histogram(
    "cognis_chat_work_scan_latency_seconds",
    "Wall time of one evidence-aware Work scan.",
)
WORK_WATERMARK_REQUESTS = Counter(
    "cognis_chat_work_watermark_requests_total",
    "Initial Work watermark fanouts.",
    labelnames=("outcome",),
)
WORK_WATERMARK_LATENCY = Histogram(
    "cognis_chat_work_watermark_latency_seconds",
    "Wall time of one initial Work watermark fanout.",
)


@dataclass(slots=True)
class _ScanBudget:
    max_pages: int
    max_events: int
    deadline: float
    pages: int = 0
    events: int = 0
    truncated: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def reserve_read(self, requested_events: int) -> int:
        """Reserve one canonical page and its event capacity before I/O."""

        async with self._lock:
            remaining_events = self.max_events - self.events
            if (
                self.pages >= self.max_pages
                or remaining_events <= 0
                or time.monotonic() >= self.deadline
            ):
                self.truncated = True
                return 0
            reserved = min(requested_events, remaining_events)
            self.pages += 1
            self.events += reserved
            return reserved

    async def release_unused_events(self, count: int) -> None:
        if count <= 0:
            return
        async with self._lock:
            self.events = max(0, self.events - count)


class _ScanTimedOut(Exception):
    pass


async def _within_budget(awaitable: Any, budget: _ScanBudget) -> Any:
    remaining = budget.deadline - time.monotonic()
    if remaining <= 0:
        budget.truncated = True
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise _ScanTimedOut
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except TimeoutError as exc:
        budget.truncated = True
        raise _ScanTimedOut from exc


class _BudgetedReader:
    def __init__(self, reader: SessionEventStore, budget: _ScanBudget) -> None:
        self.store_id = str(getattr(reader, "store_id", "intaris"))
        self._reader = reader
        self._budget = budget

    async def read_session_events(self, **kwargs: Any) -> SessionEventPage:
        requested = max(1, int(kwargs.get("limit", WORK_SCAN_CHUNK_SIZE)))
        reserved = await self._budget.reserve_read(requested)
        if reserved == 0:
            return SessionEventPage(
                store_id=self.store_id,
                session_id=str(kwargs["session_id"]),
                events=[],
                has_more_before=True,
            )
        page = await self._reader.read_session_events(**{**kwargs, "limit": reserved})
        await self._budget.release_unused_events(reserved - len(page.events))
        return page

    async def read_session_high_watermark(self, **kwargs: Any) -> Any:
        return await self._reader.read_session_high_watermark(**kwargs)


@dataclass(slots=True)
class _StreamScan:
    ref: ConversationSessionRef
    reader: SessionEventStore
    frontier: int | None
    request_start_frontier: int
    buffer: list[RawSessionEvent] = field(default_factory=list)
    page: SessionEventPage | None = None
    exhausted: bool = False
    bounded: bool = False

    def __post_init__(self) -> None:
        if self.frontier == 0:
            self.exhausted = True

    async def fill(self) -> None:
        if self.buffer or self.exhausted:
            return
        page = await self.reader.read_session_events(
            session_id=self.ref.event_store_session_id,
            before_seq=self.frontier,
            limit=WORK_SCAN_CHUNK_SIZE,
            direction="backward",
        )
        if not page.events and page.has_more_before:
            self.bounded = True
            return
        events = page.events
        if self.frontier is not None:
            events = [event for event in events if event.seq < self.frontier]
        if not events:
            if page.has_more_before:
                raise ChatV2SyncError(
                    "event_store_paging_failed",
                    "Event store reported older Work events without advancing the page",
                )
            self.frontier = 0
            self.exhausted = True
            self.page = page
            return
        # A stream is canonical by sequence. Timestamps only merge stream heads.
        self.buffer = sorted(events, key=lambda event: (event.seq, event.event_id or ""))
        self.page = page
        if self.frontier is None:
            self.frontier = max(event.seq for event in events) + 1

    def pop_newest(self) -> RawSessionEvent:
        event = self.buffer.pop()
        self.frontier = min(self.frontier or event.seq, event.seq)
        if not self.buffer and self.page is not None and not self.page.has_more_before:
            self.exhausted = True
        return event

    def head_merge_key(self) -> tuple[datetime, int, str, int, str]:
        event = self.buffer[-1]
        timestamp = event.timestamp or datetime.min.replace(tzinfo=UTC)
        return (
            timestamp,
            self.ref.ordinal,
            event.store_id,
            event.seq,
            event.event_id or "",
        )


async def _establish_stream_heads(
    streams: Sequence[_StreamScan],
    *,
    deadline: float,
) -> None:
    """Read one canonical head for every stream before global merging."""

    if len(streams) > WORK_SCAN_MAX_HEADS:
        raise ChatV2SyncError(
            "work_scan_stream_limit",
            "Work evidence scan stream count exceeds the bounded head budget",
        )

    semaphore = asyncio.Semaphore(WORK_UPSTREAM_MAX_CONCURRENCY)

    async def read_head(stream: _StreamScan) -> tuple[_StreamScan, SessionEventPage]:
        if stream.exhausted:
            return stream, SessionEventPage(
                store_id=stream.ref.store,
                session_id=stream.ref.event_store_session_id,
                events=[],
                verified_empty=True,
            )
        async with semaphore:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            page = await asyncio.wait_for(
                stream.reader.read_session_events(
                    session_id=stream.ref.event_store_session_id,
                    before_seq=stream.frontier,
                    limit=1,
                    direction="backward",
                ),
                timeout=remaining,
            )
            return stream, page

    tasks = [asyncio.create_task(read_head(stream)) for stream in streams]
    try:
        results = await asyncio.gather(*tasks)
    except TimeoutError as exc:
        raise ChatV2SyncError(
            "work_scan_timeout",
            "Work evidence scan could not establish all stream heads before its stage deadline",
        ) from exc
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    for stream, page in results:
        events = page.events
        if stream.frontier is not None:
            events = [event for event in events if event.seq < stream.frontier]
        if not events:
            if page.has_more_before:
                raise ChatV2SyncError(
                    "event_store_paging_failed",
                    "Event store reported older Work events without establishing a stream head",
                )
            stream.frontier = 0
            stream.exhausted = True
            stream.page = page
            continue
        stream.buffer = sorted(events, key=lambda event: (event.seq, event.event_id or ""))
        stream.page = page
        if stream.frontier is None:
            stream.frontier = max(event.seq for event in events) + 1


def _lineage_fingerprint(session_refs: Sequence[ConversationSessionRef]) -> str:
    import hashlib

    value = "|".join(
        f"{ref.ordinal}:{ref.store}:{ref.event_store_session_id}:{ref.role}" for ref in session_refs
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _start_frontiers(
    *,
    before: str | None,
    scope: TimelineScope,
    session_refs: Sequence[ConversationSessionRef],
    cursor_secret: str,
    now: datetime,
    graph_fingerprint: str,
) -> list[int | None]:
    if before is None:
        return [None] * len(session_refs)
    try:
        payload = validate_cursor(
            before,
            cursor_secret,
            scope_key=scope.key,
            projection_version=current_projection_version(),
            now=now,
        )
    except ChatCursorError as exc:
        raise ChatV2SyncError(exc.code, str(exc)) from exc
    if payload.graph_fingerprint != graph_fingerprint:
        raise ChatV2SyncError(
            "lineage_changed",
            "Composite Work cursor graph fingerprint no longer matches",
        )
    if len(payload.ordinal_frontiers) != len(session_refs):
        raise ChatV2SyncError(
            "lineage_changed",
            "Composite Work cursor frontier count no longer matches",
        )
    return list(payload.ordinal_frontiers)


def _encode_frontiers(
    *,
    scope: TimelineScope,
    session_refs: Sequence[ConversationSessionRef],
    frontiers: Sequence[int],
    cursor_secret: str,
    now: datetime,
    graph_fingerprint: str,
) -> str:
    payload = InternalChatCursorPayload(
        scope_key=scope.key,
        projection_version=current_projection_version(),
        ordinal_frontiers=list(frontiers),
        graph_fingerprint=graph_fingerprint,
        view_revision=sum(frontiers),
        issued_at=now.isoformat(),
        expires_at=(now + CURSOR_TTL).isoformat(),
    )
    return str(encode_cursor(payload, cursor_secret))


async def build_work_evidence_backfill_response(
    *,
    scope: TimelineScope,
    before: str | None,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    cursor_secret: str,
    evidence_predicate: Callable[[TimelineItem], bool],
    limit: int,
    event_post_processor: EventPostProcessor | None = None,
    session_cache: Any = None,
    now: datetime | None = None,
    graph_fingerprint: str | None = None,
    max_pages: int = WORK_SCAN_MAX_PAGES,
    max_events: int = WORK_SCAN_MAX_EVENTS,
    max_seconds: float = WORK_SCAN_MAX_SECONDS,
    deadline: float | None = None,
    initial_frontiers: Sequence[int] | None = None,
) -> TimelineBackfillResponse:
    """Scan canonical streams until the page contains actual Work evidence.

    The signed cursor advances through proven non-evidence gaps. It stops at the
    last returned evidence item, so older evidence from the same source page is
    never skipped.
    """

    if max_pages < 1 or max_events < 1 or max_seconds <= 0:
        raise ValueError("Work scan bounds must be positive")
    started = time.monotonic()
    request_deadline = deadline if deadline is not None else started + max_seconds
    budget = _ScanBudget(
        max_pages=max_pages,
        max_events=max_events,
        deadline=request_deadline,
    )
    validated_limit = validate_backfill_limit(limit)
    current_time = now or datetime.now(UTC)
    cursor_graph_fingerprint = graph_fingerprint or _lineage_fingerprint(session_refs)
    start_frontiers = (
        list(initial_frontiers)
        if before is None and initial_frontiers is not None
        else _start_frontiers(
            before=before,
            scope=scope,
            session_refs=session_refs,
            cursor_secret=cursor_secret,
            now=current_time,
            graph_fingerprint=cursor_graph_fingerprint,
        )
    )
    if len(start_frontiers) != len(session_refs):
        raise ValueError("Initial Work frontiers must match the stream count")
    if any(frontier is None for frontier in start_frontiers):
        discovered, watermark_truncated = await collect_initial_work_frontiers(
            session_refs=session_refs,
            event_store=event_store,
            deadline=request_deadline,
        )
        start_frontiers = [
            discovered[index] if frontier is None else frontier
            for index, frontier in enumerate(start_frontiers)
        ]
        budget.truncated = budget.truncated or watermark_truncated
    streams = [
        _StreamScan(
            ref=ref,
            reader=_reader_for_ref(ref, event_store),
            frontier=frontier,
            request_start_frontier=int(frontier),
        )
        for ref, frontier in zip(session_refs, start_frontiers, strict=True)
    ]
    evidence: list[TimelineItem] = []
    evidence_ids: set[str] = set()
    page_frontiers: list[int] | None = None
    has_more_before = False
    in_flight_stream: _StreamScan | None = None
    in_flight_frontier: int | None = None

    try:
        await _establish_stream_heads(streams, deadline=request_deadline)
        for stream in streams:
            stream.reader = _BudgetedReader(stream.reader, budget)
        while True:
            if time.monotonic() >= budget.deadline:
                budget.truncated = True
                break
            available = [stream for stream in streams if stream.buffer]
            if not available:
                refillable = [
                    stream for stream in streams if not stream.exhausted and not stream.bounded
                ]
                if not refillable or budget.truncated:
                    break
                await _within_budget(_fill_streams(refillable, budget), budget)
                available = [stream for stream in streams if stream.buffer]
                if not available:
                    break

            stream = max(available, key=lambda candidate: candidate.head_merge_key())
            in_flight_stream = stream
            in_flight_frontier = stream.frontier
            raw_event = stream.pop_newest()
            tagged = _tag_events([raw_event], stream.ref)
            if event_post_processor is not None:
                tagged = await _within_budget(event_post_processor(tagged), budget)
            hydrated = await _within_budget(
                _hydrate_window_pairings(
                    tagged,
                    session_refs=[
                        ref.model_copy(update={"reader": candidate.reader})
                        for ref, candidate in zip(session_refs, streams, strict=True)
                    ],
                    event_store=event_store,
                    session_cache=session_cache,
                ),
                budget,
            )
            projected = _project_window(hydrated)
            current_items = [
                item
                for item in projected.items
                if item.id not in evidence_ids
                and evidence_predicate(item)
                and _references_event(item, raw_event)
                and not _is_call_covered_before_request(
                    item,
                    raw_event=raw_event,
                    request_start_frontier=stream.request_start_frontier,
                )
            ]
            for item in current_items:
                evidence_ids.add(item.id)
                evidence.append(item)
                if len(evidence) == validated_limit:
                    page_frontiers = [int(candidate.frontier or 0) for candidate in streams]
                elif len(evidence) > validated_limit:
                    has_more_before = True
                    break
            if has_more_before:
                break
            in_flight_stream = None
            in_flight_frontier = None
            if not stream.buffer and not stream.exhausted:
                await _within_budget(stream.fill(), budget)
    except _ScanTimedOut:
        if in_flight_stream is not None:
            in_flight_stream.frontier = in_flight_frontier
            in_flight_stream.exhausted = False
        budget.truncated = True
    except ChatV2SyncError as exc:
        latency = time.monotonic() - started
        WORK_SCAN_REQUESTS.labels(
            outcome="timeout_heads" if exc.code == "work_scan_timeout" else "error"
        ).inc()
        WORK_SCAN_PAGES.observe(budget.pages)
        WORK_SCAN_EVENTS.observe(budget.events)
        WORK_SCAN_EVIDENCE.observe(len(evidence))
        WORK_SCAN_LATENCY.observe(latency)
        raise
    except asyncio.CancelledError:
        latency = time.monotonic() - started
        WORK_SCAN_REQUESTS.labels(outcome="cancelled").inc()
        WORK_SCAN_PAGES.observe(budget.pages)
        WORK_SCAN_EVENTS.observe(budget.events)
        WORK_SCAN_EVIDENCE.observe(len(evidence))
        WORK_SCAN_LATENCY.observe(latency)
        logger.info(
            "chat_v2: Work evidence scan cancelled",
            extra={
                "extra_data": {
                    "pages": budget.pages,
                    "events": budget.events,
                    "evidence_yield": len(evidence),
                    "truncated": True,
                    "latency_seconds": latency,
                }
            },
        )
        raise

    if page_frontiers is None and evidence:
        page_frontiers = [int(stream.frontier or 0) for stream in streams]
    if budget.truncated and page_frontiers is None:
        page_frontiers = [int(stream.frontier or 0) for stream in streams]
    made_progress = page_frontiers is not None and any(
        frontier < start for frontier, start in zip(page_frontiers, start_frontiers, strict=True)
    )
    exhausted = all(stream.exhausted and not stream.buffer for stream in streams)
    if budget.truncated and not evidence and not made_progress and not exhausted:
        latency = time.monotonic() - started
        WORK_SCAN_REQUESTS.labels(outcome="timeout_no_progress").inc()
        WORK_SCAN_PAGES.observe(budget.pages)
        WORK_SCAN_EVENTS.observe(budget.events)
        WORK_SCAN_EVIDENCE.observe(0)
        WORK_SCAN_LATENCY.observe(latency)
        logger.warning(
            "chat_v2: Work evidence scan timed out without progress",
            extra={
                "extra_data": {
                    "pages": budget.pages,
                    "events": budget.events,
                    "latency_seconds": latency,
                }
            },
        )
        raise ChatV2SyncError(
            "work_scan_timeout",
            "Work evidence scan reached its stage deadline before it made progress",
        )
    has_more_before = (
        has_more_before
        or budget.truncated
        or any(stream.buffer or stream.bounded or not stream.exhausted for stream in streams)
    )
    selected = sorted(evidence[:validated_limit], key=lambda item: item.sort_key)
    response = TimelineBackfillResponse(
        projection_version=current_projection_version(),
        scope=scope,
        conversation_id=scope.conversation_id,
        items=selected,
        cycle_states=[],
        has_more_before=has_more_before,
        before_cursor=(
            _encode_frontiers(
                scope=scope,
                session_refs=session_refs,
                frontiers=page_frontiers,
                cursor_secret=cursor_secret,
                now=current_time,
                graph_fingerprint=cursor_graph_fingerprint,
            )
            if has_more_before and page_frontiers is not None
            else None
        ),
        server_time=current_time.isoformat(),
    )
    latency = time.monotonic() - started
    WORK_SCAN_REQUESTS.labels(
        outcome="truncated_progress" if budget.truncated else "complete"
    ).inc()
    WORK_SCAN_PAGES.observe(budget.pages)
    WORK_SCAN_EVENTS.observe(budget.events)
    WORK_SCAN_EVIDENCE.observe(len(selected))
    WORK_SCAN_LATENCY.observe(latency)
    logger.info(
        "chat_v2: Work evidence scan completed",
        extra={
            "extra_data": {
                "pages": budget.pages,
                "events": budget.events,
                "evidence_yield": len(selected),
                "truncated": budget.truncated,
                "latency_seconds": latency,
            }
        },
    )
    return response


async def _fill_streams(
    streams: Sequence[_StreamScan],
    budget: _ScanBudget,
) -> None:
    """Fill only while the atomically reserved request budget has capacity."""

    for stream in streams:
        if budget.truncated:
            break
        await stream.fill()


def _references_event(item: TimelineItem, event: RawSessionEvent) -> bool:
    return any(
        source.store == event.store_id
        and source.session_id == event.session_id
        and source.seq == event.seq
        for source in item.source_refs
    )


def _is_call_covered_before_request(
    item: TimelineItem,
    *,
    raw_event: RawSessionEvent,
    request_start_frontier: int,
) -> bool:
    """Suppress a call whose hydrated result was in already-consumed territory."""

    if raw_event.type != "tool_call":
        return False
    matching = [
        source
        for source in item.source_refs
        if source.store == raw_event.store_id and source.session_id == raw_event.session_id
    ]
    return bool(matching) and max(source.seq for source in matching) >= request_start_frontier


async def collect_initial_work_frontiers(
    *,
    session_refs: Sequence[ConversationSessionRef],
    event_store: SessionEventStore,
    deadline: float,
) -> tuple[list[int], bool]:
    """Collect all initial high-watermarks within one stage deadline."""

    started = time.monotonic()
    frontiers = [WORK_SCAN_UNKNOWN_FRONTIER] * len(session_refs)
    semaphore = asyncio.Semaphore(WORK_UPSTREAM_MAX_CONCURRENCY)

    async def read_watermark(ref: ConversationSessionRef) -> Any:
        async with semaphore:
            return await _reader_for_ref(ref, event_store).read_session_high_watermark(
                session_id=ref.event_store_session_id
            )

    tasks = [asyncio.create_task(read_watermark(ref)) for ref in session_refs]
    pending: set[asyncio.Task[Any]] = set(tasks)
    try:
        remaining = max(0.0, deadline - time.monotonic())
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending:
            raise ChatV2SyncError(
                "work_watermark_timeout",
                "Work watermark fanout did not complete before its stage deadline",
            )
        for index, task in enumerate(tasks):
            last_seq = int(task.result().last_seq)
            frontiers[index] = last_seq + 1 if last_seq > 0 else 0
    except ChatV2SyncError:
        latency = time.monotonic() - started
        WORK_WATERMARK_REQUESTS.labels(outcome="timeout").inc()
        WORK_WATERMARK_LATENCY.observe(latency)
        logger.warning(
            "chat_v2: Work watermark fanout timed out",
            extra={
                "extra_data": {
                    "streams": len(session_refs),
                    "latency_seconds": latency,
                }
            },
        )
        raise
    except asyncio.CancelledError:
        latency = time.monotonic() - started
        WORK_WATERMARK_REQUESTS.labels(outcome="cancelled").inc()
        WORK_WATERMARK_LATENCY.observe(latency)
        raise
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    latency = time.monotonic() - started
    outcome = "complete"
    WORK_WATERMARK_REQUESTS.labels(outcome=outcome).inc()
    WORK_WATERMARK_LATENCY.observe(latency)
    logger.info(
        "chat_v2: Work watermark fanout completed",
        extra={
            "extra_data": {
                "streams": len(session_refs),
                "resolved": sum(frontier != WORK_SCAN_UNKNOWN_FRONTIER for frontier in frontiers),
                "outcome": outcome,
                "latency_seconds": latency,
            }
        },
    )
    return frontiers, False


__all__ = [
    "WORK_SCAN_CHUNK_SIZE",
    "WORK_SCAN_MAX_HEADS",
    "WORK_SCAN_MAX_SECONDS",
    "WORK_UPSTREAM_MAX_CONCURRENCY",
    "WORK_WATERMARK_MAX_SECONDS",
    "build_work_evidence_backfill_response",
    "collect_initial_work_frontiers",
]

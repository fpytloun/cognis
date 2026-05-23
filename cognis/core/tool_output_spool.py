"""Bounded live spool for streamed tool output chunks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class ToolOutputChunk:
    index: int
    offset: int
    stream: str | None
    text: str
    created_at: datetime = field(default_factory=_now)


@dataclass(slots=True)
class ToolOutputSpoolEntry:
    conversation_id: str
    session_id: str
    call_id: str
    tool_name: str
    turn_id: str | None = None
    status: str = "running"
    chunks: list[ToolOutputChunk] = field(default_factory=list)
    total_bytes: int = 0
    dropped_chunks: int = 0
    dropped_bytes: int = 0
    truncated: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class ToolOutputSpoolPage:
    content: str
    chunks: list[ToolOutputChunk]
    status: str
    source: str = "live_spool"
    offset: int = 0
    limit: int = 200
    next_offset: int | None = None
    prev_offset: int | None = None
    has_more_before: bool = False
    has_more_after: bool = False
    output_size: int = 0
    chunk_count: int = 0
    truncated: bool = False


class ToolOutputSpool:
    """In-process bounded live output spool.

    The spool is intentionally small and ephemeral. It preserves chunk order
    and enough metadata for the UI to page live output without placing the
    full stream in conversation history snapshots.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 900,
        max_bytes_per_call: int = 2_000_000,
        max_chunks_per_call: int = 10_000,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_bytes = max_bytes_per_call
        self._max_chunks = max_chunks_per_call
        self._entries: dict[tuple[str, str, str], ToolOutputSpoolEntry] = {}

    def append(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        text: str,
        stream: str | None,
        turn_id: str | None = None,
    ) -> tuple[int, int]:
        self.prune()
        key = (conversation_id, session_id, call_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = ToolOutputSpoolEntry(
                conversation_id=conversation_id,
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                turn_id=turn_id,
            )
            self._entries[key] = entry
        index = entry.dropped_chunks + len(entry.chunks)
        offset = entry.dropped_bytes + sum(
            len(chunk.text.encode("utf-8")) for chunk in entry.chunks
        )
        chunk = ToolOutputChunk(index=index, offset=offset, stream=stream, text=text)
        entry.chunks.append(chunk)
        entry.total_bytes += len(text.encode("utf-8"))
        entry.updated_at = _now()
        self._enforce_limits(entry)
        return index, offset

    def mark_complete(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        status: str,
        tool_name: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        key = (conversation_id, session_id, call_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = ToolOutputSpoolEntry(
                conversation_id=conversation_id,
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name or "tool",
                turn_id=turn_id,
            )
            self._entries[key] = entry
        entry.status = status
        entry.updated_at = _now()

    def get_entry(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
    ) -> ToolOutputSpoolEntry | None:
        self.prune()
        return self._entries.get((conversation_id, session_id, call_id))

    def page(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        offset: int = 0,
        limit: int = 200,
        latest: bool = False,
    ) -> ToolOutputSpoolPage | None:
        entry = self.get_entry(conversation_id, session_id, call_id)
        if entry is None:
            return None
        chunks = entry.chunks
        safe_limit = max(1, min(limit, 1000))
        start = max(0, len(chunks) - safe_limit) if latest else max(0, offset)
        end = min(len(chunks), start + safe_limit)
        selected = chunks[start:end]
        return ToolOutputSpoolPage(
            content="".join(chunk.text for chunk in selected),
            chunks=selected,
            status=entry.status,
            offset=start,
            limit=safe_limit,
            next_offset=end if end < len(chunks) else None,
            prev_offset=max(0, start - safe_limit) if start > 0 else None,
            has_more_before=start > 0 or entry.dropped_chunks > 0,
            has_more_after=end < len(chunks),
            output_size=entry.total_bytes,
            chunk_count=entry.dropped_chunks + len(chunks),
            truncated=entry.truncated,
        )

    def prune(self) -> None:
        cutoff = _now() - self._ttl
        expired = [key for key, entry in self._entries.items() if entry.updated_at < cutoff]
        for key in expired:
            self._entries.pop(key, None)

    def _enforce_limits(self, entry: ToolOutputSpoolEntry) -> None:
        while len(entry.chunks) > self._max_chunks:
            self._drop_oldest(entry)
        while sum(len(chunk.text.encode("utf-8")) for chunk in entry.chunks) > self._max_bytes:
            self._drop_oldest(entry)

    @staticmethod
    def _drop_oldest(entry: ToolOutputSpoolEntry) -> None:
        if not entry.chunks:
            return
        chunk = entry.chunks.pop(0)
        entry.dropped_chunks += 1
        entry.dropped_bytes += len(chunk.text.encode("utf-8"))
        entry.truncated = True

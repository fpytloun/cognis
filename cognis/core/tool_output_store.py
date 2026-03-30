"""Ephemeral storage for full tool outputs, keyed by call_id.

Stores the complete executor output on the controller's local filesystem
so the LLM can later explore it via ``read_tool_output`` and
``search_tool_output`` built-in tools.  Files are automatically cleaned
up based on a configurable TTL and a directory-wide size cap.

The store is local to the controller process.  For Phase 2 remote
executors, the executor would transfer the output back via JSON-RPC
and the controller would save it locally.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from cognis.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ReadResult:
    """Result of reading lines from a stored tool output."""

    content: str
    total_lines: int
    offset: int
    limit: int
    has_more: bool


@dataclass(slots=True)
class SearchMatch:
    """A single search match with context."""

    line_number: int
    line: str
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchResult:
    """Result of searching within a stored tool output."""

    matches: list[SearchMatch]
    total_matches: int
    truncated: bool = False


_MAX_SEARCH_MATCHES = 100
_MAX_LINE_LENGTH = 2000


class ToolOutputStore:
    """Ephemeral local storage for full tool outputs."""

    def __init__(
        self,
        base_dir: Path,
        *,
        ttl_hours: int = 24,
        max_size_mb: int = 500,
    ) -> None:
        self._base_dir = base_dir / "tool-outputs"
        self._ttl_seconds = ttl_hours * 3600
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, call_id: str) -> Path:
        # Sanitise call_id for filesystem safety
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", call_id)
        return self._base_dir / f"{safe_id}.txt"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save(self, call_id: str, output: str) -> None:
        """Save full output to disk.  Overwrites if exists."""
        path = self._path(call_id)
        try:
            path.write_text(output, encoding="utf-8")
        except OSError:
            logger.warning(
                "tool_output_store: failed to save output",
                extra={"extra_data": {"call_id": call_id}},
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def read(
        self,
        call_id: str,
        *,
        offset: int = 1,
        limit: int = 200,
    ) -> ReadResult | None:
        """Read lines from stored output (1-indexed offset).

        Returns ``None`` if the output file does not exist.
        """
        path = self._path(call_id)
        if not path.exists():
            return None

        try:
            all_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

        total_lines = len(all_lines)
        start_idx = max(0, offset - 1)
        end_idx = start_idx + limit
        selected = all_lines[start_idx:end_idx]

        # Number each line and truncate long lines
        numbered: list[str] = []
        for i, line in enumerate(selected, start=start_idx + 1):
            if len(line) > _MAX_LINE_LENGTH:
                line = line[:_MAX_LINE_LENGTH] + "... (line truncated)"
            numbered.append(f"{i}: {line}")

        return ReadResult(
            content="\n".join(numbered),
            total_lines=total_lines,
            offset=offset,
            limit=limit,
            has_more=end_idx < total_lines,
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        call_id: str,
        pattern: str,
        *,
        context_lines: int = 3,
    ) -> SearchResult | None:
        """Regex search within stored output.

        Returns ``None`` if the output file does not exist.
        """
        path = self._path(call_id)
        if not path.exists():
            return None

        try:
            all_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return SearchResult(matches=[], total_matches=0)

        matches: list[SearchMatch] = []
        total_matches = 0

        for i, line in enumerate(all_lines):
            if regex.search(line):
                total_matches += 1
                if len(matches) < _MAX_SEARCH_MATCHES:
                    display_line = line
                    if len(display_line) > _MAX_LINE_LENGTH:
                        display_line = display_line[:_MAX_LINE_LENGTH] + "..."
                    ctx_before = [
                        all_lines[j][:_MAX_LINE_LENGTH] for j in range(max(0, i - context_lines), i)
                    ]
                    ctx_after = [
                        all_lines[j][:_MAX_LINE_LENGTH]
                        for j in range(i + 1, min(len(all_lines), i + 1 + context_lines))
                    ]
                    matches.append(
                        SearchMatch(
                            line_number=i + 1,
                            line=display_line,
                            context_before=ctx_before,
                            context_after=ctx_after,
                        )
                    )

        return SearchResult(
            matches=matches,
            total_matches=total_matches,
            truncated=total_matches > _MAX_SEARCH_MATCHES,
        )

    # ------------------------------------------------------------------
    # Existence / Deletion
    # ------------------------------------------------------------------

    async def exists(self, call_id: str) -> bool:
        return self._path(call_id).exists()

    async def delete(self, call_id: str) -> None:
        import contextlib

        path = self._path(call_id)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    async def cleanup_session(self, call_ids: list[str]) -> None:
        """Delete outputs for specific call_ids (session cleanup)."""
        for call_id in call_ids:
            await self.delete(call_id)

    # ------------------------------------------------------------------
    # Lifecycle cleanup
    # ------------------------------------------------------------------

    async def cleanup_expired(self) -> int:
        """Delete files older than TTL.  Returns count deleted.

        Intended to be called on controller startup.
        """
        deleted = 0
        now = time.time()
        try:
            for path in self._base_dir.iterdir():
                if path.is_file() and (now - path.stat().st_mtime) > self._ttl_seconds:
                    path.unlink(missing_ok=True)
                    deleted += 1
        except OSError:
            logger.warning("tool_output_store: cleanup_expired failed", exc_info=True)
        if deleted:
            logger.info(
                "tool_output_store: expired files cleaned up",
                extra={"extra_data": {"deleted": deleted}},
            )
        return deleted

    async def enforce_size_cap(self) -> int:
        """If directory exceeds max_size_mb, delete oldest files first.

        Returns count deleted.  Intended to be called after ``save()``.
        """
        try:
            files = sorted(self._base_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError:
            return 0

        total_size = sum(f.stat().st_size for f in files if f.is_file())
        deleted = 0
        for path in files:
            if total_size <= self._max_size_bytes:
                break
            if path.is_file():
                try:
                    total_size -= path.stat().st_size
                    path.unlink(missing_ok=True)
                    deleted += 1
                except OSError:
                    pass
        if deleted:
            logger.info(
                "tool_output_store: size cap enforced",
                extra={"extra_data": {"deleted": deleted}},
            )
        return deleted

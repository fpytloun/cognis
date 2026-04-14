"""Executor-local file freshness tracking for filesystem mutation safety."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FILE_FRESHNESS_KEY = "file_freshness_tracker"


@dataclass(slots=True)
class FileStamp:
    mtime_ns: int
    size: int


class FileFreshnessTracker:
    """Track per-scope file reads and serialize file mutations."""

    def __init__(self) -> None:
        self._reads: dict[str, dict[str, FileStamp]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def scope_id(self, context: Any) -> str:
        runtime_metadata = getattr(context, "runtime_metadata", {}) or {}
        explicit = getattr(context, "execution_scope_id", None)
        if explicit:
            return str(explicit)
        user_email = runtime_metadata.get("user_email")
        executor_handle = getattr(context, "executor_handle", None)
        executor_id = getattr(executor_handle, "executor_id", "executor")
        if user_email:
            return f"{user_email}:{executor_id}"
        return str(executor_id)

    async def record_read(self, scope_id: str, path: Path) -> None:
        self._reads.setdefault(scope_id, {})[self._normalize(path)] = self._stamp(path)

    async def record_write(self, scope_id: str, path: Path) -> None:
        self._reads.setdefault(scope_id, {})[self._normalize(path)] = self._stamp(path)

    async def assert_can_modify_existing(self, scope_id: str, path: Path) -> None:
        normalized = self._normalize(path)
        stamp = self._reads.get(scope_id, {}).get(normalized)
        if stamp is None:
            raise RuntimeError(
                f"You must read file {path} before modifying it. Use the read tool first."
            )
        current = self._stamp(path)
        if current.mtime_ns != stamp.mtime_ns or current.size != stamp.size:
            raise RuntimeError(
                f"File {path} has been modified since it was last read. Please read the file again before modifying it."
            )

    def lock_for(self, path: Path) -> asyncio.Lock:
        normalized = self._normalize(path)
        lock = self._locks.get(normalized)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[normalized] = lock
        return lock

    @staticmethod
    def _normalize(path: Path) -> str:
        return str(path.expanduser().resolve(strict=False))

    @staticmethod
    def _stamp(path: Path) -> FileStamp:
        stat = path.stat()
        return FileStamp(mtime_ns=stat.st_mtime_ns, size=stat.st_size)


def get_file_freshness_tracker(
    runtime_metadata: Mapping[str, Any] | dict[str, Any],
) -> FileFreshnessTracker:
    """Return the shared file freshness tracker from runtime metadata."""
    existing = runtime_metadata.get(_FILE_FRESHNESS_KEY)
    if isinstance(existing, FileFreshnessTracker):
        return existing
    if not isinstance(runtime_metadata, dict):
        raise TypeError("runtime_metadata must be mutable for file freshness tracking")
    tracker = FileFreshnessTracker()
    runtime_metadata[_FILE_FRESHNESS_KEY] = tracker
    return tracker

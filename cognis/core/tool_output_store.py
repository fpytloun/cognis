"""Ephemeral storage for full tool outputs, keyed by call_id.

Stores the complete executor output so the LLM can later explore it via
``read_tool_output`` and ``search_tool_output`` built-in tools.  Files
are automatically cleaned up based on a configurable TTL and a
directory-wide size cap.

Supports two backends:
- **filesystem** (default): local controller filesystem.
- **s3**: MinIO/S3-compatible object storage for shared/persistent access.

The store is backend-agnostic at the API level.  ``read()`` and
``search()`` load the full content into memory for line-based operations
— acceptable for ephemeral tool outputs (typically < 1 MB).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolOutputBackend(Protocol):
    """Protocol for tool output storage backends."""

    async def save(self, call_id: str, output: str) -> None: ...
    async def load(self, call_id: str) -> str | None: ...
    async def exists(self, call_id: str) -> bool: ...
    async def delete(self, call_id: str) -> None: ...
    async def cleanup_expired(self, ttl_seconds: int) -> int: ...
    async def enforce_size_cap(self, max_size_bytes: int) -> int: ...


# ---------------------------------------------------------------------------
# Filesystem backend
# ---------------------------------------------------------------------------


class FilesystemToolOutputBackend:
    """Store tool outputs on the local filesystem."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir / "tool-outputs"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, call_id: str) -> Path:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", call_id)
        return self._base_dir / f"{safe_id}.txt"

    async def save(self, call_id: str, output: str) -> None:
        path = self._path(call_id)
        try:
            path.write_text(output, encoding="utf-8")
        except OSError:
            logger.warning(
                "tool_output_store: failed to save output",
                extra={"extra_data": {"call_id": call_id}},
                exc_info=True,
            )

    async def load(self, call_id: str) -> str | None:
        path = self._path(call_id)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    async def exists(self, call_id: str) -> bool:
        return self._path(call_id).exists()

    async def delete(self, call_id: str) -> None:
        import contextlib

        path = self._path(call_id)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    async def cleanup_expired(self, ttl_seconds: int) -> int:
        deleted = 0
        now = time.time()
        try:
            for path in self._base_dir.iterdir():
                if path.is_file() and (now - path.stat().st_mtime) > ttl_seconds:
                    path.unlink(missing_ok=True)
                    deleted += 1
        except OSError:
            logger.warning("tool_output_store: cleanup_expired failed", exc_info=True)
        return deleted

    async def enforce_size_cap(self, max_size_bytes: int) -> int:
        try:
            files = sorted(self._base_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError:
            return 0

        total_size = sum(f.stat().st_size for f in files if f.is_file())
        deleted = 0
        for path in files:
            if total_size <= max_size_bytes:
                break
            if path.is_file():
                try:
                    file_size = path.stat().st_size
                    path.unlink(missing_ok=True)
                    total_size -= file_size
                    deleted += 1
                except OSError:
                    pass
        return deleted


# ---------------------------------------------------------------------------
# S3 backend
# ---------------------------------------------------------------------------


class S3ToolOutputBackend:
    """Store tool outputs in S3/MinIO-compatible object storage.

    Objects are stored as UTF-8 text under the key
    ``tool-outputs/{safe_call_id}.txt``.  TTL cleanup uses object
    ``LastModified`` timestamps.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "",
    ) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        kwargs: dict[str, Any] = {
            "endpoint_url": endpoint,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": BotoConfig(signature_version="s3v4"),
        }
        if region:
            kwargs["region_name"] = region

        self._client = boto3.client("s3", **kwargs)
        self._bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self._bucket)
                logger.info(
                    "tool_output_store: created S3 bucket",
                    extra={"extra_data": {"bucket": self._bucket}},
                )
            except Exception:
                logger.warning(
                    "tool_output_store: failed to create S3 bucket",
                    extra={"extra_data": {"bucket": self._bucket}},
                    exc_info=True,
                )

    def _key(self, call_id: str) -> str:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", call_id)
        return f"tool-outputs/{safe_id}.txt"

    def _sync_save(self, call_id: str, output: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key(call_id),
            Body=output.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )

    async def save(self, call_id: str, output: str) -> None:
        try:
            await asyncio.to_thread(self._sync_save, call_id, output)
        except Exception:
            logger.warning(
                "tool_output_store: S3 save failed",
                extra={"extra_data": {"call_id": call_id}},
                exc_info=True,
            )

    def _sync_load(self, call_id: str) -> str | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key(call_id))
            return response["Body"].read().decode("utf-8")
        except self._client.exceptions.NoSuchKey:
            return None

    async def load(self, call_id: str) -> str | None:
        try:
            return await asyncio.to_thread(self._sync_load, call_id)
        except Exception:
            logger.warning(
                "tool_output_store: S3 load failed",
                extra={"extra_data": {"call_id": call_id}},
                exc_info=True,
            )
            return None

    def _sync_exists(self, call_id: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(call_id))
            return True
        except Exception:
            return False

    async def exists(self, call_id: str) -> bool:
        return await asyncio.to_thread(self._sync_exists, call_id)

    def _sync_delete(self, call_id: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._key(call_id))

    async def delete(self, call_id: str) -> None:
        try:
            await asyncio.to_thread(self._sync_delete, call_id)
        except Exception:
            pass

    def _sync_cleanup_expired(self, ttl_seconds: int) -> int:
        deleted = 0
        now = time.time()
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix="tool-outputs/"):
            for obj in page.get("Contents", []):
                last_modified = obj["LastModified"].timestamp()
                if (now - last_modified) > ttl_seconds:
                    self._client.delete_object(Bucket=self._bucket, Key=obj["Key"])
                    deleted += 1
        return deleted

    async def cleanup_expired(self, ttl_seconds: int) -> int:
        """Delete objects older than TTL based on LastModified."""
        try:
            return await asyncio.to_thread(self._sync_cleanup_expired, ttl_seconds)
        except Exception:
            logger.warning("tool_output_store: S3 cleanup_expired failed", exc_info=True)
            return 0

    def _sync_enforce_size_cap(self, max_size_bytes: int) -> int:
        objects: list[dict[str, Any]] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix="tool-outputs/"):
            for obj in page.get("Contents", []):
                objects.append(obj)

        total_size = sum(obj["Size"] for obj in objects)
        if total_size <= max_size_bytes:
            return 0

        objects.sort(key=lambda o: o["LastModified"])
        deleted = 0
        for obj in objects:
            if total_size <= max_size_bytes:
                break
            self._client.delete_object(Bucket=self._bucket, Key=obj["Key"])
            total_size -= obj["Size"]
            deleted += 1
        return deleted

    async def enforce_size_cap(self, max_size_bytes: int) -> int:
        """Delete oldest objects if total size exceeds cap."""
        try:
            return await asyncio.to_thread(self._sync_enforce_size_cap, max_size_bytes)
        except Exception:
            logger.warning("tool_output_store: S3 enforce_size_cap failed", exc_info=True)
            return 0

            objects.sort(key=lambda o: o["LastModified"])
            deleted = 0
            for obj in objects:
                if total_size <= max_size_bytes:
                    break
                self._client.delete_object(Bucket=self._bucket, Key=obj["Key"])
                total_size -= obj["Size"]
                deleted += 1
            return deleted
        except Exception:
            logger.warning("tool_output_store: S3 enforce_size_cap failed", exc_info=True)
            return 0


# ---------------------------------------------------------------------------
# High-level store (backend-agnostic)
# ---------------------------------------------------------------------------


class ToolOutputStore:
    """Ephemeral storage for full tool outputs.

    Delegates to a pluggable backend (filesystem or S3).
    """

    def __init__(
        self,
        backend: ToolOutputBackend,
        *,
        ttl_hours: int = 24,
        max_size_mb: int = 500,
    ) -> None:
        self._backend = backend
        self._ttl_seconds = ttl_hours * 3600
        self._max_size_bytes = max_size_mb * 1024 * 1024

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save(self, call_id: str, output: str) -> None:
        """Save full output.  Overwrites if exists."""
        await self._backend.save(call_id, output)

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

        Returns ``None`` if the output does not exist.
        """
        content = await self._backend.load(call_id)
        if content is None:
            return None

        all_lines = content.splitlines()
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

        Returns ``None`` if the output does not exist.
        """
        content = await self._backend.load(call_id)
        if content is None:
            return None

        all_lines = content.splitlines()

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
        return await self._backend.exists(call_id)

    async def delete(self, call_id: str) -> None:
        await self._backend.delete(call_id)

    async def cleanup_session(self, call_ids: list[str]) -> None:
        """Delete outputs for specific call_ids (session cleanup)."""
        for call_id in call_ids:
            await self.delete(call_id)

    # ------------------------------------------------------------------
    # Lifecycle cleanup
    # ------------------------------------------------------------------

    async def cleanup_expired(self) -> int:
        """Delete outputs older than TTL.  Returns count deleted."""
        deleted = await self._backend.cleanup_expired(self._ttl_seconds)
        if deleted:
            logger.info(
                "tool_output_store: expired outputs cleaned up",
                extra={"extra_data": {"deleted": deleted}},
            )
        return deleted

    async def enforce_size_cap(self) -> int:
        """If storage exceeds max size, delete oldest outputs first."""
        return await self._backend.enforce_size_cap(self._max_size_bytes)

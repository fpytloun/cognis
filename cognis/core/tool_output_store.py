"""TTL-bound artifact storage for full tool outputs, keyed by call_id.

Stores the complete executor output so the LLM can later explore it via
``read_tool_output`` and ``search_tool_output`` built-in tools.  Files
are automatically cleaned up based on a configurable TTL and a
directory-wide size cap.

Supports two backends:
- **filesystem** (default): local controller artifact filesystem.
- **s3**: MinIO/S3-compatible artifact object storage for shared access.

The store is backend-agnostic at the API level.  ``read()`` and
``search()`` load the full content into memory for line-based operations
— acceptable for TTL-bound tool outputs (typically < 1 MB).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cognis.core.anchored_output import markdown_heading_anchors
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


@dataclass(slots=True)
class OutputAnchor:
    """Named section within a stored tool output."""

    anchor: str
    label: str | None
    kind: str
    start_line: int | None
    end_line: int | None
    artifact_candidate: dict[str, Any] | None = None
    anchor_id: str | None = None
    format: str = "text"
    summary: str | None = None
    locator: dict[str, Any] | None = None
    recovery_op: str = "read_lines"
    priority: int = 50
    promote: bool = False
    lazy_artifact_ref: str | None = None


@dataclass(slots=True)
class AnchorReadResult:
    """Result of reading a stored anchored section."""

    anchor: OutputAnchor
    content: str


_MAX_SEARCH_MATCHES = 100
_MAX_LINE_LENGTH = 2000
_ANCHOR_LINE_RE = re.compile(r"^\[\[(?P<anchor>[^\]]+)\]\]$")


def _parse_inline_anchors(content: str) -> list[OutputAnchor]:
    """Parse inline ``[[anchor]]`` markers from stored output text."""
    lines = content.splitlines()
    found: list[tuple[str, int]] = []
    for index, line in enumerate(lines, start=1):
        match = _ANCHOR_LINE_RE.fullmatch(line.strip())
        if match:
            found.append((match.group("anchor"), index))

    anchors: list[OutputAnchor] = []
    for position, (anchor, start_line) in enumerate(found):
        next_start = found[position + 1][1] if position + 1 < len(found) else len(lines) + 1
        label = None
        for candidate in lines[start_line : next_start - 1]:
            stripped = candidate.strip()
            if stripped and not _ANCHOR_LINE_RE.fullmatch(stripped):
                label = stripped[:120]
                break
        anchors.append(
            OutputAnchor(
                anchor=anchor,
                label=label,
                kind=_anchor_kind(anchor),
                start_line=start_line,
                end_line=next_start - 1,
                artifact_candidate=None,
            )
        )
    return anchors


def _augment_with_markdown_heading_anchors(
    content: str,
    anchors: list[OutputAnchor],
) -> list[OutputAnchor]:
    """Add Markdown heading anchors to stored output anchor metadata."""

    heading_anchors = markdown_heading_anchors(
        content,
        existing_anchors=[
            {
                "anchor": item.anchor,
                "label": item.label,
                "kind": item.kind,
                "start_line": item.start_line,
                "end_line": item.end_line,
            }
            for item in anchors
        ],
    )
    if not heading_anchors:
        return anchors
    result = list(anchors)
    for item in heading_anchors:
        anchor = item.get("anchor")
        kind = item.get("kind")
        start_line = item.get("start_line")
        end_line = item.get("end_line")
        if (
            not isinstance(anchor, str)
            or not isinstance(kind, str)
            or not isinstance(start_line, int)
            or not isinstance(end_line, int)
        ):
            continue
        label = item.get("label")
        result.append(
            OutputAnchor(
                anchor=anchor,
                label=label if isinstance(label, str) else None,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                artifact_candidate=None,
            )
        )
    return result


def _anchor_kind(anchor: str) -> str:
    """Infer a generic section kind from an anchor name."""
    prefix, _, _ = anchor.partition(":")
    if anchor == "answer":
        return "answer"
    if prefix == "result":
        return "search_result"
    return prefix or "section"


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolOutputBackend(Protocol):
    """Protocol for tool output storage backends."""

    async def save(self, call_id: str, output: str) -> None: ...
    async def save_anchors(self, call_id: str, anchors: object) -> None: ...
    async def load(self, call_id: str) -> str | None: ...
    async def load_anchors(self, call_id: str) -> object | None: ...
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

    def _anchors_path(self, call_id: str) -> Path:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", call_id)
        return self._base_dir / f"{safe_id}.anchors.json"

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

    async def save_anchors(self, call_id: str, anchors: object) -> None:
        path = self._anchors_path(call_id)
        try:
            if anchors:
                path.write_text(json.dumps(anchors), encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "tool_output_store: failed to save anchors",
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

    async def load_anchors(self, call_id: str) -> object | None:
        path = self._anchors_path(call_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, (list, dict)) else None

    async def exists(self, call_id: str) -> bool:
        return self._path(call_id).exists()

    async def delete(self, call_id: str) -> None:
        import contextlib

        path = self._path(call_id)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        anchors_path = self._anchors_path(call_id)
        with contextlib.suppress(OSError):
            anchors_path.unlink(missing_ok=True)

    def _sync_cleanup_expired(self, ttl_seconds: int) -> int:
        deleted = 0
        now = time.time()
        try:
            for path in self._base_dir.iterdir():
                try:
                    if not path.is_file() or not path.name.endswith(".txt"):
                        continue
                    if (now - path.stat().st_mtime) > ttl_seconds:
                        path.unlink(missing_ok=True)
                        deleted += 1
                        self._base_dir.joinpath(path.stem + ".anchors.json").unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            logger.warning("tool_output_store: cleanup_expired failed", exc_info=True)
        return deleted

    async def cleanup_expired(self, ttl_seconds: int) -> int:
        return await asyncio.to_thread(self._sync_cleanup_expired, ttl_seconds)

    def _sync_enforce_size_cap(self, max_size_bytes: int) -> int:
        files: list[tuple[float, Path]] = []
        try:
            for path in self._base_dir.iterdir():
                try:
                    if path.is_file() and path.name.endswith(".txt"):
                        files.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError:
            return 0
        files.sort(key=lambda item: item[0])

        total_size = 0
        existing_files: list[Path] = []
        for _, path in files:
            try:
                total_size += path.stat().st_size
                anchors_path = self._base_dir / f"{path.stem}.anchors.json"
                if anchors_path.exists():
                    total_size += anchors_path.stat().st_size
                existing_files.append(path)
            except OSError:
                continue
        deleted = 0
        for path in existing_files:
            if total_size <= max_size_bytes:
                break
            try:
                file_size = path.stat().st_size
                path.unlink(missing_ok=True)
                total_size -= file_size
                anchors_path = self._base_dir / f"{path.stem}.anchors.json"
                if anchors_path.exists():
                    anchor_size = anchors_path.stat().st_size
                    anchors_path.unlink(missing_ok=True)
                    total_size -= anchor_size
                deleted += 1
            except OSError:
                pass
        return deleted

    async def enforce_size_cap(self, max_size_bytes: int) -> int:
        return await asyncio.to_thread(self._sync_enforce_size_cap, max_size_bytes)


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

    def _anchors_key(self, call_id: str) -> str:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", call_id)
        return f"tool-outputs/{safe_id}.anchors.json"

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

    def _sync_save_anchors(self, call_id: str, anchors: object) -> None:
        if not anchors:
            self._client.delete_object(Bucket=self._bucket, Key=self._anchors_key(call_id))
            return
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._anchors_key(call_id),
            Body=json.dumps(anchors).encode("utf-8"),
            ContentType="application/json",
        )

    async def save_anchors(self, call_id: str, anchors: object) -> None:
        try:
            await asyncio.to_thread(self._sync_save_anchors, call_id, anchors)
        except Exception:
            logger.warning(
                "tool_output_store: S3 save anchors failed",
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

    def _sync_load_anchors(self, call_id: str) -> object | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._anchors_key(call_id))
            raw = json.loads(response["Body"].read().decode("utf-8"))
        except self._client.exceptions.NoSuchKey:
            return None
        return raw if isinstance(raw, (list, dict)) else None

    async def load_anchors(self, call_id: str) -> object | None:
        try:
            return await asyncio.to_thread(self._sync_load_anchors, call_id)
        except Exception:
            logger.warning(
                "tool_output_store: S3 load anchors failed",
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
        self._client.delete_object(Bucket=self._bucket, Key=self._anchors_key(call_id))

    async def delete(self, call_id: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._sync_delete, call_id)

    def _sync_cleanup_expired(self, ttl_seconds: int) -> int:
        deleted = 0
        now = time.time()
        text_objects: dict[str, dict[str, Any]] = {}
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix="tool-outputs/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".txt"):
                    text_objects[key.removeprefix("tool-outputs/").removesuffix(".txt")] = obj
        for safe_id, obj in text_objects.items():
            last_modified = obj["LastModified"].timestamp()
            if (now - last_modified) > ttl_seconds:
                self._client.delete_object(Bucket=self._bucket, Key=f"tool-outputs/{safe_id}.txt")
                self._client.delete_object(
                    Bucket=self._bucket,
                    Key=f"tool-outputs/{safe_id}.anchors.json",
                )
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
        pairs: dict[str, dict[str, Any]] = {}
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix="tool-outputs/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".txt"):
                    safe_id = key.removeprefix("tool-outputs/").removesuffix(".txt")
                    pairs.setdefault(safe_id, {"size": 0, "last_modified": obj["LastModified"]})
                    pairs[safe_id]["size"] += obj["Size"]
                    pairs[safe_id]["last_modified"] = obj["LastModified"]
                elif key.endswith(".anchors.json"):
                    safe_id = key.removeprefix("tool-outputs/").removesuffix(".anchors.json")
                    pairs.setdefault(safe_id, {"size": 0, "last_modified": obj["LastModified"]})
                    pairs[safe_id]["size"] += obj["Size"]

        total_size = sum(int(pair["size"]) for pair in pairs.values())
        if total_size <= max_size_bytes:
            return 0

        ordered_pairs = sorted(pairs.items(), key=lambda item: item[1]["last_modified"])
        deleted = 0
        for safe_id, pair in ordered_pairs:
            if total_size <= max_size_bytes:
                break
            self._client.delete_object(Bucket=self._bucket, Key=f"tool-outputs/{safe_id}.txt")
            self._client.delete_object(
                Bucket=self._bucket, Key=f"tool-outputs/{safe_id}.anchors.json"
            )
            total_size -= int(pair["size"])
            deleted += 1
        return deleted

    async def enforce_size_cap(self, max_size_bytes: int) -> int:
        """Delete oldest objects if total size exceeds cap."""
        try:
            return await asyncio.to_thread(self._sync_enforce_size_cap, max_size_bytes)
        except Exception:
            logger.warning("tool_output_store: S3 enforce_size_cap failed", exc_info=True)
            return 0


# ---------------------------------------------------------------------------
# High-level store (backend-agnostic)
# ---------------------------------------------------------------------------


class ToolOutputStore:
    """TTL-bound storage for full tool outputs.

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

    @property
    def ttl_seconds(self) -> int:
        """Configured retention window for saved tool outputs."""

        return self._ttl_seconds

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save(
        self,
        call_id: str,
        output: str,
        *,
        anchors: list[dict[str, Any]] | None = None,
        anchor_manifest: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Save output and return the anchor manifest confirmed by round trip."""
        await self._backend.save(call_id, output)
        persisted_payload: object = (
            anchor_manifest if anchor_manifest is not None else anchors or []
        )
        await self._backend.save_anchors(call_id, persisted_payload)
        if not await self._backend.exists(call_id):
            return []
        if not anchors:
            return []
        persisted = await self._backend.load_anchors(call_id)
        if persisted != persisted_payload:
            return []
        if isinstance(persisted, dict) and isinstance(persisted.get("anchors"), list):
            return persisted["anchors"]
        return persisted if isinstance(persisted, list) else []

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

    async def list_anchors(self, call_id: str) -> list[OutputAnchor] | None:
        """Return stored anchors for a tool output."""
        content = await self._backend.load(call_id)
        if content is None:
            return None
        raw = await self._backend.load_anchors(call_id)
        if raw is None:
            return _augment_with_markdown_heading_anchors(content, _parse_inline_anchors(content))
        if isinstance(raw, dict):
            raw = raw.get("anchors")
        if not isinstance(raw, list):
            return _augment_with_markdown_heading_anchors(content, _parse_inline_anchors(content))
        anchors: list[OutputAnchor] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            anchor = item.get("anchor")
            kind = item.get("kind")
            start_line = item.get("start_line")
            end_line = item.get("end_line")
            if not isinstance(anchor, str) or not isinstance(kind, str):
                continue
            locator = item.get("locator")
            valid_lines = isinstance(start_line, int) and isinstance(end_line, int)
            if not valid_lines and not isinstance(locator, dict):
                continue
            label = item.get("label")
            format_name = item.get("format")
            recovery_op = item.get("recovery_op")
            priority = item.get("priority")
            anchors.append(
                OutputAnchor(
                    anchor=anchor,
                    label=label if isinstance(label, str) else None,
                    kind=kind,
                    start_line=start_line if isinstance(start_line, int) else None,
                    end_line=end_line if isinstance(end_line, int) else None,
                    artifact_candidate=item.get("artifact_candidate")
                    if isinstance(item.get("artifact_candidate"), dict)
                    else None,
                    anchor_id=item.get("anchor_id")
                    if isinstance(item.get("anchor_id"), str)
                    else None,
                    format=format_name if isinstance(format_name, str) else "text",
                    summary=item.get("summary") if isinstance(item.get("summary"), str) else None,
                    locator=item.get("locator") if isinstance(item.get("locator"), dict) else None,
                    recovery_op=recovery_op if isinstance(recovery_op, str) else "read_lines",
                    priority=priority if isinstance(priority, int) else 50,
                    promote=item.get("promote") is True,
                    lazy_artifact_ref=item.get("lazy_artifact_ref")
                    if isinstance(item.get("lazy_artifact_ref"), str)
                    else None,
                )
            )
        if not anchors:
            anchors = _parse_inline_anchors(content)
        return _augment_with_markdown_heading_anchors(content, anchors)

    async def read_anchor(
        self,
        call_id: str,
        anchor: str,
        *,
        before_lines: int = 0,
        after_lines: int = 0,
    ) -> AnchorReadResult | None:
        """Read a stored anchored section with optional surrounding context."""
        anchors = await self.list_anchors(call_id)
        if anchors is None:
            return None
        selected = next((item for item in anchors if item.anchor == anchor), None)
        if selected is None:
            return None
        content = await self._backend.load(call_id)
        if content is None:
            return None

        locator = selected.locator or {}
        locator_type = locator.get("type")
        if locator_type == "stored_json":
            try:
                value: Any = json.loads(content)
                for token in str(locator.get("pointer") or "").lstrip("/").split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    value = value[int(token)] if isinstance(value, list) else value[token]
                rendered = json.dumps(value, indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                return None
            return AnchorReadResult(anchor=selected, content=rendered)
        if locator_type == "artifact_part":
            return AnchorReadResult(
                anchor=selected,
                content=json.dumps({"artifact_part": locator}, ensure_ascii=False),
            )
        all_lines = content.splitlines()
        if locator_type == "stored_rows":
            start_row = int(locator.get("start_row") or 1)
            end_row = int(locator.get("end_row") or start_row)
            selected_lines = all_lines[start_row - 1 : end_row]
            return AnchorReadResult(anchor=selected, content="\n".join(selected_lines))
        if selected.start_line is None or selected.end_line is None:
            return None
        start_idx = max(0, selected.start_line - 1 - before_lines)
        end_idx = min(len(all_lines), selected.end_line + after_lines)
        numbered: list[str] = []
        for i, line in enumerate(all_lines[start_idx:end_idx], start=start_idx + 1):
            if len(line) > _MAX_LINE_LENGTH:
                line = line[:_MAX_LINE_LENGTH] + "... (line truncated)"
            numbered.append(f"{i}: {line}")
        return AnchorReadResult(anchor=selected, content="\n".join(numbered))

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

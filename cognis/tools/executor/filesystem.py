"""Executor-native filesystem tools: read, write, edit, apply_patch, multiedit, list_directory."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import hashlib
import json
import mimetypes
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from cognis.logging import get_logger
from cognis.models.tool import ToolResult
from cognis.tools.executor.file_freshness import get_file_freshness_tracker
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

if TYPE_CHECKING:
    from cognis.tools.executor.lsp.manager import LSPManager

logger = get_logger(__name__)

_MAX_READ_LINES = 2000
_MAX_LINE_LENGTH = 2000
_MAX_INLINE_BINARY_READ_BYTES = 10 * 1024 * 1024
_TEXTUAL_BINARY_MIME_TYPES = {"image/svg+xml"}


def _resolve_path(raw: str, context: ToolExecutionContext) -> Path:
    """Resolve a user-provided path, expanding ``~`` and environment variables."""
    return resolve_path(raw, context=context)


# Canonical key for LSPManager in ToolExecutionContext.runtime_metadata
_LSP_MANAGER_KEY = "lsp_manager"  # Must match LSP_MANAGER_KEY from lsp package


async def _record_read(context: ToolExecutionContext, path: Path) -> None:
    tracker = get_file_freshness_tracker(context.runtime_metadata)
    await tracker.record_read(tracker.scope_id(context), path)


async def _record_write(context: ToolExecutionContext, path: Path) -> None:
    tracker = get_file_freshness_tracker(context.runtime_metadata)
    await tracker.record_write(tracker.scope_id(context), path)


def _remove_tracked_path(context: ToolExecutionContext, path: Path) -> None:
    tracker = get_file_freshness_tracker(context.runtime_metadata)
    tracker.remove_path(tracker.scope_id(context), path)


async def _move_tracked_path(
    context: ToolExecutionContext, source: Path, destination: Path
) -> None:
    tracker = get_file_freshness_tracker(context.runtime_metadata)
    await tracker.move_path(tracker.scope_id(context), source, destination)


async def _assert_can_modify_existing(context: ToolExecutionContext, path: Path) -> None:
    tracker = get_file_freshness_tracker(context.runtime_metadata)
    await tracker.assert_can_modify_existing(tracker.scope_id(context), path)


async def _with_file_lock(
    context: ToolExecutionContext,
    path: Path,
    operation: Callable[[], Awaitable[ToolResult]],
) -> ToolResult:
    tracker = get_file_freshness_tracker(context.runtime_metadata)
    async with tracker.lock_for(path):
        return await operation()


@contextlib.asynccontextmanager
async def _with_file_locks(context: ToolExecutionContext, paths: list[Path]):
    tracker = get_file_freshness_tracker(context.runtime_metadata)
    async with tracker.locks_for(paths):
        yield


@dataclass(slots=True)
class _PatchHunk:
    old_text: str
    new_text: str
    old_start: int | None = None
    change_context: str | None = None
    is_end_of_file: bool = False


@dataclass(slots=True)
class _PatchOperation:
    kind: str
    source_path: Path | None = None
    destination_path: Path | None = None
    hunks: list[_PatchHunk] = field(default_factory=list)
    add_content: str = ""


@dataclass(slots=True)
class _StagedPatchOperation:
    kind: str
    source_path: Path | None = None
    destination_path: Path | None = None
    previous_content: str = ""
    content: str | None = None


class PatchFormatError(ValueError):
    """Raised when patch input is syntactically invalid."""


class PatchConflictError(ValueError):
    """Raised when a patch is semantically invalid for the current workspace."""


async def _collect_lsp_diagnostics(
    context: ToolExecutionContext,
    file_path: str,
) -> str:
    """Touch LSP and return formatted diagnostics, or empty string.

    This is best-effort: any LSP failure returns an empty string so
    that the file operation result is never affected.
    """
    import os

    lsp: LSPManager | None = context.runtime_metadata.get(_LSP_MANAGER_KEY)
    if lsp is None:
        return ""
    try:
        abs_path = os.path.abspath(file_path)
        await lsp.touch_file(abs_path, wait=True)
        diagnostics = lsp.get_diagnostics(abs_path)
        if not diagnostics:
            return ""
        from cognis.tools.executor.lsp.diagnostics import format_diagnostics_for_llm

        return format_diagnostics_for_llm(diagnostics, abs_path)
    except Exception:
        logger.debug(
            "lsp: diagnostics collection failed",
            extra={"extra_data": {"file_path": file_path}},
        )
        return ""


async def _collect_lsp_diagnostics_batch(
    context: ToolExecutionContext,
    file_paths: list[str],
) -> str:
    """Collect LSP diagnostics for multiple files concurrently."""
    lsp: LSPManager | None = context.runtime_metadata.get(_LSP_MANAGER_KEY)
    if lsp is None or not file_paths:
        return ""
    try:
        # Touch all files concurrently
        await asyncio.gather(
            *(lsp.touch_file(fp, wait=True) for fp in file_paths),
            return_exceptions=True,
        )
        # Collect all diagnostics
        all_diagnostics: dict[str, list[Any]] = {}
        for fp in file_paths:
            diags = lsp.get_diagnostics(fp)
            for path, path_diags in diags.items():
                existing = all_diagnostics.get(path, [])
                existing.extend(path_diags)
                all_diagnostics[path] = existing

        if not all_diagnostics:
            return ""

        from cognis.tools.executor.lsp.diagnostics import format_diagnostics_for_llm

        # Use the first file as the "edited file" for formatting
        return format_diagnostics_for_llm(all_diagnostics, file_paths[0])
    except Exception:
        logger.debug(
            "lsp: batch diagnostics collection failed",
            extra={"extra_data": {"file_count": len(file_paths)}},
        )
        return ""


def _should_wait_for_edit_diagnostics(context: ToolExecutionContext, file_paths: list[str]) -> bool:
    """Return whether edit tools should synchronously wait for diagnostics."""
    lsp: LSPManager | None = context.runtime_metadata.get(_LSP_MANAGER_KEY)
    if lsp is None:
        return False
    return not (lsp.has_pending_diagnostics(file_paths) or lsp.has_cached_diagnostics(file_paths))


def _warm_lsp(context: ToolExecutionContext, file_path: str) -> None:
    """Warm LSP for a file (non-blocking, no wait for diagnostics).

    Used by the read tool so that subsequent edits get faster diagnostics.
    """
    lsp: LSPManager | None = context.runtime_metadata.get(_LSP_MANAGER_KEY)
    if lsp is None:
        return

    async def _warm() -> None:
        with contextlib.suppress(Exception):
            await lsp.touch_file(file_path, wait=False)

    asyncio.create_task(_warm())


def _warm_lsp_batch(context: ToolExecutionContext, file_paths: list[str]) -> None:
    """Warm LSP for multiple files without blocking the edit result."""
    lsp: LSPManager | None = context.runtime_metadata.get(_LSP_MANAGER_KEY)
    if lsp is None or not file_paths:
        return

    async def _warm() -> None:
        await asyncio.gather(
            *(lsp.touch_file(path, wait=False) for path in file_paths),
            return_exceptions=True,
        )

    asyncio.create_task(_warm())


def _format_cached_lsp_diagnostics(context: ToolExecutionContext, file_paths: list[str]) -> str:
    """Return already available diagnostics without touching or waiting on LSP."""
    lsp: LSPManager | None = context.runtime_metadata.get(_LSP_MANAGER_KEY)
    if lsp is None or not file_paths:
        return ""

    all_diagnostics: dict[str, list[Any]] = {}
    for path in file_paths:
        for diag_path, path_diags in lsp.get_diagnostics(path).items():
            existing = all_diagnostics.get(diag_path, [])
            existing.extend(path_diags)
            all_diagnostics[diag_path] = existing

    if not all_diagnostics:
        return ""

    from cognis.tools.executor.lsp.diagnostics import format_diagnostics_for_llm

    return format_diagnostics_for_llm(all_diagnostics, file_paths[0])


_DEFAULT_IGNORE = {
    "node_modules",
    "__pycache__",
    ".git",
    "dist",
    "build",
    "target",
    "vendor",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
}

_PRETTIER_EXTENSIONS = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".json",
    ".md",
    ".css",
    ".scss",
    ".html",
    ".yaml",
    ".yml",
}


async def _maybe_format_file(path: Path) -> bool:
    command = _formatter_command(path)
    if command is None:
        return False
    before = path.read_bytes()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(path.parent),
        )
        await asyncio.wait_for(process.communicate(), timeout=20)
        after = path.read_bytes()
        return before != after
    except Exception:
        logger.debug(
            "formatter: file format failed",
            extra={"extra_data": {"file_path": str(path)}},
        )
        return False


def _formatter_command(path: Path) -> list[str] | None:
    suffix = path.suffix.lower()
    if suffix == ".py":
        ruff = shutil.which("ruff")
        if ruff:
            return [ruff, "format", str(path)]
        return None
    if suffix not in _PRETTIER_EXTENSIONS:
        return None
    prettier = _find_prettier_binary(path.parent)
    if prettier is None:
        return None
    return [prettier, "--write", str(path)]


def _find_prettier_binary(start_dir: Path) -> str | None:
    for directory in [start_dir, *start_dir.parents]:
        local = directory / "node_modules" / ".bin" / "prettier"
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
        if (directory / "package.json").is_file():
            break
    global_prettier = shutil.which("prettier")
    return global_prettier


def _unified_diff(path: Path, before: str, after: str) -> str:
    if before == after:
        return ""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def _files_written_metadata(
    paths: list[Path], diffs: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"files_written": [str(path) for path in paths]}
    if diffs:
        metadata["file_diffs"] = diffs
    return metadata


async def handle_read(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Read a file or directory, returning line-numbered content for text."""
    file_path = arguments.get("file_path", "")
    offset = max(1, int(arguments.get("offset", 1)))
    limit = int(arguments.get("limit", _MAX_READ_LINES))

    try:
        path = _resolve_path(file_path, context)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    if not path.exists():
        return ToolResult(output=f"Path does not exist: {file_path}", is_error=True)

    if path.is_dir():
        return _read_directory(path)

    try:
        raw_content = path.read_bytes()
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Cannot read file: {exc}", is_error=True)

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if _should_route_binary_read(path, raw_content, mime_type):
        await _record_read(context, path)
        return _binary_read_result(path, raw_content, mime_type)

    content = raw_content.decode("utf-8", errors="replace")

    lines = content.splitlines(keepends=True)
    total = len(lines)
    selected = lines[offset - 1 : offset - 1 + limit]

    output_lines: list[str] = []
    for i, line in enumerate(selected, start=offset):
        line_content = line.rstrip("\n\r")
        if len(line_content) > _MAX_LINE_LENGTH:
            line_content = line_content[:_MAX_LINE_LENGTH] + "..."
        output_lines.append(f"{i}: {line_content}")

    result = "\n".join(output_lines)
    if offset + limit - 1 < total:
        result += f"\n\n(Showing lines {offset}-{offset + len(selected) - 1} of {total}. Use offset={offset + limit} to continue.)"

    # Warm LSP for subsequent edits (non-blocking)
    if path.is_file():
        await _record_read(context, path)
        _warm_lsp(context, str(path))

    return ToolResult(output=result, metadata={"files_read": [str(path)]})


def _should_route_binary_read(path: Path, content: bytes, mime_type: str) -> bool:
    normalized_mime = mime_type.lower()
    if normalized_mime in _TEXTUAL_BINARY_MIME_TYPES:
        return False
    if normalized_mime.startswith(("image/", "audio/", "video/")):
        return True
    if normalized_mime == "application/pdf":
        return True
    if b"\x00" in content:
        return True
    suffix = path.suffix.lower()
    return suffix in {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".mp3",
        ".wav",
        ".ogg",
        ".m4a",
        ".mp4",
        ".mov",
        ".webm",
    }


def _binary_read_result(path: Path, content: bytes, mime_type: str) -> ToolResult:
    if len(content) > _MAX_INLINE_BINARY_READ_BYTES:
        return ToolResult(
            output=(
                f"Binary file is too large to read inline ({len(content)} bytes). "
                "Use artifact_publish first if you need to inspect it."
            ),
            is_error=True,
            metadata={"files_read": [str(path)]},
        )
    return ToolResult(
        output=(
            f"Prepared binary file '{path.name}' for attachment analysis. "
            "The controller will inspect it with the current model when possible, "
            "and fall back to the attachment_analysis route when needed."
        ),
        metadata={
            "files_read": [str(path)],
            "attachment_analysis_request": {"source": "read", "path": str(path)},
        },
        attachments=[
            {
                "filename": path.name,
                "mime_type": mime_type,
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        ],
    )


def _read_directory(path: Path) -> ToolResult:
    """List directory entries."""
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Cannot read directory: {exc}", is_error=True)

    lines: list[str] = []
    for entry in entries[:200]:
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{entry.name}{suffix}")
    if len(entries) > 200:
        lines.append(f"... and {len(entries) - 200} more entries")
    return ToolResult(output="\n".join(lines))


async def handle_write(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Write content to a file, creating parent directories if needed."""
    file_path = arguments.get("file_path", "")
    content = arguments.get("content", "")

    try:
        path = _resolve_path(file_path, context)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    async def _write() -> ToolResult:
        exists = path.exists()
        before = ""
        content_to_write = content
        if exists:
            try:
                await _assert_can_modify_existing(context, path)
                before = _read_text_preserving_newlines(path) if path.is_file() else ""
                if path.is_file():
                    content_to_write = _normalize_text_for_newline(content, _detect_newline(before))
            except RuntimeError as exc:
                return ToolResult(output=str(exc), is_error=True)
            except (OSError, PermissionError, UnicodeDecodeError) as exc:
                return ToolResult(output=f"Cannot read file before write: {exc}", is_error=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content_to_write, encoding="utf-8", newline="")
            formatter_changed = await _maybe_format_file(path)
            final_content = _read_text_preserving_newlines(path)
            if formatter_changed:
                _remove_tracked_path(context, path)
            else:
                await _record_write(context, path)
        except (OSError, PermissionError, UnicodeDecodeError) as exc:
            return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

        output = f"Wrote {len(content_to_write.encode('utf-8'))} bytes to {file_path}"
        diagnostics_text = await _collect_lsp_diagnostics(context, file_path)
        if diagnostics_text:
            output += f"\n\n{diagnostics_text}"
        return ToolResult(
            output=output,
            metadata=_files_written_metadata(
                [path], [{"path": str(path), "diff": _unified_diff(path, before, final_content)}]
            ),
        )

    return await _with_file_lock(context, path, _write)


async def handle_artifact_save(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Save a controller-resolved Cognis artifact to the executor filesystem."""
    file_path = arguments.get("file_path", "")
    content_b64 = arguments.get("source_artifact_content_b64")

    if not isinstance(content_b64, str) or not content_b64.strip():
        return ToolResult(
            output=(
                "artifact_save requires controller-resolved artifact content. "
                "Provide source_artifact_id so the controller can resolve it first."
            ),
            is_error=True,
        )

    try:
        content = base64.b64decode(content_b64, validate=True)
    except Exception:
        return ToolResult(output="artifact_save received invalid artifact content.", is_error=True)

    try:
        path = _resolve_path(file_path, context)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    async def _write_binary() -> ToolResult:
        exists = path.exists()
        if exists:
            try:
                await _assert_can_modify_existing(context, path)
            except RuntimeError as exc:
                return ToolResult(output=str(exc), is_error=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            await _record_write(context, path)
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

        source_artifact_id = str(arguments.get("source_artifact_id") or "")
        source_filename = str(arguments.get("source_artifact_filename") or "")
        mime_type = str(arguments.get("source_artifact_mime_type") or "application/octet-stream")
        output = {
            "saved_path": str(path),
            "source_artifact_id": source_artifact_id or None,
            "source_filename": source_filename or None,
            "mime_type": mime_type,
            "size_bytes": len(content),
        }
        return ToolResult(output=str(output), metadata={"files_written": [str(path)]})

    return await _with_file_lock(context, path, _write_binary)


async def handle_skill_asset_materialize(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Materialize a skill asset to an executor-local path."""

    skill_id = str(arguments.get("skill_id") or "").strip()
    asset_id = str(arguments.get("asset_id") or "").strip()
    filename_arg = str(arguments.get("filename") or "").strip()
    if not skill_id:
        return ToolResult(output="skill_id is required", is_error=True)

    asset = _find_skill_asset(context, skill_id, asset_id=asset_id, filename=filename_arg)
    if asset is None:
        return ToolResult(
            output=(
                "Skill asset not found in this executor runtime. Call skill_load and use an "
                "asset_id from asset_manifest, or ensure the executor has been reconfigured."
            ),
            is_error=True,
        )

    filename = str(asset.get("filename") or "").strip()
    if not filename:
        return ToolResult(output="Skill asset is missing filename metadata", is_error=True)

    try:
        content = await _load_skill_asset_content(asset, context)
    except Exception as exc:
        return ToolResult(output=f"Failed to load skill asset: {exc}", is_error=True)

    expected_hash = str(asset.get("content_hash") or "").strip()
    if expected_hash:
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != expected_hash:
            return ToolResult(output=f"Asset hash mismatch for {filename}", is_error=True)

    target_path = str(arguments.get("target_path") or "").strip()
    try:
        if target_path:
            path = _resolve_path(target_path, context)
            if path.exists() and path.is_dir():
                path = _resolve_asset_target_in_directory(path, filename)
        else:
            path = _default_skill_asset_path(skill_id, asset, filename)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    async def _write_asset() -> ToolResult:
        if path.exists():
            try:
                await _assert_can_modify_existing(context, path)
            except RuntimeError as exc:
                return ToolResult(output=str(exc), is_error=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            await _record_write(context, path)
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot materialize skill asset: {exc}", is_error=True)

        output = {
            "local_path": str(path),
            "skill_id": skill_id,
            "asset_id": asset.get("asset_id"),
            "filename": filename,
            "content_type": asset.get("content_type") or "application/octet-stream",
            "size_bytes": len(content),
            "content_hash": hashlib.sha256(content).hexdigest(),
        }
        return ToolResult(
            output=json.dumps(output, sort_keys=True),
            metadata={"files_written": [str(path)], "skill_asset": output},
        )

    return await _with_file_lock(context, path, _write_asset)


def _find_skill_asset(
    context: ToolExecutionContext,
    skill_id: str,
    *,
    asset_id: str,
    filename: str,
) -> dict[str, Any] | None:
    manifests = context.runtime_metadata.get("skill_manifests")
    if not isinstance(manifests, list):
        manifests = []
    candidates: list[dict[str, Any]] = []
    for manifest in manifests:
        if not isinstance(manifest, dict) or manifest.get("skill_id") != skill_id:
            continue
        for raw_asset in manifest.get("asset_manifest") or []:
            if isinstance(raw_asset, dict):
                candidates.append(raw_asset)
    if asset_id:
        for asset in candidates:
            if str(asset.get("asset_id") or "") == asset_id:
                return asset
        return None
    if filename:
        for asset in candidates:
            if str(asset.get("filename") or "") == filename:
                return asset
        return None
    return candidates[0] if len(candidates) == 1 else None


async def _load_skill_asset_content(asset: dict[str, Any], context: ToolExecutionContext) -> bytes:
    content_b64 = asset.get("content_b64")
    if isinstance(content_b64, str) and content_b64.strip():
        return base64.b64decode(content_b64, validate=True)

    url = str(asset.get("url") or "").strip()
    if url:
        import httpx

        _validate_skill_asset_url(url, context)
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                response = exc.response
                body = response.text[:200].replace("\n", " ").strip()
                detail = f"HTTP {response.status_code}"
                if body:
                    detail = f"{detail}: {body}"
                raise ValueError(
                    f"failed to fetch controller-provided asset URL ({detail})"
                ) from exc
            except Exception as exc:
                raise ValueError("failed to fetch controller-provided asset URL") from exc
            return response.content

    artifact_store = None
    if context.shared_runtime_metadata is not None:
        artifact_store = context.shared_runtime_metadata.get("artifact_store")
    artifact_store = artifact_store or context.runtime_metadata.get("artifact_store")
    artifact_oid = str(asset.get("artifact_object_id") or "").strip()
    filename = str(asset.get("filename") or "").strip()
    if artifact_store is not None and artifact_oid and filename:
        namespace = str(asset.get("artifact_namespace") or "skills")
        content, _content_type = await artifact_store.async_load(namespace, artifact_oid, filename)
        return content

    raise ValueError("asset has no materializable content, URL, or artifact store reference")


def _default_skill_asset_path(skill_id: str, asset: dict[str, Any], filename: str) -> Path:
    safe_skill = re.sub(r"[^a-zA-Z0-9_.-]+", "_", skill_id).strip("._") or "skill"
    safe_asset = (
        re.sub(
            r"[^a-zA-Z0-9_.-]+",
            "_",
            str(asset.get("asset_id") or asset.get("content_hash") or "asset"),
        ).strip("._")
        or "asset"
    )
    data_dir = Path(os.environ.get("COGNIS_DATA_DIR") or "~/.cognis").expanduser()
    root = data_dir / "skill_assets" / safe_skill / safe_asset
    target = (root / filename).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Unsafe skill asset filename rejected: {filename}")
    return target


def _resolve_asset_target_in_directory(directory: Path, filename: str) -> Path:
    root = directory.resolve()
    target = (root / filename).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Unsafe skill asset filename rejected: {filename}")
    return target


def _validate_skill_asset_url(url: str, context: ToolExecutionContext) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("skill asset URL must be an HTTP(S) controller URL")
    controller_url = str(context.runtime_metadata.get("controller_url") or "").strip()
    if not controller_url:
        raise ValueError("skill asset URL requires a configured controller origin")
    controller = urlparse(
        controller_url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
    )
    if controller.netloc and parsed.netloc != controller.netloc:
        raise ValueError("skill asset URL host does not match the configured controller")


async def handle_edit(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Edit a file by replacing exact text matches."""
    file_path = arguments.get("file_path", "")
    old_string = arguments.get("old_string", "")
    new_string = arguments.get("new_string", "")
    replace_all = bool(arguments.get("replace_all", False))

    if old_string == new_string:
        return ToolResult(output="old_string and new_string are identical.", is_error=True)

    try:
        path = _resolve_path(file_path, context)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    if not path.is_file():
        return ToolResult(output=f"File not found: {file_path}", is_error=True)

    async def _edit() -> ToolResult:
        try:
            await _assert_can_modify_existing(context, path)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                content = handle.read()
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot read file: {exc}", is_error=True)

        newline = _detect_newline(content)
        normalized_old = _normalize_text_for_newline(old_string, newline)
        normalized_new = _normalize_text_for_newline(new_string, newline)

        count = content.count(normalized_old)
        if count == 0:
            return ToolResult(output="oldString not found in content.", is_error=True)
        if count > 1 and not replace_all:
            return ToolResult(
                output=f"Found {count} matches for oldString. Use replace_all=true or provide more context to make the match unique.",
                is_error=True,
            )

        new_content = (
            content.replace(normalized_old, normalized_new)
            if replace_all
            else content.replace(normalized_old, normalized_new, 1)
        )
        try:
            path.write_text(new_content, encoding="utf-8", newline="")
            formatter_changed = await _maybe_format_file(path)
            final_content = _read_text_preserving_newlines(path)
            if formatter_changed:
                _remove_tracked_path(context, path)
            else:
                await _record_write(context, path)
        except (OSError, PermissionError, UnicodeDecodeError) as exc:
            return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

        replacements = count if replace_all else 1
        output = f"Replaced {replacements} occurrence(s) in {file_path}"
        if formatter_changed:
            output += " (reformatted by formatter)"
        diagnostics_text = await _collect_lsp_diagnostics(context, file_path)
        if diagnostics_text:
            output += f"\n\n{diagnostics_text}"
        return ToolResult(
            output=output,
            metadata=_files_written_metadata(
                [path], [{"path": str(path), "diff": _unified_diff(path, content, final_content)}]
            ),
        )

    return await _with_file_lock(context, path, _edit)


async def handle_apply_patch(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Apply an apply_patch envelope, native operation, or unified diff patch."""
    patch_text = arguments.get("patchText") or ""
    operation = arguments.get("operation")
    if not patch_text.strip() and not isinstance(operation, dict):
        return ToolResult(output="Empty apply_patch payload.", is_error=True)

    try:
        if patch_text.strip():
            operations = _parse_patch_operations(patch_text, context)
        elif isinstance(operation, dict):
            operations = _parse_native_apply_patch_operation(operation, context)
        else:
            operations = []
    except (
        PatchFormatError,
        PatchConflictError,
        RuntimeError,
        OSError,
        PermissionError,
        ValueError,
    ) as exc:
        return ToolResult(output=str(exc), is_error=True)

    touched_paths = _operation_lock_paths(operations)
    try:
        async with _with_file_locks(context, touched_paths):
            staged = await _stage_patch_operations(operations, context)
            # Re-stage once more immediately before apply so we catch files
            # that changed after the initial read but before the write phase.
            staged = await _stage_patch_operations(operations, context)
            summary_lines, diagnostic_paths, file_diffs = await _apply_staged_patch_operations(
                staged, context
            )
    except (
        PatchFormatError,
        PatchConflictError,
        RuntimeError,
        OSError,
        PermissionError,
        ValueError,
    ) as exc:
        return ToolResult(output=str(exc), is_error=True)

    if not summary_lines:
        return ToolResult(output="No files were patched.", is_error=True)

    diagnostics_targets = [str(path) for path in diagnostic_paths]
    if diagnostics_targets:
        if _should_wait_for_edit_diagnostics(context, diagnostics_targets):
            diagnostics_text = await _collect_lsp_diagnostics_batch(context, diagnostics_targets)
        else:
            _warm_lsp_batch(context, diagnostics_targets)
            diagnostics_text = ""
        if diagnostics_text:
            summary_lines.append(diagnostics_text)

    stale_diagnostics = _format_cached_lsp_diagnostics(context, diagnostics_targets)
    if stale_diagnostics:
        summary_lines.append(stale_diagnostics)

    return ToolResult(
        output="\n".join(summary_lines),
        metadata=_files_written_metadata(diagnostic_paths, file_diffs),
    )


async def handle_multiedit(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Apply multiple sequential edits to a single file."""
    file_path = arguments.get("file_path", "")
    edits = arguments.get("edits", [])

    if not isinstance(edits, list) or not edits:
        return ToolResult(output="No edits provided.", is_error=True)

    try:
        path = _resolve_path(file_path, context)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    if not path.is_file():
        return ToolResult(output=f"File not found: {file_path}", is_error=True)

    async def _multiedit() -> ToolResult:
        try:
            await _assert_can_modify_existing(context, path)
            with path.open("r", encoding="utf-8", newline="") as handle:
                content = handle.read()
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot read file: {exc}", is_error=True)

        newline = _detect_newline(content)
        original_content = content

        applied = 0
        for i, edit in enumerate(edits):
            old_string = edit.get("old_string", "")
            new_string = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all", False))

            if old_string == new_string:
                continue
            normalized_old = _normalize_text_for_newline(old_string, newline)
            normalized_new = _normalize_text_for_newline(new_string, newline)
            count = content.count(normalized_old)
            if count == 0:
                return ToolResult(
                    output=f"Edit {i + 1}: oldString not found in content.", is_error=True
                )
            if count > 1 and not replace_all:
                return ToolResult(
                    output=f"Edit {i + 1}: Found {count} matches. Use replace_all or provide more context.",
                    is_error=True,
                )
            content = (
                content.replace(normalized_old, normalized_new)
                if replace_all
                else content.replace(normalized_old, normalized_new, 1)
            )
            applied += 1

        try:
            path.write_text(content, encoding="utf-8", newline="")
            formatter_changed = await _maybe_format_file(path)
            final_content = _read_text_preserving_newlines(path)
            if formatter_changed:
                _remove_tracked_path(context, path)
            else:
                await _record_write(context, path)
        except (OSError, PermissionError, UnicodeDecodeError) as exc:
            return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

        output = f"Applied {applied} edit(s) to {file_path}"
        if formatter_changed:
            output += " (reformatted by formatter)"
        diagnostics_text = await _collect_lsp_diagnostics(context, file_path)
        if diagnostics_text:
            output += f"\n\n{diagnostics_text}"
        return ToolResult(
            output=output,
            metadata=_files_written_metadata(
                [path],
                [{"path": str(path), "diff": _unified_diff(path, original_content, final_content)}],
            ),
        )

    return await _with_file_lock(context, path, _multiedit)


async def handle_list_directory(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """List directory contents with optional ignore patterns."""
    dir_path = arguments.get("path")
    ignore_patterns = arguments.get("ignore") or []

    try:
        path = resolve_path(dir_path, context=context, default_to_home=True)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    if not path.is_dir():
        return ToolResult(output=f"Not a directory: {dir_path}", is_error=True)

    ignore_set = _DEFAULT_IGNORE | set(ignore_patterns)

    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Cannot read directory: {exc}", is_error=True)

    lines: list[str] = []
    for entry in entries:
        if entry.name in ignore_set:
            continue
        if any(_glob_match(entry.name, pat) for pat in ignore_patterns):
            continue
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{entry.name}{suffix}")
        if len(lines) >= 200:
            lines.append(f"... (truncated, {len(entries)} total entries)")
            break

    return ToolResult(output="\n".join(lines) if lines else "(empty directory)")


def _glob_match(name: str, pattern: str) -> bool:
    """Simple glob match for ignore patterns."""
    from fnmatch import fnmatchcase

    return fnmatchcase(name, pattern)


def _canonicalize_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _line_stripped(line: str) -> str:
    return line.rstrip("\r\n")


def _is_patch_envelope_header(stripped: str) -> bool:
    return stripped in {"*** End Patch", "*** End of File"} or stripped.startswith(
        ("*** Update File: ", "*** Add File: ", "*** Delete File: ", "*** Move to: ")
    )


def _is_no_newline_marker(stripped: str) -> bool:
    return stripped == "\\ No newline at end of file"


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _normalize_text_for_newline(text: str, newline: str) -> str:
    if newline == "\n":
        return text.replace("\r\n", "\n")
    return text.replace("\r\n", "\n").replace("\n", newline)


def _normalize_patch_text_for_newline(text: str, newline: str) -> str:
    if newline == "\n":
        return text
    return text.replace("\n", newline)


def _strip_one_trailing_newline(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _strip_last_part_newline(parts: list[str]) -> None:
    if parts:
        parts[-1] = _strip_one_trailing_newline(parts[-1])


def _apply_no_newline_marker(
    old_parts: list[str], new_parts: list[str], last_line_kind: str | None
) -> None:
    if last_line_kind == "-":
        _strip_last_part_newline(old_parts)
    elif last_line_kind == "+":
        _strip_last_part_newline(new_parts)
    elif last_line_kind == " ":
        _strip_last_part_newline(old_parts)
        _strip_last_part_newline(new_parts)


def _read_text_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _read_text_file(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except (OSError, PermissionError) as exc:
        raise PatchConflictError(f"Cannot read file: {path}: {exc}") from exc
    if b"\x00" in raw:
        raise PatchConflictError(f"Patch only supports UTF-8 text files: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchConflictError(f"Patch only supports UTF-8 text files: {path}") from exc


def _parse_native_apply_patch_operation(
    operation: dict[str, Any], context: ToolExecutionContext
) -> list[_PatchOperation]:
    operation_type = str(operation.get("type") or "").strip()
    raw_path = str(operation.get("path") or "").strip()
    if not raw_path:
        raise PatchFormatError("Native apply_patch operation requires a path.")
    path = _canonicalize_path(_resolve_path(raw_path, context))

    if operation_type == "delete_file":
        return [_PatchOperation(kind="delete", source_path=path)]

    if operation_type == "create_file":
        diff = str(operation.get("diff") or "")
        return [
            _PatchOperation(
                kind="add",
                destination_path=path,
                add_content=_native_apply_patch_create_content(diff),
            )
        ]

    if operation_type == "update_file":
        diff = str(operation.get("diff") or "")
        if not diff.strip():
            raise PatchFormatError("Native apply_patch update_file operation requires a diff.")
        return [
            _PatchOperation(
                kind="update",
                source_path=path,
                destination_path=path,
                hunks=_parse_native_apply_patch_hunks(diff),
            )
        ]

    raise PatchFormatError(
        "Unsupported native apply_patch operation type. Expected create_file, update_file, or delete_file."
    )


def _parse_native_apply_patch_hunks(diff: str) -> list[_PatchHunk]:
    lines = diff.splitlines(keepends=True)
    hunks: list[_PatchHunk] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("@@"):
            raise PatchFormatError("Native apply_patch update_file diff must use @@ hunks.")
        hunk, index = _parse_patch_envelope_hunk(lines, index)
        hunks.append(hunk)
    if not hunks:
        raise PatchFormatError("Native apply_patch update_file diff did not contain a hunk.")
    return hunks


def _native_apply_patch_create_content(diff: str) -> str:
    if not diff:
        return ""
    lines = diff.splitlines(keepends=True)
    if lines and lines[0].startswith("@@"):
        hunks = _parse_native_apply_patch_hunks(diff)
        return "".join(hunk.new_text for hunk in hunks)
    content_parts: list[str] = []
    for line in lines:
        stripped = _line_stripped(line)
        if _is_no_newline_marker(stripped):
            _strip_last_part_newline(content_parts)
            continue
        if line.startswith(("+", " ")):
            content_parts.append(line[1:])
        else:
            raise PatchFormatError(
                "Native apply_patch create_file diff must use @@ hunks or + lines."
            )
    return "".join(content_parts)


def _parse_patch_operations(
    patch_text: str, context: ToolExecutionContext
) -> list[_PatchOperation]:
    stripped = patch_text.lstrip()
    if stripped.startswith("*** Begin Patch"):
        return _parse_patch_envelope(patch_text, context)
    return _parse_unified_diff(patch_text, context)


def _parse_patch_envelope(patch_text: str, context: ToolExecutionContext) -> list[_PatchOperation]:
    lines = patch_text.splitlines(keepends=True)
    if not lines or _line_stripped(lines[0]) != "*** Begin Patch":
        raise PatchFormatError("apply_patch must start with `*** Begin Patch`.")

    operations: list[_PatchOperation] = []
    index = 1
    while index < len(lines):
        stripped = _line_stripped(lines[index])
        if stripped == "*** End Patch":
            trailing = [line for line in lines[index + 1 :] if line.strip()]
            if trailing:
                raise PatchFormatError("Unexpected content after `*** End Patch`.")
            return operations
        if stripped == "*** End of File":
            index += 1
            continue
        if stripped.startswith("*** Update File: "):
            raw_path = stripped[len("*** Update File: ") :].strip()
            if not raw_path:
                raise PatchFormatError("`*** Update File:` requires a path.")
            operation = _PatchOperation(
                kind="update",
                source_path=_canonicalize_path(_resolve_path(raw_path, context)),
                destination_path=_canonicalize_path(_resolve_path(raw_path, context)),
            )
            index += 1
            if index < len(lines):
                move_line = _line_stripped(lines[index])
                if move_line.startswith("*** Move to: "):
                    move_path = move_line[len("*** Move to: ") :].strip()
                    if not move_path:
                        raise PatchFormatError("`*** Move to:` requires a path.")
                    operation.kind = "move"
                    operation.destination_path = _canonicalize_path(
                        _resolve_path(move_path, context)
                    )
                    index += 1

            while index < len(lines):
                stripped = _line_stripped(lines[index])
                if stripped == "*** End of File":
                    index += 1
                    continue
                if _is_patch_envelope_header(stripped) and not stripped.startswith("@@"):
                    break
                if not lines[index].startswith("@@"):
                    raise PatchFormatError(
                        f"Unexpected line in update patch: {stripped or '<blank>'}"
                    )
                hunk, index = _parse_patch_envelope_hunk(lines, index)
                operation.hunks.append(hunk)

            if not operation.hunks and operation.kind != "move":
                raise PatchFormatError("`*** Update File:` requires at least one hunk.")
            operations.append(operation)
            continue

        if stripped.startswith("*** Add File: "):
            raw_path = stripped[len("*** Add File: ") :].strip()
            if not raw_path:
                raise PatchFormatError("`*** Add File:` requires a path.")
            operation = _PatchOperation(
                kind="add",
                destination_path=_canonicalize_path(_resolve_path(raw_path, context)),
            )
            index += 1
            content_parts: list[str] = []
            while index < len(lines):
                stripped = _line_stripped(lines[index])
                if stripped == "*** End of File":
                    index += 1
                    continue
                if _is_patch_envelope_header(stripped):
                    break
                if _is_no_newline_marker(stripped):
                    _strip_last_part_newline(content_parts)
                    index += 1
                    continue
                if not lines[index].startswith("+"):
                    raise PatchFormatError("`*** Add File:` only accepts `+` lines.")
                content_parts.append(lines[index][1:])
                index += 1
            operation.add_content = "".join(content_parts)
            operations.append(operation)
            continue

        if stripped.startswith("*** Delete File: "):
            raw_path = stripped[len("*** Delete File: ") :].strip()
            if not raw_path:
                raise PatchFormatError("`*** Delete File:` requires a path.")
            operation = _PatchOperation(
                kind="delete",
                source_path=_canonicalize_path(_resolve_path(raw_path, context)),
            )
            index += 1
            while index < len(lines):
                stripped = _line_stripped(lines[index])
                if stripped == "*** End of File":
                    index += 1
                    continue
                if _is_patch_envelope_header(stripped):
                    break
                raise PatchFormatError("`*** Delete File:` does not accept body content.")
            operations.append(operation)
            continue

        raise PatchFormatError(f"Unknown patch header: {stripped or '<blank>'}")

    raise PatchFormatError("apply_patch is missing `*** End Patch`.")


def _parse_patch_envelope_hunk(lines: list[str], start_index: int) -> tuple[_PatchHunk, int]:
    header = _line_stripped(lines[start_index])
    change_context = None
    if header.startswith("@@ "):
        change_context = header[3:]
    index = start_index + 1
    old_parts: list[str] = []
    new_parts: list[str] = []
    last_line_kind: str | None = None
    is_end_of_file = False
    while index < len(lines):
        stripped = _line_stripped(lines[index])
        if lines[index].startswith("@@") or _is_patch_envelope_header(stripped):
            if stripped == "*** End of File":
                is_end_of_file = True
                index += 1
            break
        if _is_no_newline_marker(stripped):
            _apply_no_newline_marker(old_parts, new_parts, last_line_kind)
            index += 1
            continue
        line = lines[index]
        if line.startswith("-"):
            old_parts.append(line[1:])
            last_line_kind = "-"
        elif line.startswith("+"):
            new_parts.append(line[1:])
            last_line_kind = "+"
        elif line.startswith(" "):
            content = line[1:]
            old_parts.append(content)
            new_parts.append(content)
            last_line_kind = " "
        else:
            raise PatchFormatError(f"Invalid hunk line: {_line_stripped(line) or '<blank>'}")
        index += 1
    return (
        _PatchHunk(
            old_text="".join(old_parts),
            new_text="".join(new_parts),
            change_context=change_context,
            is_end_of_file=is_end_of_file,
        ),
        index,
    )


def _parse_unified_diff(patch_text: str, context: ToolExecutionContext) -> list[_PatchOperation]:
    operations: list[_PatchOperation] = []
    current_path: Path | None = None
    expected_old_path: Path | None = None
    current_hunks: list[_PatchHunk] = []
    old_parts: list[str] = []
    new_parts: list[str] = []
    hunk_start: int | None = None
    last_line_kind: str | None = None

    for line in patch_text.splitlines(keepends=True):
        stripped = _line_stripped(line)
        if _is_no_newline_marker(stripped):
            _apply_no_newline_marker(old_parts, new_parts, last_line_kind)
            continue
        if line.startswith("diff ") or line.startswith("index "):
            continue
        if line.startswith("--- "):
            old_path = line[4:].strip()
            if old_path == "/dev/null":
                raise PatchFormatError(
                    "Unified diff add/delete/rename operations are not supported."
                )
            if old_path.startswith("a/"):
                old_path = old_path[2:]
            expected_old_path = _canonicalize_path(_resolve_path(old_path, context))
            continue
        if line.startswith("+++ "):
            if current_path is not None:
                if old_parts or new_parts:
                    current_hunks.append(
                        _PatchHunk("".join(old_parts), "".join(new_parts), old_start=hunk_start)
                    )
                if current_hunks:
                    operations.append(
                        _PatchOperation(
                            kind="update",
                            source_path=current_path,
                            destination_path=current_path,
                            hunks=current_hunks,
                        )
                    )
            path = line[4:].strip()
            if path == "/dev/null":
                raise PatchFormatError(
                    "Unified diff add/delete/rename operations are not supported."
                )
            if path.startswith("b/"):
                path = path[2:]
            current_path = _canonicalize_path(_resolve_path(path, context))
            if expected_old_path is not None and expected_old_path != current_path:
                raise PatchFormatError(
                    "Unified diff rename/add/delete operations are not supported."
                )
            current_hunks = []
            old_parts = []
            new_parts = []
            hunk_start = None
            last_line_kind = None
            expected_old_path = None
            continue
        if (
            line.startswith("rename ")
            or line.startswith("new file mode")
            or line.startswith("deleted file mode")
        ):
            raise PatchFormatError("Unified diff rename/add/delete metadata is not supported.")
        if line.startswith("@@"):
            if current_path is None:
                raise PatchFormatError("Unified diff hunk is missing a target file.")
            if old_parts or new_parts:
                current_hunks.append(
                    _PatchHunk("".join(old_parts), "".join(new_parts), old_start=hunk_start)
                )
            match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match is None:
                raise PatchFormatError("Unsupported unified diff hunk header.")
            hunk_start = int(match.group(1))
            old_parts = []
            new_parts = []
            last_line_kind = None
            continue
        if current_path is None:
            continue
        if line.startswith("-"):
            old_parts.append(line[1:])
            last_line_kind = "-"
        elif line.startswith("+"):
            new_parts.append(line[1:])
            last_line_kind = "+"
        elif line.startswith(" "):
            content = line[1:]
            old_parts.append(content)
            new_parts.append(content)
            last_line_kind = " "
        else:
            raise PatchFormatError(f"Unsupported unified diff line: {stripped or '<blank>'}")

    if current_path is not None:
        if old_parts or new_parts:
            current_hunks.append(
                _PatchHunk("".join(old_parts), "".join(new_parts), old_start=hunk_start)
            )
        if current_hunks:
            operations.append(
                _PatchOperation(
                    kind="update",
                    source_path=current_path,
                    destination_path=current_path,
                    hunks=current_hunks,
                )
            )

    if not operations:
        raise PatchFormatError("Patch did not contain any supported file operations.")
    return operations


def _operation_lock_paths(operations: list[_PatchOperation]) -> list[Path]:
    paths: list[Path] = []
    for operation in operations:
        if operation.source_path is not None:
            paths.append(operation.source_path)
        if operation.destination_path is not None:
            paths.append(operation.destination_path)
    return paths


async def _stage_patch_operations(
    operations: list[_PatchOperation], context: ToolExecutionContext
) -> list[_StagedPatchOperation]:
    staged: list[_StagedPatchOperation] = []
    seen_paths: set[Path] = set()
    for operation in operations:
        op_paths = {
            path for path in (operation.source_path, operation.destination_path) if path is not None
        }
        for path in op_paths:
            if path in seen_paths:
                raise PatchConflictError(f"Patch touches the same file multiple times: {path}")
        seen_paths.update(op_paths)
        staged.append(await _stage_patch_operation(operation, context))
    return staged


async def _stage_patch_operation(
    operation: _PatchOperation, context: ToolExecutionContext
) -> _StagedPatchOperation:
    if operation.kind == "add":
        assert operation.destination_path is not None
        if operation.destination_path.exists():
            raise PatchConflictError(
                f"Add File target already exists: {operation.destination_path}"
            )
        return _StagedPatchOperation(
            kind="add",
            destination_path=operation.destination_path,
            previous_content="",
            content=operation.add_content,
        )

    source_path = operation.source_path
    assert source_path is not None
    if not source_path.exists():
        raise PatchConflictError(f"File not found: {source_path}")
    if not source_path.is_file():
        raise PatchConflictError(f"Not a file: {source_path}")

    if operation.kind in {"update", "delete", "move"}:
        await _assert_can_modify_existing(context, source_path)
    source_content = _read_text_file(source_path)
    newline = _detect_newline(source_content)

    if operation.kind == "delete":
        return _StagedPatchOperation(
            kind="delete", source_path=source_path, previous_content=source_content
        )

    destination_path = operation.destination_path or source_path
    if operation.kind == "move":
        if destination_path == source_path:
            raise PatchConflictError(f"Move destination must differ from source: {source_path}")
        if destination_path.exists():
            raise PatchConflictError(f"Move destination already exists: {destination_path}")

    staged_content = _apply_envelope_hunks(source_content, operation.hunks, newline)
    return _StagedPatchOperation(
        kind=operation.kind,
        source_path=source_path,
        destination_path=destination_path,
        previous_content=source_content,
        content=staged_content,
    )


def _apply_envelope_hunks(content: str, hunks: list[_PatchHunk], newline: str) -> str:
    if any(hunk.old_start is not None for hunk in hunks):
        lines = content.splitlines(keepends=True)
        offset = 0
        for hunk in hunks:
            if hunk.old_start is None:
                raise PatchConflictError("Mixed patch hunk modes are not supported.")
            old_text = _normalize_patch_text_for_newline(hunk.old_text, newline)
            new_text = _normalize_patch_text_for_newline(hunk.new_text, newline)
            old_lines = old_text.splitlines(keepends=True)
            new_lines = new_text.splitlines(keepends=True)
            index = hunk.old_start - 1 + offset
            if index < 0 or lines[index : index + len(old_lines)] != old_lines:
                raise PatchConflictError(
                    "Unified diff hunk did not match at the expected location."
                )
            lines[index : index + len(old_lines)] = new_lines
            offset += len(new_lines) - len(old_lines)
        return "".join(lines)

    lines = content.splitlines(keepends=True)
    replacements: list[tuple[int, int, list[str]]] = []
    line_index = 0
    for hunk in hunks:
        old_text = _normalize_patch_text_for_newline(hunk.old_text, newline)
        new_text = _normalize_patch_text_for_newline(hunk.new_text, newline)
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)

        if hunk.change_context is not None:
            context_line = _normalize_patch_text_for_newline(hunk.change_context, newline)
            context_index = _seek_patch_sequence(lines, [context_line], line_index, eof=False)
            if context_index is None:
                raise PatchConflictError(
                    f"Failed to find context '{hunk.change_context}' in the current file content."
                )
            line_index = context_index + 1

        if not old_lines:
            if not new_lines:
                continue
            replacements.append((len(lines), 0, new_lines))
            continue

        found = _seek_patch_sequence(lines, old_lines, line_index, hunk.is_end_of_file)
        if found is None and old_lines[-1] == "":
            old_lines = old_lines[:-1]
            if new_lines and new_lines[-1] == "":
                new_lines = new_lines[:-1]
            found = _seek_patch_sequence(lines, old_lines, line_index, hunk.is_end_of_file)
        if found is None:
            raise PatchConflictError("Patch hunk did not match the current file content.")

        replacements.append((found, len(old_lines), new_lines))
        line_index = found + len(old_lines)

    updated_lines = lines.copy()
    for start_index, old_len, new_segment in sorted(replacements, reverse=True):
        updated_lines[start_index : start_index + old_len] = new_segment
    return "".join(updated_lines)


def _seek_patch_sequence(lines: list[str], pattern: list[str], start: int, eof: bool) -> int | None:
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    search_start = len(lines) - len(pattern) if eof else start
    search_end = len(lines) - len(pattern)
    if search_start > search_end:
        return None

    def exact(left: str, right: str) -> bool:
        return _line_stripped(left) == _line_stripped(right)

    def trim_end(left: str, right: str) -> bool:
        return _line_stripped(left).rstrip() == _line_stripped(right).rstrip()

    def trim(left: str, right: str) -> bool:
        return _line_stripped(left).strip() == _line_stripped(right).strip()

    for predicate in (exact, trim_end, trim, _normalized_patch_line_equal):
        found = _seek_patch_sequence_with(lines, pattern, search_start, search_end, predicate)
        if found is not None:
            return found
    return None


def _seek_patch_sequence_with(
    lines: list[str],
    pattern: list[str],
    search_start: int,
    search_end: int,
    predicate: Callable[[str, str], bool],
) -> int | None:
    for index in range(search_start, search_end + 1):
        if all(predicate(lines[index + offset], item) for offset, item in enumerate(pattern)):
            return index
    return None


def _normalized_patch_line_equal(left: str, right: str) -> bool:
    return _normalize_patch_line_for_match(left) == _normalize_patch_line_for_match(right)


def _normalize_patch_line_for_match(text: str) -> str:
    replacements = {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u00a0": " ",
        "\u2002": " ",
        "\u2003": " ",
        "\u2004": " ",
        "\u2005": " ",
        "\u2006": " ",
        "\u2007": " ",
        "\u2008": " ",
        "\u2009": " ",
        "\u200a": " ",
        "\u202f": " ",
        "\u205f": " ",
        "\u3000": " ",
    }
    return "".join(replacements.get(char, char) for char in _line_stripped(text).strip())


async def _apply_staged_patch_operations(
    staged: list[_StagedPatchOperation], context: ToolExecutionContext
) -> tuple[list[str], list[Path], list[dict[str, str]]]:
    summary_lines: list[str] = []
    diagnostic_paths: list[Path] = []
    file_diffs: list[dict[str, str]] = []

    for operation in staged:
        if operation.kind == "add":
            assert operation.destination_path is not None and operation.content is not None
            operation.destination_path.parent.mkdir(parents=True, exist_ok=True)
            operation.destination_path.write_text(operation.content, encoding="utf-8", newline="")
            formatter_changed = await _maybe_format_file(operation.destination_path)
            final_content = _read_text_file(operation.destination_path)
            if formatter_changed:
                _remove_tracked_path(context, operation.destination_path)
            else:
                await _record_write(context, operation.destination_path)
            summary_lines.append(f"Added {operation.destination_path}")
            diagnostic_paths.append(operation.destination_path)
            file_diffs.append(
                {
                    "path": str(operation.destination_path),
                    "diff": _unified_diff(operation.destination_path, "", final_content),
                }
            )
            continue

        if operation.kind == "delete":
            assert operation.source_path is not None
            operation.source_path.unlink()
            _remove_tracked_path(context, operation.source_path)
            summary_lines.append(f"Deleted {operation.source_path}")
            file_diffs.append(
                {
                    "path": str(operation.source_path),
                    "diff": _unified_diff(operation.source_path, operation.previous_content, ""),
                }
            )
            continue

        if operation.kind == "update":
            assert operation.source_path is not None and operation.content is not None
            if operation.content == operation.previous_content:
                summary_lines.append(f"Unchanged {operation.source_path}")
                continue
            operation.source_path.write_text(operation.content, encoding="utf-8", newline="")
            formatter_changed = await _maybe_format_file(operation.source_path)
            final_content = _read_text_file(operation.source_path)
            if formatter_changed:
                _remove_tracked_path(context, operation.source_path)
            else:
                await _record_write(context, operation.source_path)
            summary_lines.append(f"Updated {operation.source_path}")
            diagnostic_paths.append(operation.source_path)
            file_diffs.append(
                {
                    "path": str(operation.source_path),
                    "diff": _unified_diff(
                        operation.source_path, operation.previous_content, final_content
                    ),
                }
            )
            continue

        if operation.kind == "move":
            assert (
                operation.source_path is not None
                and operation.destination_path is not None
                and operation.content is not None
            )
            source_content = _read_text_file(operation.source_path)
            if source_content == operation.content:
                operation.destination_path.parent.mkdir(parents=True, exist_ok=True)
                operation.source_path.rename(operation.destination_path)
                await _move_tracked_path(context, operation.source_path, operation.destination_path)
                final_content = operation.content
            else:
                operation.destination_path.parent.mkdir(parents=True, exist_ok=True)
                operation.destination_path.write_text(
                    operation.content, encoding="utf-8", newline=""
                )
                formatter_changed = await _maybe_format_file(operation.destination_path)
                final_content = _read_text_file(operation.destination_path)
                if formatter_changed:
                    _remove_tracked_path(context, operation.destination_path)
                else:
                    await _record_write(context, operation.destination_path)
                operation.source_path.unlink()
                _remove_tracked_path(context, operation.source_path)
            summary_lines.append(f"Moved {operation.source_path} -> {operation.destination_path}")
            diagnostic_paths.append(operation.destination_path)
            file_diffs.append(
                {
                    "path": str(operation.destination_path),
                    "diff": _unified_diff(
                        operation.destination_path, operation.previous_content, final_content
                    ),
                }
            )
            continue

        raise PatchConflictError(f"Unsupported patch operation: {operation.kind}")

    deduped_paths = list(dict.fromkeys(diagnostic_paths))
    return summary_lines, deduped_paths, file_diffs

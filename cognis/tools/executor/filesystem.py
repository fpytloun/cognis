"""Executor-native filesystem tools: read, write, edit, patch, multiedit, list_directory."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def _resolve_path(raw: str) -> Path:
    """Resolve a user-provided path, expanding ``~`` and environment variables."""
    return resolve_path(raw)


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


async def handle_read(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Read a file or directory, returning line-numbered content."""
    file_path = arguments.get("file_path", "")
    offset = max(1, int(arguments.get("offset", 1)))
    limit = int(arguments.get("limit", _MAX_READ_LINES))

    path = _resolve_path(file_path)
    if not path.exists():
        return ToolResult(output=f"Path does not exist: {file_path}", is_error=True)

    if path.is_dir():
        return _read_directory(path)

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Cannot read file: {exc}", is_error=True)

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

    return ToolResult(output=result)


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

    path = _resolve_path(file_path)

    async def _write() -> ToolResult:
        exists = path.exists()
        if exists:
            try:
                await _assert_can_modify_existing(context, path)
            except RuntimeError as exc:
                return ToolResult(output=str(exc), is_error=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            formatter_changed = await _maybe_format_file(path)
            if formatter_changed:
                _remove_tracked_path(context, path)
            else:
                await _record_write(context, path)
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

        output = f"Wrote {len(content)} bytes to {file_path}"
        diagnostics_text = await _collect_lsp_diagnostics(context, file_path)
        if diagnostics_text:
            output += f"\n\n{diagnostics_text}"
        return ToolResult(output=output)

    return await _with_file_lock(context, path, _write)


async def handle_edit(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Edit a file by replacing exact text matches."""
    file_path = arguments.get("file_path", "")
    old_string = arguments.get("old_string", "")
    new_string = arguments.get("new_string", "")
    replace_all = bool(arguments.get("replace_all", False))

    if old_string == new_string:
        return ToolResult(output="old_string and new_string are identical.", is_error=True)

    path = _resolve_path(file_path)
    if not path.is_file():
        return ToolResult(output=f"File not found: {file_path}", is_error=True)

    async def _edit() -> ToolResult:
        try:
            await _assert_can_modify_existing(context, path)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot read file: {exc}", is_error=True)

        count = content.count(old_string)
        if count == 0:
            return ToolResult(output="oldString not found in content.", is_error=True)
        if count > 1 and not replace_all:
            return ToolResult(
                output=f"Found {count} matches for oldString. Use replace_all=true or provide more context to make the match unique.",
                is_error=True,
            )

        new_content = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        try:
            path.write_text(new_content, encoding="utf-8")
            formatter_changed = await _maybe_format_file(path)
            if formatter_changed:
                _remove_tracked_path(context, path)
            else:
                await _record_write(context, path)
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

        replacements = count if replace_all else 1
        output = f"Replaced {replacements} occurrence(s) in {file_path}"
        diagnostics_text = await _collect_lsp_diagnostics(context, file_path)
        if diagnostics_text:
            output += f"\n\n{diagnostics_text}"
        return ToolResult(output=output)

    return await _with_file_lock(context, path, _edit)


async def handle_patch(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Apply a strict unified diff or apply_patch patch."""
    patch_text = arguments.get("patch_text", "")
    if not patch_text.strip():
        return ToolResult(output="Empty patch text.", is_error=True)

    try:
        operations = _parse_patch_operations(patch_text)
        await _stage_patch_operations(operations, context)
    except (PatchFormatError, PatchConflictError, RuntimeError, OSError, PermissionError) as exc:
        return ToolResult(output=str(exc), is_error=True)

    touched_paths = _operation_lock_paths(operations)
    try:
        async with _with_file_locks(context, touched_paths):
            staged = await _stage_patch_operations(operations, context)
            summary_lines, diagnostic_paths = await _apply_staged_patch_operations(staged, context)
    except (PatchFormatError, PatchConflictError, RuntimeError, OSError, PermissionError) as exc:
        return ToolResult(output=str(exc), is_error=True)

    if not summary_lines:
        return ToolResult(output="No files were patched.", is_error=True)

    diagnostics_targets = [str(path) for path in diagnostic_paths]
    if diagnostics_targets:
        diagnostics_text = await _collect_lsp_diagnostics_batch(context, diagnostics_targets)
        if diagnostics_text:
            summary_lines.append(diagnostics_text)

    return ToolResult(output="\n".join(summary_lines))


async def handle_multiedit(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Apply multiple sequential edits to a single file."""
    file_path = arguments.get("file_path", "")
    edits = arguments.get("edits", [])

    if not isinstance(edits, list) or not edits:
        return ToolResult(output="No edits provided.", is_error=True)

    path = _resolve_path(file_path)
    if not path.is_file():
        return ToolResult(output=f"File not found: {file_path}", is_error=True)

    async def _multiedit() -> ToolResult:
        try:
            await _assert_can_modify_existing(context, path)
            content = path.read_text(encoding="utf-8")
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot read file: {exc}", is_error=True)

        applied = 0
        for i, edit in enumerate(edits):
            old_string = edit.get("old_string", "")
            new_string = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all", False))

            if old_string == new_string:
                continue
            count = content.count(old_string)
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
                content.replace(old_string, new_string)
                if replace_all
                else content.replace(old_string, new_string, 1)
            )
            applied += 1

        try:
            path.write_text(content, encoding="utf-8")
            formatter_changed = await _maybe_format_file(path)
            if formatter_changed:
                _remove_tracked_path(context, path)
            else:
                await _record_write(context, path)
        except (OSError, PermissionError) as exc:
            return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

        output = f"Applied {applied} edit(s) to {file_path}"
        diagnostics_text = await _collect_lsp_diagnostics(context, file_path)
        if diagnostics_text:
            output += f"\n\n{diagnostics_text}"
        return ToolResult(output=output)

    return await _with_file_lock(context, path, _multiedit)


async def handle_list_directory(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """List directory contents with optional ignore patterns."""
    dir_path = arguments.get("path")
    ignore_patterns = arguments.get("ignore") or []

    path = resolve_path(dir_path, default_to_home=True)
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


def _is_apply_patch_header(stripped: str) -> bool:
    return stripped in {"*** End Patch", "*** End of File"} or stripped.startswith(
        ("*** Update File: ", "*** Add File: ", "*** Delete File: ", "*** Move to: ")
    )


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _normalize_patch_text_for_newline(text: str, newline: str) -> str:
    if newline == "\n":
        return text
    return text.replace("\n", newline)


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


def _parse_patch_operations(patch_text: str) -> list[_PatchOperation]:
    stripped = patch_text.lstrip()
    if stripped.startswith("*** Begin Patch"):
        return _parse_apply_patch(patch_text)
    return _parse_unified_diff(patch_text)


def _parse_apply_patch(patch_text: str) -> list[_PatchOperation]:
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
            raise PatchFormatError("`*** End of File` is not supported.")
        if stripped.startswith("*** Update File: "):
            raw_path = stripped[len("*** Update File: ") :].strip()
            if not raw_path:
                raise PatchFormatError("`*** Update File:` requires a path.")
            operation = _PatchOperation(
                kind="update",
                source_path=_canonicalize_path(_resolve_path(raw_path)),
                destination_path=_canonicalize_path(_resolve_path(raw_path)),
            )
            index += 1
            if index < len(lines):
                move_line = _line_stripped(lines[index])
                if move_line.startswith("*** Move to: "):
                    move_path = move_line[len("*** Move to: ") :].strip()
                    if not move_path:
                        raise PatchFormatError("`*** Move to:` requires a path.")
                    operation.kind = "move"
                    operation.destination_path = _canonicalize_path(_resolve_path(move_path))
                    index += 1

            while index < len(lines):
                stripped = _line_stripped(lines[index])
                if stripped == "*** End of File":
                    raise PatchFormatError("`*** End of File` is not supported.")
                if _is_apply_patch_header(stripped) and not stripped.startswith("@@"):
                    break
                if not lines[index].startswith("@@"):
                    raise PatchFormatError(
                        f"Unexpected line in update patch: {stripped or '<blank>'}"
                    )
                hunk, index = _parse_apply_patch_hunk(lines, index)
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
                destination_path=_canonicalize_path(_resolve_path(raw_path)),
            )
            index += 1
            content_parts: list[str] = []
            while index < len(lines):
                stripped = _line_stripped(lines[index])
                if stripped == "*** End of File":
                    raise PatchFormatError("`*** End of File` is not supported.")
                if _is_apply_patch_header(stripped):
                    break
                if stripped == "\\ No newline at end of file":
                    raise PatchFormatError("`\\ No newline at end of file` is not supported.")
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
                source_path=_canonicalize_path(_resolve_path(raw_path)),
            )
            index += 1
            while index < len(lines):
                stripped = _line_stripped(lines[index])
                if stripped == "*** End of File":
                    raise PatchFormatError("`*** End of File` is not supported.")
                if _is_apply_patch_header(stripped):
                    break
                raise PatchFormatError("`*** Delete File:` does not accept body content.")
            operations.append(operation)
            continue

        raise PatchFormatError(f"Unknown apply_patch header: {stripped or '<blank>'}")

    raise PatchFormatError("apply_patch is missing `*** End Patch`.")


def _parse_apply_patch_hunk(lines: list[str], start_index: int) -> tuple[_PatchHunk, int]:
    index = start_index + 1
    old_parts: list[str] = []
    new_parts: list[str] = []
    while index < len(lines):
        stripped = _line_stripped(lines[index])
        if lines[index].startswith("@@") or _is_apply_patch_header(stripped):
            break
        if stripped == "\\ No newline at end of file":
            raise PatchFormatError("`\\ No newline at end of file` is not supported.")
        line = lines[index]
        if line.startswith("-"):
            old_parts.append(line[1:])
        elif line.startswith("+"):
            new_parts.append(line[1:])
        elif line.startswith(" "):
            content = line[1:]
            old_parts.append(content)
            new_parts.append(content)
        else:
            raise PatchFormatError(f"Invalid hunk line: {_line_stripped(line) or '<blank>'}")
        index += 1
    return _PatchHunk(old_text="".join(old_parts), new_text="".join(new_parts)), index


def _parse_unified_diff(patch_text: str) -> list[_PatchOperation]:
    operations: list[_PatchOperation] = []
    current_path: Path | None = None
    expected_old_path: Path | None = None
    current_hunks: list[_PatchHunk] = []
    old_parts: list[str] = []
    new_parts: list[str] = []
    hunk_start: int | None = None

    for line in patch_text.splitlines(keepends=True):
        stripped = _line_stripped(line)
        if stripped == "\\ No newline at end of file":
            raise PatchFormatError("`\\ No newline at end of file` is not supported.")
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
            expected_old_path = _canonicalize_path(_resolve_path(old_path))
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
            current_path = _canonicalize_path(_resolve_path(path))
            if expected_old_path is not None and expected_old_path != current_path:
                raise PatchFormatError(
                    "Unified diff rename/add/delete operations are not supported."
                )
            current_hunks = []
            old_parts = []
            new_parts = []
            hunk_start = None
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
            continue
        if current_path is None:
            continue
        if line.startswith("-"):
            old_parts.append(line[1:])
        elif line.startswith("+"):
            new_parts.append(line[1:])
        elif line.startswith(" "):
            content = line[1:]
            old_parts.append(content)
            new_parts.append(content)
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
        if not operation.destination_path.parent.is_dir():
            raise PatchConflictError(
                f"Parent directory does not exist: {operation.destination_path.parent}"
            )
        return _StagedPatchOperation(
            kind="add",
            destination_path=operation.destination_path,
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
        return _StagedPatchOperation(kind="delete", source_path=source_path)

    destination_path = operation.destination_path or source_path
    if operation.kind == "move":
        if destination_path == source_path:
            raise PatchConflictError(f"Move destination must differ from source: {source_path}")
        if destination_path.exists():
            raise PatchConflictError(f"Move destination already exists: {destination_path}")
        if not destination_path.parent.is_dir():
            raise PatchConflictError(f"Parent directory does not exist: {destination_path.parent}")

    staged_content = _apply_patch_hunks(source_content, operation.hunks, newline)
    return _StagedPatchOperation(
        kind=operation.kind,
        source_path=source_path,
        destination_path=destination_path,
        content=staged_content,
    )


def _apply_patch_hunks(content: str, hunks: list[_PatchHunk], newline: str) -> str:
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

    updated = content
    for hunk in hunks:
        old_text = _normalize_patch_text_for_newline(hunk.old_text, newline)
        new_text = _normalize_patch_text_for_newline(hunk.new_text, newline)
        matches = updated.count(old_text)
        if matches == 0:
            raise PatchConflictError("Patch hunk did not match the current file content.")
        if matches > 1:
            raise PatchConflictError("Patch hunk matched multiple locations in the current file.")
        updated = updated.replace(old_text, new_text, 1)
    return updated


async def _apply_staged_patch_operations(
    staged: list[_StagedPatchOperation], context: ToolExecutionContext
) -> tuple[list[str], list[Path]]:
    summary_lines: list[str] = []
    diagnostic_paths: list[Path] = []

    for operation in staged:
        if operation.kind == "add":
            assert operation.destination_path is not None and operation.content is not None
            operation.destination_path.write_text(operation.content, encoding="utf-8")
            formatter_changed = await _maybe_format_file(operation.destination_path)
            if formatter_changed:
                _remove_tracked_path(context, operation.destination_path)
            else:
                await _record_write(context, operation.destination_path)
            summary_lines.append(f"Added {operation.destination_path}")
            diagnostic_paths.append(operation.destination_path)
            continue

        if operation.kind == "delete":
            assert operation.source_path is not None
            operation.source_path.unlink()
            _remove_tracked_path(context, operation.source_path)
            summary_lines.append(f"Deleted {operation.source_path}")
            continue

        if operation.kind == "update":
            assert operation.source_path is not None and operation.content is not None
            operation.source_path.write_text(operation.content, encoding="utf-8")
            formatter_changed = await _maybe_format_file(operation.source_path)
            if formatter_changed:
                _remove_tracked_path(context, operation.source_path)
            else:
                await _record_write(context, operation.source_path)
            summary_lines.append(f"Updated {operation.source_path}")
            diagnostic_paths.append(operation.source_path)
            continue

        if operation.kind == "move":
            assert (
                operation.source_path is not None
                and operation.destination_path is not None
                and operation.content is not None
            )
            source_content = _read_text_file(operation.source_path)
            if source_content == operation.content:
                operation.source_path.rename(operation.destination_path)
                await _move_tracked_path(context, operation.source_path, operation.destination_path)
            else:
                operation.destination_path.write_text(operation.content, encoding="utf-8")
                formatter_changed = await _maybe_format_file(operation.destination_path)
                if formatter_changed:
                    _remove_tracked_path(context, operation.destination_path)
                else:
                    await _record_write(context, operation.destination_path)
                operation.source_path.unlink()
                _remove_tracked_path(context, operation.source_path)
            summary_lines.append(f"Moved {operation.source_path} -> {operation.destination_path}")
            diagnostic_paths.append(operation.destination_path)
            continue

        raise PatchConflictError(f"Unsupported patch operation: {operation.kind}")

    deduped_paths = list(dict.fromkeys(diagnostic_paths))
    return summary_lines, deduped_paths

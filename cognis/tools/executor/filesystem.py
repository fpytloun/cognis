"""Executor-native filesystem tools: read, write, edit, patch, multiedit, list_directory."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.registry import ToolExecutionContext

_MAX_READ_LINES = 2000
_MAX_LINE_LENGTH = 2000
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


async def handle_read(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Read a file or directory, returning line-numbered content."""
    file_path = arguments.get("file_path", "")
    offset = max(1, int(arguments.get("offset", 1)))
    limit = int(arguments.get("limit", _MAX_READ_LINES))

    path = Path(file_path)
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

    path = Path(file_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

    return ToolResult(output=f"Wrote {len(content)} bytes to {file_path}")


async def handle_edit(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Edit a file by replacing exact text matches."""
    file_path = arguments.get("file_path", "")
    old_string = arguments.get("old_string", "")
    new_string = arguments.get("new_string", "")
    replace_all = bool(arguments.get("replace_all", False))

    if old_string == new_string:
        return ToolResult(output="old_string and new_string are identical.", is_error=True)

    path = Path(file_path)
    if not path.is_file():
        return ToolResult(output=f"File not found: {file_path}", is_error=True)

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

    if replace_all:
        new_content = content.replace(old_string, new_string)
    else:
        new_content = content.replace(old_string, new_string, 1)

    try:
        path.write_text(new_content, encoding="utf-8")
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

    replacements = count if replace_all else 1
    return ToolResult(output=f"Replaced {replacements} occurrence(s) in {file_path}")


async def handle_patch(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Apply a unified diff patch."""
    patch_text = arguments.get("patch_text", "")
    if not patch_text.strip():
        return ToolResult(output="Empty patch text.", is_error=True)

    files_patched: list[str] = []
    errors: list[str] = []

    for file_path, hunks in _parse_unified_diff(patch_text):
        path = Path(file_path)
        if not path.is_file():
            errors.append(f"File not found: {file_path}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            new_lines = _apply_hunks(lines, hunks)
            path.write_text("".join(new_lines), encoding="utf-8")
            files_patched.append(file_path)
        except Exception as exc:
            errors.append(f"Failed to patch {file_path}: {exc}")

    parts: list[str] = []
    if files_patched:
        parts.append(f"Patched {len(files_patched)} file(s): {', '.join(files_patched)}")
    if errors:
        parts.append("Errors:\n" + "\n".join(errors))
    if not parts:
        return ToolResult(output="No files were patched.", is_error=True)
    return ToolResult(output="\n".join(parts), is_error=bool(errors))


async def handle_multiedit(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Apply multiple sequential edits to a single file."""
    file_path = arguments.get("file_path", "")
    edits = arguments.get("edits", [])

    if not isinstance(edits, list) or not edits:
        return ToolResult(output="No edits provided.", is_error=True)

    path = Path(file_path)
    if not path.is_file():
        return ToolResult(output=f"File not found: {file_path}", is_error=True)

    try:
        content = path.read_text(encoding="utf-8")
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
                output=f"Edit {i + 1}: oldString not found in content.",
                is_error=True,
            )
        if count > 1 and not replace_all:
            return ToolResult(
                output=f"Edit {i + 1}: Found {count} matches. Use replace_all or provide more context.",
                is_error=True,
            )
        if replace_all:
            content = content.replace(old_string, new_string)
        else:
            content = content.replace(old_string, new_string, 1)
        applied += 1

    try:
        path.write_text(content, encoding="utf-8")
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Cannot write file: {exc}", is_error=True)

    return ToolResult(output=f"Applied {applied} edit(s) to {file_path}")


async def handle_list_directory(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """List directory contents with optional ignore patterns."""
    dir_path = arguments.get("path", ".")
    ignore_patterns = arguments.get("ignore") or []

    path = Path(dir_path)
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


def _parse_unified_diff(
    patch_text: str,
) -> list[tuple[str, list[tuple[int, list[str], list[str]]]]]:
    """Parse a unified diff into (file_path, hunks) pairs.

    Each hunk is (start_line, old_lines, new_lines).
    """
    files: list[tuple[str, list[tuple[int, list[str], list[str]]]]] = []
    current_file: str | None = None
    hunks: list[tuple[int, list[str], list[str]]] = []
    old_lines: list[str] = []
    new_lines: list[str] = []
    hunk_start = 0

    for line in patch_text.splitlines(keepends=True):
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            if current_file is not None and hunks:
                files.append((current_file, hunks))
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            current_file = path
            hunks = []
            old_lines = []
            new_lines = []
            continue
        hunk_match = re.match(r"^@@ -(\d+)", line)
        if hunk_match:
            if old_lines or new_lines:
                hunks.append((hunk_start, old_lines, new_lines))
            hunk_start = int(hunk_match.group(1))
            old_lines = []
            new_lines = []
            continue
        if current_file is None:
            continue
        if line.startswith("-"):
            old_lines.append(line[1:])
        elif line.startswith("+"):
            new_lines.append(line[1:])
        else:
            content = line[1:] if line.startswith(" ") else line
            old_lines.append(content)
            new_lines.append(content)

    if old_lines or new_lines:
        hunks.append((hunk_start, old_lines, new_lines))
    if current_file is not None and hunks:
        files.append((current_file, hunks))

    return files


def _apply_hunks(lines: list[str], hunks: list[tuple[int, list[str], list[str]]]) -> list[str]:
    """Apply parsed hunks to file lines."""
    result = list(lines)
    offset = 0
    for start, old, new in hunks:
        idx = start - 1 + offset
        old_len = len(old)
        new_normalized = [ln if ln.endswith("\n") else ln + "\n" for ln in new]
        result[idx : idx + old_len] = new_normalized
        offset += len(new_normalized) - old_len
    return result

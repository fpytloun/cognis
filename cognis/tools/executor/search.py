"""Executor-native search tools: glob, grep."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

_MAX_RESULTS = 200
_MAX_LINE_LENGTH = 2000


async def handle_glob(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Find files matching a glob pattern, sorted by modification time."""
    pattern = arguments.get("pattern", "")
    search_path = arguments.get("path", ".")

    if not pattern:
        return ToolResult(output="No pattern provided.", is_error=True)

    base = resolve_path(search_path)
    if not base.is_dir():
        return ToolResult(output=f"Not a directory: {search_path}", is_error=True)

    try:
        matches = list(base.glob(pattern))
    except (OSError, ValueError) as exc:
        return ToolResult(output=f"Glob error: {exc}", is_error=True)

    # Sort by modification time (newest first)
    try:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        matches.sort(key=lambda p: str(p))

    if not matches:
        return ToolResult(output="No files found matching the pattern.")

    lines: list[str] = []
    for match in matches[:_MAX_RESULTS]:
        try:
            rel = match.relative_to(base)
        except ValueError:
            rel = match
        lines.append(str(rel))

    result = "\n".join(lines)
    if len(matches) > _MAX_RESULTS:
        result += f"\n\n({len(matches)} total matches, showing first {_MAX_RESULTS})"
    return ToolResult(output=result)


async def handle_grep(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Search file contents using regex patterns."""
    pattern = arguments.get("pattern", "")
    search_path = arguments.get("path", ".")
    include = arguments.get("include")

    if not pattern:
        return ToolResult(output="No pattern provided.", is_error=True)

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult(output=f"Invalid regex: {exc}", is_error=True)

    base = resolve_path(search_path)
    if not base.is_dir():
        return ToolResult(output=f"Not a directory: {search_path}", is_error=True)

    file_pattern = f"**/{include}" if include else "**/*"

    results: list[tuple[Path, list[tuple[int, str]]]] = []
    total_matches = 0

    try:
        for file_path in base.glob(file_pattern):
            if not file_path.is_file():
                continue
            if _should_skip(file_path):
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue

            file_matches: list[tuple[int, str]] = []
            for line_num, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    display = line[:_MAX_LINE_LENGTH]
                    if len(line) > _MAX_LINE_LENGTH:
                        display += "..."
                    file_matches.append((line_num, display))
                    total_matches += 1

            if file_matches:
                results.append((file_path, file_matches))

            if total_matches >= 500:
                break
    except (OSError, ValueError) as exc:
        return ToolResult(output=f"Search error: {exc}", is_error=True)

    if not results:
        return ToolResult(output="No matches found.")

    # Sort by modification time (newest first)
    import contextlib

    with contextlib.suppress(OSError):
        results.sort(key=lambda r: r[0].stat().st_mtime, reverse=True)

    lines: list[str] = []
    for file_path, matches in results[:_MAX_RESULTS]:
        try:
            rel = file_path.relative_to(base)
        except ValueError:
            rel = file_path
        for line_num, line_text in matches[:10]:
            lines.append(f"{rel}:{line_num}: {line_text}")
        if len(matches) > 10:
            lines.append(f"  ... and {len(matches) - 10} more matches in {rel}")

    result = "\n".join(lines)
    if total_matches >= 500:
        result += "\n\n(Search truncated at 500 matches)"
    return ToolResult(output=result)


_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    "target",
    "vendor",
}

_BINARY_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".o",
    ".a",
    ".class",
    ".jar",
    ".war",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
}


def _should_skip(path: Path) -> bool:
    """Check if a file should be skipped during search."""
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    return any(part in _SKIP_DIRS for part in path.parts)

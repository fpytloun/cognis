"""Executor-native search tools: glob, grep."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import re
import shutil
from pathlib import Path
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

_MAX_RESULTS = 200
_MAX_LINE_LENGTH = 2000
_MAX_MATCHES = 500
_RG_PATH = shutil.which("rg")
_FD_PATH = shutil.which("fd") or shutil.which("fdfind")

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


async def handle_glob(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Find files matching a glob pattern, sorted by modification time."""
    pattern = arguments.get("pattern", "")
    search_path = arguments.get("path")

    if not pattern:
        return ToolResult(output="No pattern provided.", is_error=True)

    try:
        base = resolve_path(search_path, context=context, default_to_home=True)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    if not base.is_dir():
        return ToolResult(output=f"Not a directory: {search_path}", is_error=True)

    if _FD_PATH is not None:
        fd_result = await _glob_with_fd(base, str(pattern))
        if fd_result is not None:
            return fd_result

    return await _glob_with_python(base, str(pattern))


async def handle_grep(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Search file contents using regex patterns."""
    pattern = arguments.get("pattern", "")
    search_path = arguments.get("path")
    include = arguments.get("include")
    include_patterns = _normalize_include_patterns(include)

    if not pattern:
        return ToolResult(output="No pattern provided.", is_error=True)

    try:
        re.compile(str(pattern))
    except re.error as exc:
        return ToolResult(output=f"Invalid regex: {exc}", is_error=True)

    try:
        base = resolve_path(search_path, context=context, default_to_home=True)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    if base.is_file():
        if _RG_PATH is not None:
            rg_result = await _grep_with_rg(base.parent, str(pattern), [], target=base)
            if rg_result is not None:
                return rg_result
        return await _grep_file_with_python(base, str(pattern))

    if not base.is_dir():
        return ToolResult(output=f"Not a directory or file: {search_path}", is_error=True)

    if _RG_PATH is not None:
        rg_result = await _grep_with_rg(base, str(pattern), include_patterns)
        if rg_result is not None:
            return rg_result

    return await _grep_with_python(base, str(pattern), include_patterns)


async def _glob_with_fd(base: Path, pattern: str) -> ToolResult | None:
    command = [_FD_PATH, "--absolute-path", "--color", "never", "--glob", pattern, str(base)]
    for skip_dir in sorted(_SKIP_DIRS):
        command.extend(["--exclude", skip_dir])
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except OSError:
        return None

    if process.returncode not in {0, 1}:
        error = stderr.decode("utf-8", errors="replace").strip()
        return ToolResult(output=f"Glob error: {error or 'fd failed'}", is_error=True)

    matches = [Path(line) for line in stdout.decode("utf-8", errors="replace").splitlines() if line]
    return _format_glob_results(base, matches)


async def _glob_with_python(base: Path, pattern: str) -> ToolResult:
    try:
        matches = [path for path in base.glob(pattern) if path.is_file() and not _should_skip(path)]
    except (OSError, ValueError) as exc:
        return ToolResult(output=f"Glob error: {exc}", is_error=True)
    return _format_glob_results(base, matches)


def _format_glob_results(base: Path, matches: list[Path]) -> ToolResult:
    if not matches:
        return ToolResult(output="No files found matching the pattern.")

    try:
        matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        matches.sort(key=lambda path: str(path))

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


async def _grep_with_rg(
    base: Path,
    pattern: str,
    include_patterns: list[str],
    *,
    target: Path | None = None,
) -> ToolResult | None:
    command = [
        _RG_PATH,
        "--json",
        "--line-number",
        "--hidden",
        "--color",
        "never",
        "--max-count",
        str(_MAX_MATCHES),
    ]
    for include in include_patterns:
        command.extend(["--glob", include])
    for skip_dir in sorted(_SKIP_DIRS):
        command.extend(["--glob", f"!**/{skip_dir}/**"])
    command.extend(["--", pattern, str(target or base)])

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except OSError:
        return None

    if process.returncode not in {0, 1}:
        error = stderr.decode("utf-8", errors="replace").strip()
        return ToolResult(output=f"Search error: {error or 'rg failed'}", is_error=True)

    results: dict[str, list[tuple[int, str]]] = {}
    total_matches = 0
    for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw_line)
            if payload.get("type") != "match":
                continue
            data = payload.get("data", {})
            path_info = data.get("path", {})
            line_info = data.get("lines", {})
            line_number = data.get("line_number")
            if not isinstance(line_number, int):
                continue
            text = str(line_info.get("text", "")).rstrip("\n")
            if len(text) > _MAX_LINE_LENGTH:
                text = text[:_MAX_LINE_LENGTH] + "..."
            path_text = str(path_info.get("text", "")).strip()
            if not path_text:
                continue
            results.setdefault(path_text, []).append((line_number, text))
            total_matches += 1
            if total_matches >= _MAX_MATCHES:
                break

    return _format_grep_results(base, results, total_matches)


async def _grep_with_python(base: Path, pattern: str, include_patterns: list[str]) -> ToolResult:
    regex = re.compile(pattern)
    results: dict[str, list[tuple[int, str]]] = {}
    total_matches = 0

    try:
        for file_path in base.glob("**/*"):
            if not file_path.is_file() or _should_skip(file_path):
                continue
            if include_patterns and not _matches_any_include(base, file_path, include_patterns):
                continue
            file_matches = _grep_python_file_matches(file_path, regex)
            total_matches += len(file_matches)
            if file_matches:
                results[str(file_path)] = file_matches
            if total_matches >= _MAX_MATCHES:
                break
    except (OSError, ValueError) as exc:
        return ToolResult(output=f"Search error: {exc}", is_error=True)

    return _format_grep_results(base, results, total_matches)


def _normalize_include_patterns(include: Any) -> list[str]:
    """Return include globs, accepting either brace syntax or comma lists."""
    if include is None:
        return []
    return [part for part in _split_top_level_commas(str(include)) if part]


def _split_top_level_commas(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    brace_depth = 0

    for char in value:
        if char == "{":
            brace_depth += 1
        elif char == "}" and brace_depth > 0:
            brace_depth -= 1

        if char == "," and brace_depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)

    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def _matches_any_include(base: Path, file_path: Path, include_patterns: list[str]) -> bool:
    try:
        rel = file_path.relative_to(base).as_posix()
    except ValueError:
        rel = file_path.as_posix()
    name = file_path.name

    for pattern in include_patterns:
        for expanded in _expand_brace_pattern(pattern):
            if "/" in expanded:
                if fnmatch.fnmatch(rel, expanded):
                    return True
            elif fnmatch.fnmatch(name, expanded):
                return True
    return False


def _expand_brace_pattern(pattern: str) -> list[str]:
    start = pattern.find("{")
    if start == -1:
        return [pattern]

    depth = 0
    end = -1
    for index, char in enumerate(pattern[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end == -1:
        return [pattern]

    prefix = pattern[:start]
    suffix = pattern[end + 1 :]
    expanded: list[str] = []
    for option in _split_top_level_commas(pattern[start + 1 : end]):
        expanded.extend(_expand_brace_pattern(f"{prefix}{option}{suffix}"))
    return expanded or [pattern]


async def _grep_file_with_python(file_path: Path, pattern: str) -> ToolResult:
    regex = re.compile(pattern)
    try:
        matches = _grep_python_file_matches(file_path, regex)
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Search error: {exc}", is_error=True)
    return _format_grep_results(
        file_path.parent, {str(file_path): matches} if matches else {}, len(matches)
    )


def _grep_python_file_matches(file_path: Path, regex: re.Pattern[str]) -> list[tuple[int, str]]:
    content = file_path.read_text(encoding="utf-8", errors="replace")
    file_matches: list[tuple[int, str]] = []
    for line_num, line in enumerate(content.splitlines(), start=1):
        if regex.search(line):
            display = line[:_MAX_LINE_LENGTH]
            if len(line) > _MAX_LINE_LENGTH:
                display += "..."
            file_matches.append((line_num, display))
            if len(file_matches) >= _MAX_MATCHES:
                break
    return file_matches


def _format_grep_results(
    base: Path,
    results: dict[str, list[tuple[int, str]]],
    total_matches: int,
) -> ToolResult:
    if not results:
        return ToolResult(output="No matches found.")

    paths = [Path(path_text) for path_text in results]
    try:
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        paths.sort(key=lambda path: str(path))

    lines: list[str] = []
    for file_path in paths[:_MAX_RESULTS]:
        matches = results.get(str(file_path), [])
        try:
            rel = file_path.relative_to(base)
        except ValueError:
            rel = file_path
        for line_num, line_text in matches[:10]:
            lines.append(f"{rel}:{line_num}: {line_text}")
        if len(matches) > 10:
            lines.append(f"  ... and {len(matches) - 10} more matches in {rel}")

    result = "\n".join(lines)
    if total_matches >= _MAX_MATCHES:
        result += f"\n\n(Search truncated at {_MAX_MATCHES} matches)"
    return ToolResult(output=result)


def _should_skip(path: Path) -> bool:
    """Check if a file should be skipped during search."""
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    return any(part in _SKIP_DIRS for part in path.parts)

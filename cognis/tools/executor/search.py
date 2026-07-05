"""Executor-native search tools: glob, grep."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import re
import shutil
from dataclasses import dataclass
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


@dataclass(slots=True)
class _GrepOptions:
    case_insensitive: bool = False
    context_lines: int = 0
    output_mode: str = "content"
    max_per_file: int = 10
    single_file_target: bool = False


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
    output_mode = str(arguments.get("output_mode") or "content")
    if output_mode not in {"content", "files_with_matches", "count"}:
        return ToolResult(
            output="output_mode must be one of: content, files_with_matches, count.",
            is_error=True,
        )
    context_lines, context_error = _parse_non_negative_int(
        arguments.get("context_lines", 0), "context_lines"
    )
    if context_error:
        return ToolResult(output=context_error, is_error=True)
    raw_max_per_file = arguments.get("max_per_file")
    case_insensitive = bool(arguments.get("case_insensitive", False))

    if not pattern:
        return ToolResult(output="No pattern provided.", is_error=True)

    try:
        re.compile(str(pattern), flags=re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        return ToolResult(output=f"Invalid regex: {exc}", is_error=True)

    try:
        base = resolve_path(search_path, context=context, default_to_home=True)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    max_per_file, max_error = _parse_max_per_file(raw_max_per_file, single_file=base.is_file())
    if max_error:
        return ToolResult(output=max_error, is_error=True)
    options = _GrepOptions(
        case_insensitive=case_insensitive,
        context_lines=context_lines,
        output_mode=output_mode,
        max_per_file=max_per_file,
        single_file_target=base.is_file(),
    )

    if base.is_file():
        if _RG_PATH is not None:
            rg_result = await _grep_with_rg(base.parent, str(pattern), [], options, target=base)
            if rg_result is not None:
                return _grep_result_with_trust(rg_result, options)
        return _grep_result_with_trust(
            await _grep_file_with_python(base, str(pattern), options), options
        )

    if not base.is_dir():
        return ToolResult(output=f"Not a directory or file: {search_path}", is_error=True)

    if _RG_PATH is not None:
        rg_result = await _grep_with_rg(base, str(pattern), include_patterns, options)
        if rg_result is not None:
            return _grep_result_with_trust(rg_result, options)

    return _grep_result_with_trust(
        await _grep_with_python(base, str(pattern), include_patterns, options), options
    )


def _grep_result_with_trust(result: ToolResult, options: _GrepOptions) -> ToolResult:
    if options.output_mode != "content" or result.is_error:
        return result
    metadata = dict(result.metadata or {})
    metadata["content_trust"] = "untrusted"
    return result.model_copy(update={"metadata": metadata})


async def _glob_with_fd(base: Path, pattern: str) -> ToolResult | None:
    fd_path = _FD_PATH
    if fd_path is None:
        return None
    command = [
        fd_path,
        "--absolute-path",
        "--type",
        "f",
        "--color",
        "never",
        "--glob",
        pattern,
        str(base),
    ]
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

    matches = [
        Path(line)
        for line in stdout.decode("utf-8", errors="replace").splitlines()
        if line and Path(line).is_file()
    ]
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
        lines.append(str(match.resolve()))

    result = "\n".join(lines)
    if len(matches) > _MAX_RESULTS:
        result += f"\n\n({len(matches)} total matches, showing first {_MAX_RESULTS})"
    return ToolResult(output=result)


async def _grep_with_rg(
    base: Path,
    pattern: str,
    include_patterns: list[str],
    options: _GrepOptions,
    *,
    target: Path | None = None,
) -> ToolResult | None:
    rg_path = _RG_PATH
    if rg_path is None:
        return None
    command = [
        rg_path,
        "--line-number",
        "--hidden",
        "--color",
        "never",
    ]
    if options.case_insensitive:
        command.append("-i")
    if options.output_mode == "content":
        command.extend(["--max-count", str(options.max_per_file)])
        command.append("--json")
        if options.context_lines:
            command.extend(["-C", str(options.context_lines)])
    elif options.output_mode == "files_with_matches":
        command.append("--files-with-matches")
    else:
        command.append("--count-matches")
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

    if options.output_mode == "files_with_matches":
        paths = [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line]
        return _format_files_with_matches(paths)
    if options.output_mode == "count":
        return _format_count_results(stdout.decode("utf-8", errors="replace"))

    results: dict[str, list[tuple[int, str, bool]]] = {}
    total_matches = 0
    for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw_line)
            payload_type = payload.get("type")
            if payload_type not in {"match", "context"}:
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
            is_match = payload_type == "match"
            per_file = results.setdefault(path_text, [])
            if is_match:
                total_matches += 1
            if is_match or options.context_lines:
                per_file.append((line_number, text, is_match))
            if total_matches >= _MAX_MATCHES:
                break

    return _format_grep_results(base, results, total_matches, options)


async def _grep_with_python(
    base: Path, pattern: str, include_patterns: list[str], options: _GrepOptions
) -> ToolResult:
    regex = re.compile(pattern, flags=re.IGNORECASE if options.case_insensitive else 0)
    results: dict[str, list[tuple[int, str, bool]]] = {}
    total_matches = 0

    try:
        for file_path in base.glob("**/*"):
            if not file_path.is_file() or _should_skip(file_path):
                continue
            if include_patterns and not _matches_any_include(base, file_path, include_patterns):
                continue
            file_matches = _grep_python_file_matches(file_path, regex, options)
            match_count = sum(1 for _, _, is_match in file_matches if is_match)
            if file_matches:
                results[str(file_path)] = file_matches
            total_matches += match_count
            if total_matches >= _MAX_MATCHES:
                break
    except (OSError, ValueError) as exc:
        return ToolResult(output=f"Search error: {exc}", is_error=True)

    if options.output_mode == "files_with_matches":
        return _format_files_with_matches(results)
    if options.output_mode == "count":
        return _format_count_results(results)
    return _format_grep_results(base, results, total_matches, options)


def _parse_non_negative_int(value: Any, name: str) -> tuple[int, str | None]:
    try:
        if isinstance(value, bool):
            raise TypeError
        parsed = int(value)
    except (TypeError, ValueError):
        return 0, f"{name} must be an integer."
    if parsed < 0:
        return 0, f"{name} must be >= 0."
    return parsed, None


def _parse_max_per_file(value: Any, *, single_file: bool) -> tuple[int, str | None]:
    if value is None:
        return (_MAX_MATCHES if single_file else 10), None
    parsed, error = _parse_non_negative_int(value, "max_per_file")
    if error:
        return 0, error
    if parsed < 1:
        return 0, "max_per_file must be >= 1."
    return min(parsed, _MAX_MATCHES), None


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


async def _grep_file_with_python(
    file_path: Path, pattern: str, options: _GrepOptions
) -> ToolResult:
    regex = re.compile(pattern, flags=re.IGNORECASE if options.case_insensitive else 0)
    try:
        matches = _grep_python_file_matches(file_path, regex, options)
    except (OSError, PermissionError) as exc:
        return ToolResult(output=f"Search error: {exc}", is_error=True)
    if options.output_mode == "files_with_matches":
        return _format_files_with_matches({str(file_path): matches} if matches else {})
    if options.output_mode == "count":
        return _format_count_results({str(file_path): matches} if matches else {})
    return _format_grep_results(
        file_path.parent,
        {str(file_path): matches} if matches else {},
        sum(1 for _, _, is_match in matches if is_match),
        options,
    )


def _grep_python_file_matches(
    file_path: Path, regex: re.Pattern[str], options: _GrepOptions
) -> list[tuple[int, str, bool]]:
    content = file_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    file_matches: list[tuple[int, str, bool]] = []
    emitted_lines: set[int] = set()
    match_count = 0
    for index, line in enumerate(lines):
        if not regex.search(line):
            continue
        if match_count >= options.max_per_file or match_count >= _MAX_MATCHES:
            break
        start = max(0, index - options.context_lines)
        end = min(len(lines), index + options.context_lines + 1)
        for line_index in range(start, end):
            if line_index in emitted_lines:
                continue
            emitted_lines.add(line_index)
            context_line = lines[line_index]
            display = context_line[:_MAX_LINE_LENGTH]
            if len(context_line) > _MAX_LINE_LENGTH:
                display += "..."
            file_matches.append((line_index + 1, display, line_index == index))
        match_count += 1
    return file_matches


def _format_grep_results(
    base: Path,
    results: dict[str, list[tuple[int, str, bool]]],
    total_matches: int,
    options: _GrepOptions,
) -> ToolResult:
    del base
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
        path_text = str(file_path.resolve())
        for line_num, line_text, is_match in matches:
            separator = ":" if is_match else "-"
            lines.append(f"{path_text}{separator}{line_num}{separator} {line_text}")
        actual_matches = sum(1 for _, _, is_match in matches if is_match)
        if actual_matches >= options.max_per_file and not options.single_file_target:
            lines.append(
                f"  ... more matches may exist in {path_text}. Re-run with path='{path_text}' "
                f"or increase max_per_file (current {options.max_per_file})."
            )

    result = "\n".join(lines)
    if total_matches >= _MAX_MATCHES:
        result += f"\n\n(Search truncated at {_MAX_MATCHES} matches)"
    return ToolResult(output=result)


def _format_files_with_matches(
    results: dict[str, list[tuple[int, str, bool]]] | list[str],
) -> ToolResult:
    if isinstance(results, list):
        paths = [Path(path) for path in results]
    else:
        paths = [
            Path(path) for path, matches in results.items() if any(item[2] for item in matches)
        ]
    if not paths:
        return ToolResult(output="No matches found.")
    paths.sort(key=lambda path: str(path))
    return ToolResult(output="\n".join(str(path.resolve()) for path in paths[:_MAX_RESULTS]))


def _format_count_results(
    results: dict[str, list[tuple[int, str, bool]]] | str,
) -> ToolResult:
    counts: dict[str, int] = {}
    if isinstance(results, str):
        for line in results.splitlines():
            if not line:
                continue
            path, separator, count_text = line.rpartition(":")
            if not separator:
                continue
            with contextlib.suppress(ValueError):
                counts[path] = int(count_text)
    else:
        counts = {
            path: sum(1 for _, _, is_match in matches if is_match)
            for path, matches in results.items()
            if any(is_match for _, _, is_match in matches)
        }
    if not counts:
        return ToolResult(output="No matches found.")
    lines = [f"{Path(path).resolve()}: {count}" for path, count in sorted(counts.items())]
    lines.append(f"Total matches: {sum(counts.values())}")
    return ToolResult(output="\n".join(lines))


def _should_skip(path: Path) -> bool:
    """Check if a file should be skipped during search."""
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    return any(part in _SKIP_DIRS for part in path.parts)

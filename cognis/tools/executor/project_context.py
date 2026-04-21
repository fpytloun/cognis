"""Internal executor helper for probing project roots and instruction files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from cognis.core.project_context import (
    build_project_instruction_message,
    normalize_project_path,
    project_instruction_hash,
)
from cognis.models.tool import ToolResult
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

INTERNAL_PROJECT_CONTEXT_PROBE_TOOL = "_project_context_probe"

_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "README.md")
_PROJECT_MARKERS = (
    ".git",
    ".hg",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    ".project-root",
)
_COMMON_PROJECT_ROOTS = ("src", "code", "projects", "workspace", "workspaces", "dev")
_HINT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}")
_HINT_STOPWORDS = {
    "a",
    "an",
    "and",
    "app",
    "code",
    "directory",
    "file",
    "for",
    "from",
    "git",
    "in",
    "inside",
    "into",
    "list",
    "open",
    "path",
    "project",
    "read",
    "repo",
    "repository",
    "root",
    "show",
    "the",
    "this",
    "under",
    "use",
    "workdir",
    "workspace",
}
_MAX_PROJECT_INSTRUCTION_BYTES = 32_000


async def handle_project_context_probe(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Resolve a project root and load its frozen instruction payload."""

    raw_path = arguments.get("path")
    path_kind = str(arguments.get("path_kind") or "directory")
    hint_text = str(arguments.get("hint_text") or "")
    fallback_cwd = normalize_project_path(arguments.get("fallback_cwd"))
    fallback_home = normalize_project_path(arguments.get("fallback_home"))

    anchor = _resolve_anchor(
        raw_path=raw_path if isinstance(raw_path, str) else None,
        path_kind=path_kind,
        context=context,
        fallback_cwd=fallback_cwd,
        fallback_home=fallback_home,
    )
    result = _discover_project_context(anchor=anchor, hint_text=hint_text, fallback_home=fallback_home)
    if result["status"] == "loaded":
        return ToolResult(
            output=f"Loaded project instructions for {result['project_root']}",
            metadata={"project_context": result},
        )
    if result["status"] == "missing":
        return ToolResult(
            output=(
                f"Resolved project root {result['project_root']} but found no AGENTS.md, "
                "CLAUDE.md, or README.md to load."
            ),
            metadata={"project_context": result},
        )
    return ToolResult(output="No project context resolved.", metadata={"project_context": result})


def _resolve_anchor(
    *,
    raw_path: str | None,
    path_kind: str,
    context: ToolExecutionContext,
    fallback_cwd: str | None,
    fallback_home: str | None,
) -> Path | None:
    try:
        if raw_path:
            resolved = resolve_path(raw_path, context=context, default_to_home=False)
        elif fallback_cwd:
            resolved = Path(fallback_cwd)
        elif fallback_home:
            resolved = Path(fallback_home)
        else:
            resolved = resolve_path(None, context=context, default_to_home=True)
    except ValueError:
        return None
    except OSError:
        return None

    if path_kind == "file":
        return resolved.parent if resolved.name else resolved
    if resolved.exists() and resolved.is_file():
        return resolved.parent
    return resolved


def _discover_project_context(
    *,
    anchor: Path | None,
    hint_text: str,
    fallback_home: str | None,
) -> dict[str, Any]:
    if anchor is not None:
        direct = _search_ancestors(anchor)
        if direct is not None:
            return direct

    hinted = _search_by_hint(hint_text, fallback_home=fallback_home)
    if hinted is not None:
        return hinted

    return {
        "status": "not_project",
        "project_root": None,
        "working_directory": str(anchor) if anchor is not None else None,
        "source_path": None,
        "content": None,
        "content_hash": None,
    }


def _search_ancestors(anchor: Path) -> dict[str, Any] | None:
    anchor = anchor.resolve(strict=False)
    ancestors = [anchor, *anchor.parents]

    for directory in ancestors:
        instruction = _select_instruction(directory, include_readme=False)
        if instruction is not None:
            return _loaded_result(directory, anchor, instruction)

    for directory in ancestors:
        if _is_project_root(directory):
            instruction = _select_instruction(directory, include_readme=True)
            if instruction is not None:
                return _loaded_result(directory, anchor, instruction)
            return _missing_result(directory, anchor)

    for directory in ancestors:
        instruction = _select_instruction(directory, include_readme=True)
        if instruction is not None:
            return _loaded_result(directory, anchor, instruction)
    return None


def _search_by_hint(hint_text: str, *, fallback_home: str | None) -> dict[str, Any] | None:
    home = Path(fallback_home or Path.home()).resolve(strict=False)
    hints = _extract_project_hints(hint_text)
    if not hints:
        return None

    matches: list[Path] = []
    for hint in hints:
        for root_name in _COMMON_PROJECT_ROOTS:
            candidate = home / root_name / hint
            if candidate.is_dir() and (_is_project_root(candidate) or _select_instruction(candidate)):
                matches.append(candidate.resolve(strict=False))
    unique_matches = sorted({path for path in matches})
    if len(unique_matches) != 1:
        return None
    root = unique_matches[0]
    instruction = _select_instruction(root, include_readme=True)
    if instruction is None:
        return _missing_result(root, root)
    return _loaded_result(root, root, instruction)


def _extract_project_hints(text: str) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for match in _HINT_RE.findall(text or ""):
        candidate = match.strip("./~`").lower()
        if len(candidate) < 2 or candidate in _HINT_STOPWORDS:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        hints.append(match.strip("./~`"))
    return hints[:6]


def _loaded_result(project_root: Path, anchor: Path, source_path: Path) -> dict[str, Any]:
    try:
        raw_content = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _missing_result(project_root, anchor)
    content = raw_content[:_MAX_PROJECT_INSTRUCTION_BYTES].strip()
    if not content:
        return _missing_result(project_root, anchor)
    message = build_project_instruction_message(
        project_root=str(project_root),
        source_path=str(source_path),
        content=content,
        working_directory=str(anchor),
    )
    return {
        "status": "loaded",
        "project_root": str(project_root),
        "working_directory": str(anchor),
        "source_path": str(source_path),
        "content": message,
        "content_hash": project_instruction_hash(message),
    }


def _missing_result(project_root: Path, anchor: Path) -> dict[str, Any]:
    return {
        "status": "missing",
        "project_root": str(project_root),
        "working_directory": str(anchor),
        "source_path": None,
        "content": None,
        "content_hash": None,
    }


def _select_instruction(directory: Path, *, include_readme: bool = True) -> Path | None:
    for filename in _INSTRUCTION_FILES:
        if filename == "README.md" and not include_readme:
            continue
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def _is_project_root(directory: Path) -> bool:
    for marker in _PROJECT_MARKERS:
        candidate = directory / marker
        if candidate.exists():
            return True
    return False

"""Shared path resolution for executor tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cognis.tools.registry import ToolExecutionContext


def resolve_path(
    raw: str | None,
    *,
    context: ToolExecutionContext | None = None,
    default_to_home: bool = False,
    base: str = "cwd",
) -> Path:
    """Resolve a tool path against runtime cwd/root rules."""

    metadata = context.runtime_metadata if context is not None else {}
    workspace_root = _runtime_workspace_root(metadata)
    working_directory = _runtime_working_directory(metadata)

    if raw is None or raw == "":
        if default_to_home:
            if working_directory:
                return _validate_workspace_path(working_directory, workspace_root)
            return Path.home()
        return working_directory or Path("")

    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    anchored = False
    if not path.is_absolute():
        anchor = working_directory if base == "cwd" else workspace_root or working_directory
        if anchor is not None:
            path = anchor / path
            anchored = True
    if workspace_root is None and not anchored:
        return path
    return _validate_workspace_path(path, workspace_root)


def runtime_working_directory(metadata: dict[str, Any] | None) -> str | None:
    path = _runtime_working_directory(metadata or {})
    return str(path) if path is not None else None


def runtime_workspace_root(metadata: dict[str, Any] | None) -> str | None:
    path = _runtime_workspace_root(metadata or {})
    return str(path) if path is not None else None


def _runtime_working_directory(metadata: dict[str, Any]) -> Path | None:
    raw = metadata.get("working_directory")
    return _path_or_none(raw)


def _runtime_workspace_root(metadata: dict[str, Any]) -> Path | None:
    raw = metadata.get("workspace_root")
    return _path_or_none(raw)


def _path_or_none(raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return Path(os.path.realpath(os.path.expandvars(os.path.expanduser(raw))))
    except OSError:
        return None


def _validate_workspace_path(path: Path, workspace_root: Path | None) -> Path:
    resolved = Path(os.path.realpath(path))
    if workspace_root is None:
        return path
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace root: {resolved}") from exc
    return resolved

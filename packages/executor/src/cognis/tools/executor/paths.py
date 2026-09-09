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
    """Resolve a tool path against runtime cwd/root rules.

    ``workspace_root`` remains available as a future policy hook and as an
    anchor for relative paths, but explicit paths are not sandboxed here.
    Guardrails decide whether a given path access is acceptable.
    """

    metadata = context.runtime_metadata if context is not None else {}
    workspace_root = _runtime_workspace_root(metadata)
    working_directory = _runtime_working_directory(metadata)

    if raw is None or raw == "":
        if default_to_home:
            if working_directory:
                return _normalize_resolved_path(working_directory)
            return Path.home()
        return working_directory or Path("")

    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    anchored = False
    if not path.is_absolute():
        anchor = working_directory if base == "cwd" else workspace_root or working_directory
        if anchor is not None:
            path = anchor / path
    if workspace_root is None and not anchored:
        return path
    return _normalize_resolved_path(path)


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


def _normalize_resolved_path(path: Path) -> Path:
    resolved = Path(os.path.realpath(path))
    return resolved

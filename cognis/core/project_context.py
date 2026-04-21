"""Project-context helpers for dynamically loaded instruction files."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

PROJECT_INSTRUCTIONS_DYNAMIC_SOURCE = "project_instructions_dynamic"
PROJECT_CONTEXT_STATUS_LOADED = "loaded"
PROJECT_CONTEXT_STATUS_MISSING = "missing"


@dataclass(slots=True)
class ProjectContextEntry:
    """Frozen project instruction state scoped to a single Cognis session."""

    project_root: str
    status: str = PROJECT_CONTEXT_STATUS_LOADED
    source_path: str | None = None
    content: str | None = None
    content_hash: str | None = None
    working_directory: str | None = None
    seq: int = 0


def normalize_project_path(path: str | None) -> str | None:
    """Return a stable absolute path string or ``None`` when unavailable."""

    if not isinstance(path, str) or not path.strip():
        return None
    try:
        return os.path.realpath(os.path.expanduser(os.path.expandvars(path)))
    except OSError:
        return None


def build_project_instruction_message(
    *,
    project_root: str,
    source_path: str,
    content: str,
    working_directory: str | None = None,
) -> str:
    """Render the protected instruction message shown to the model."""

    lines = [
        f"Instructions for project at {project_root} loaded from {source_path}.",
        f"Project root: {project_root}",
    ]
    if working_directory:
        lines.append(f"Effective working directory: {working_directory}")
    lines.extend(["", "<project_instructions>", content, "</project_instructions>"])
    return "\n".join(lines)


def project_instruction_hash(content: str) -> str:
    """Return the stable content hash for one instruction payload."""

    return sha256(content.encode("utf-8")).hexdigest()


def project_context_event_data(
    entry: ProjectContextEntry,
    *,
    turn_id: str | None,
) -> dict[str, Any]:
    """Build the persisted Intaris event payload for a loaded project context."""

    if entry.status != PROJECT_CONTEXT_STATUS_LOADED or not entry.content or not entry.source_path:
        raise ValueError("Only loaded project contexts can be persisted as events")
    return {
        "role": "developer",
        "content": entry.content,
        "content_type": "text",
        "source": PROJECT_INSTRUCTIONS_DYNAMIC_SOURCE,
        "turn_id": turn_id,
        "project_root": entry.project_root,
        "source_path": entry.source_path,
        "working_directory": entry.working_directory,
        "content_hash": entry.content_hash,
        "hash": sha256(
            json.dumps(
                {
                    "role": "developer",
                    "content": entry.content,
                    "source": PROJECT_INSTRUCTIONS_DYNAMIC_SOURCE,
                    "project_root": entry.project_root,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def project_context_from_event_data(data: dict[str, Any], *, seq: int) -> ProjectContextEntry | None:
    """Parse a persisted project-context event back into cache state."""

    if data.get("source") != PROJECT_INSTRUCTIONS_DYNAMIC_SOURCE:
        return None
    project_root = normalize_project_path(data.get("project_root"))
    source_path = normalize_project_path(data.get("source_path"))
    content = data.get("content")
    status = data.get("status") or PROJECT_CONTEXT_STATUS_LOADED
    if project_root is None or not isinstance(content, str) or not content.strip():
        return None
    return ProjectContextEntry(
        project_root=project_root,
        status=str(status),
        source_path=source_path,
        content=content,
        content_hash=(
            str(data.get("content_hash"))
            if isinstance(data.get("content_hash"), str)
            else project_instruction_hash(content)
        ),
        working_directory=normalize_project_path(data.get("working_directory")),
        seq=seq,
    )

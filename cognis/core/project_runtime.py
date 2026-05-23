"""Runtime project metadata helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from cognis.core.project_context import (
    ProjectMetadataEntry,
    normalize_project_path,
    project_metadata_hash,
)


def build_project_context_message(project: Any, sources: list[Any], workflow_ids: list[str]) -> str:
    """Render sanitized project metadata for prompt injection."""

    lines = [
        "<project_metadata>",
        f"Project: {project.name}",
        f"Project ID: {project.project_id}",
    ]
    if project.description:
        lines.append(f"Description: {project.description}")
    if project.instructions:
        lines.extend(["", "Instructions:", str(project.instructions)])
    if workflow_ids:
        lines.extend(
            ["", "Project workflow IDs:", *[f"- {workflow_id}" for workflow_id in workflow_ids]]
        )
    if sources:
        lines.extend(["", "Sources:"])
        for source in sources:
            parts = [source.name]
            if source.local_path:
                parts.append(f"local_path={source.local_path}")
            if source.remote_url:
                parts.append(f"remote_url={source.remote_url}")
            if source.default_branch:
                parts.append(f"default_branch={source.default_branch}")
            if source.credential_ref:
                parts.append(f"credential_ref={source.credential_ref}")
            lines.append(f"- {'; '.join(parts)}")
            if source.instructions:
                lines.append(f"  instructions: {source.instructions}")
    lines.append("</project_metadata>")
    return "\n".join(lines)


@dataclass(slots=True)
class ResolvedProjectMetadata:
    """DB project metadata resolved for an explicit project or touched path."""

    project: Any
    sources: list[Any]
    workflow_ids: list[str]
    matched_source: Any | None = None
    project_root: str | None = None


def _path_is_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def project_metadata_entry_from_resolution(
    resolved: ResolvedProjectMetadata,
    *,
    working_directory: str | None = None,
) -> ProjectMetadataEntry:
    """Render resolved DB project metadata as a session-cache entry."""

    content = build_project_context_message(
        resolved.project,
        resolved.sources,
        resolved.workflow_ids,
    )
    return ProjectMetadataEntry(
        project_id=str(resolved.project.project_id),
        project_name=str(resolved.project.name),
        project_root=normalize_project_path(resolved.project_root),
        source_id=(
            str(resolved.matched_source.source_id)
            if resolved.matched_source is not None
            and getattr(resolved.matched_source, "source_id", None)
            else None
        ),
        content=content,
        content_hash=project_metadata_hash(content),
        working_directory=normalize_project_path(working_directory),
    )


async def resolve_project_metadata_for_project_id(
    db_session: Any,
    *,
    user_email: str,
    project_id: str | None,
) -> ResolvedProjectMetadata | None:
    """Resolve visible DB project metadata for an explicit project id."""

    if not isinstance(project_id, str) or not project_id.strip():
        return None
    from cognis.store.queries import (
        list_project_sources,
        list_project_workflow_ids,
        list_projects_for_user,
    )

    projects = await list_projects_for_user(db_session, user_email)
    project = next((item for item in projects if item.project_id == project_id), None)
    if project is None:
        return None
    sources = await list_project_sources(db_session, project.project_id)
    workflow_ids = await list_project_workflow_ids(db_session, project.project_id)
    roots = [normalize_project_path(getattr(source, "local_path", None)) for source in sources]
    project_root = next((root for root in roots if root is not None), None)
    return ResolvedProjectMetadata(
        project=project,
        sources=sources,
        workflow_ids=workflow_ids,
        project_root=project_root,
    )


async def resolve_project_metadata_for_path(
    db_session: Any,
    *,
    user_email: str,
    path: str | None,
    path_kind: str = "directory",
    working_directory: str | None = None,
) -> ResolvedProjectMetadata | None:
    """Resolve visible DB project metadata for a touched filesystem path."""

    normalized_path = normalize_project_path(path)
    if normalized_path is None:
        return None
    if path_kind == "file":
        normalized_path = (
            normalize_project_path(os.path.dirname(normalized_path)) or normalized_path
        )

    from cognis.store.queries import (
        list_project_sources,
        list_project_workflow_ids,
        list_projects_for_user,
    )

    best: tuple[int, str, Any, list[Any], Any, str] | None = None
    for project in await list_projects_for_user(db_session, user_email):
        sources = await list_project_sources(db_session, project.project_id)
        for source in sources:
            source_root = normalize_project_path(getattr(source, "local_path", None))
            if source_root is None or not _path_is_under(normalized_path, source_root):
                continue
            candidate = (len(source_root), str(project.name), project, sources, source, source_root)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        return None
    _, _, project, sources, matched_source, source_root = best
    workflow_ids = await list_project_workflow_ids(db_session, project.project_id)
    return ResolvedProjectMetadata(
        project=project,
        sources=sources,
        workflow_ids=workflow_ids,
        matched_source=matched_source,
        project_root=source_root or normalize_project_path(working_directory),
    )

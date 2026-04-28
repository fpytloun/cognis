"""Runtime project metadata helpers."""

from __future__ import annotations

from typing import Any


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
        lines.extend(["", "Project workflow IDs:", *[f"- {workflow_id}" for workflow_id in workflow_ids]])
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

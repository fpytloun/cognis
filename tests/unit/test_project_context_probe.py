from __future__ import annotations

from pathlib import Path

import pytest

from cognis.models.tool import ExecutorCapabilities, ExecutorHandle
from cognis.tools.executor.project_context import handle_project_context_probe
from cognis.tools.registry import ToolExecutionContext


def _context(*, workspace_root: str | None = None, working_directory: str | None = None) -> ToolExecutionContext:
    runtime_metadata: dict[str, object] = {}
    if workspace_root is not None:
        runtime_metadata["workspace_root"] = workspace_root
    if working_directory is not None:
        runtime_metadata["working_directory"] = working_directory
    return ToolExecutionContext(
        executor_handle=ExecutorHandle(
            executor_id="exec-1",
            executor_type="in_process",
            capabilities=ExecutorCapabilities(),
        ),
        runtime_metadata=runtime_metadata,
        shared_runtime_metadata=runtime_metadata,
    )


@pytest.mark.asyncio
async def test_project_context_probe_loads_nearest_agents_file(tmp_path: Path) -> None:
    project_root = tmp_path / "cognis"
    nested = project_root / "ui" / "src"
    nested.mkdir(parents=True)
    (project_root / ".git").mkdir()
    (project_root / "AGENTS.md").write_text("Use pytest.\n", encoding="utf-8")

    result = await handle_project_context_probe(
        {"path": str(nested), "path_kind": "directory"},
        _context(),
    )

    payload = result.metadata["project_context"]
    assert payload["status"] == "loaded"
    assert payload["project_root"] == str(project_root)
    assert payload["source_path"] == str(project_root / "AGENTS.md")
    assert "Instructions for project at" in payload["content"]


@pytest.mark.asyncio
async def test_project_context_probe_uses_project_hints_under_common_roots(tmp_path: Path) -> None:
    project_root = tmp_path / "src" / "cognis"
    project_root.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text("[project]\nname='cognis'\n", encoding="utf-8")
    (project_root / "AGENTS.md").write_text("Run unit tests.\n", encoding="utf-8")

    result = await handle_project_context_probe(
        {
            "path": None,
            "path_kind": "directory",
            "hint_text": "Inspect the cognis project",
            "fallback_home": str(tmp_path),
        },
        _context(),
    )

    payload = result.metadata["project_context"]
    assert payload["status"] == "loaded"
    assert payload["project_root"] == str(project_root)


@pytest.mark.asyncio
async def test_project_context_probe_allows_explicit_path_outside_current_workspace(
    tmp_path: Path,
) -> None:
    current_workspace = tmp_path / "current"
    current_workspace.mkdir()
    target_project = tmp_path / "obsidian"
    target_project.mkdir()
    (target_project / ".git").mkdir()
    (target_project / "AGENTS.md").write_text("Use markdown notes.\n", encoding="utf-8")

    result = await handle_project_context_probe(
        {"path": str(target_project), "path_kind": "directory"},
        _context(workspace_root=str(current_workspace), working_directory=str(current_workspace)),
    )

    payload = result.metadata["project_context"]
    assert payload["status"] == "loaded"
    assert payload["project_root"] == str(target_project)
    assert payload["source_path"] == str(target_project / "AGENTS.md")

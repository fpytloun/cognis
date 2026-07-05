from __future__ import annotations

import json

import pytest

from cognis.tools.builtin.artifact_tools import handle_artifact_tool

pytest_plugins = ("tests.unit.test_task_continuation_tools",)


def _scoped_metadata(task_id: str) -> dict[str, object]:
    return {
        "conversation_context": {
            "platform_data": {
                "forked_from": "task",
                "task_id": task_id,
            }
        }
    }


@pytest.mark.asyncio
async def test_artifact_tools_read_and_metadata_owned_deliverable(
    task_continuation_db,
) -> None:
    metadata = await handle_artifact_tool(
        "artifact_get_metadata",
        {"artifact_id": "dlv_owner"},
        llm=None,
        artifact_store=object(),
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )
    read = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_owner", "offset": 1, "limit": 2},
        llm=None,
        artifact_store=object(),
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )

    assert metadata.is_error is False
    assert metadata.metadata is not None
    assert metadata.metadata["artifact_id"] == "dlv_owner"
    assert metadata.metadata["source"] == "deliverable"
    assert metadata.metadata["virtual"] is True
    assert metadata.metadata["filename"] == "Full-report.md"
    assert metadata.metadata["mime_type"] == "text/markdown"
    assert metadata.metadata["size_bytes"] == len(b"# Full report\n\nComplete deliverable body.")
    assert metadata.metadata["task_id"] == "task-owner"
    assert json.loads(metadata.output)["download_url_tool"] == "artifact_get_url"

    assert read.is_error is False
    assert "1: # Full report" in read.output
    assert "2: " in read.output
    assert read.metadata is not None
    assert read.metadata["source"] == "deliverable"
    assert read.metadata["virtual"] is True


@pytest.mark.asyncio
async def test_artifact_tools_deny_cross_user_deliverable(task_continuation_db) -> None:
    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_owner"},
        llm=None,
        artifact_store=object(),
        session_factory=task_continuation_db,
        user_email="other@example.com",
    )

    assert result.is_error is True
    assert result.output == "Artifact not found: dlv_owner"


@pytest.mark.asyncio
async def test_artifact_tools_preserve_task_scope_for_deliverable_refs(
    task_continuation_db,
) -> None:
    metadata = await handle_artifact_tool(
        "artifact_get_metadata",
        {"artifact_id": "dlv_sibling"},
        llm=None,
        artifact_store=object(),
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_scoped_metadata("task-owner"),
    )
    read = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "dlv_sibling"},
        llm=None,
        artifact_store=object(),
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_scoped_metadata("task-owner"),
    )
    url = await handle_artifact_tool(
        "artifact_get_url",
        {"artifact_id": "dlv_sibling", "ttl_seconds": 60},
        llm=None,
        artifact_store=object(),
        session_factory=task_continuation_db,
        user_email="owner@example.com",
        runtime_metadata=_scoped_metadata("task-owner"),
    )

    assert metadata.is_error is True
    assert metadata.output == "Artifact not found: dlv_sibling"
    assert read.is_error is True
    assert read.output == "Artifact not found: dlv_sibling"
    assert url.is_error is True
    assert url.output == "Artifact not found: dlv_sibling"


@pytest.mark.asyncio
async def test_artifact_get_url_returns_virtual_deliverable_url(task_continuation_db) -> None:
    class _Store:
        _config = type("Config", (), {"base_url": "https://cognis.test", "signing_secret": "s"})()

        def _filesystem_signature(
            self, namespace: str, object_id: str, filename: str, exp: int, *, mode: str = "download"
        ) -> str:
            return f"sig-{namespace}-{object_id}-{filename}-{exp}-{mode}"

    result = await handle_artifact_tool(
        "artifact_get_url",
        {"artifact_id": "dlv_owner", "ttl_seconds": 60},
        llm=None,
        artifact_store=_Store(),
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["source"] == "deliverable"
    assert result.metadata["virtual"] is True
    assert (
        "/api/v1/artifacts/virtual/deliverables/dlv_owner/Full-report.md" in result.metadata["url"]
    )
    assert "sig=sig-deliverables-dlv_owner-Full-report.md-" in result.metadata["url"]
    assert result.metadata["mode"] == "download"


@pytest.mark.asyncio
async def test_artifact_get_url_rejects_virtual_deliverable_view_for_non_html(
    task_continuation_db,
) -> None:
    result = await handle_artifact_tool(
        "artifact_get_url",
        {"artifact_id": "dlv_owner", "ttl_seconds": 60, "mode": "view"},
        llm=None,
        artifact_store=object(),
        session_factory=task_continuation_db,
        user_email="owner@example.com",
    )

    assert result.is_error is True
    assert result.output == "Artifact view is only supported for HTML artifacts: dlv_owner"

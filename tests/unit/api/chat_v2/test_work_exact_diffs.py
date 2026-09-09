from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.chat_v2.schemas import FileDiffRef, TimelineScope, ToolCallTimelineItem
from cognis.api.chat_v2.work_repository import _hydrate_file_items
from cognis.api.routes.work import _project_exact_file_diff


def _file_fact() -> SimpleNamespace:
    return SimpleNamespace(
        work_record_id="record-1",
        file_ordinal=0,
        path="/srv/repo/src/app.py",
        path_id="root:src/app.py",
        path_generation_id="generation-1",
        additions=2,
        deletions=1,
        status="modified",
        old_path=None,
        binary=False,
        generated=False,
        truncated=False,
        preview_omitted=True,
    )


@pytest.mark.asyncio
async def test_hydrate_file_items_restores_file_history_identity() -> None:
    fact = _file_fact()
    db = AsyncMock()
    db.scalars.return_value = SimpleNamespace(all=lambda: [fact])
    item = ToolCallTimelineItem(
        id="tool:call-1",
        call_id="call-1",
        tool_name="apply_patch",
        sort_key="1",
        file_diffs=[
            FileDiffRef(
                path="/srv/repo/src/app.py",
                diff="",
                preview_omitted=True,
                preview_omission_reason="not_persisted",
            )
        ],
    )

    hydrated = await _hydrate_file_items(
        db,
        records=[SimpleNamespace(work_record_id="record-1")],
        items=[item],
    )

    diff = hydrated[0].file_diffs[0]  # type: ignore[union-attr]
    assert diff.path_id == "root:src/app.py"
    assert diff.path_generation_id == "generation-1"
    assert diff.additions == 2
    assert diff.deletions == 1


def test_project_exact_file_diff_preserves_source_and_generation_identity() -> None:
    exact = _project_exact_file_diff(
        items=[
            ToolCallTimelineItem(
                id="tool:call-1",
                call_id="call-1",
                tool_name="apply_patch",
                sort_key="1",
                status="complete",
                file_diffs=[
                    FileDiffRef(
                        path="/srv/repo/src/app.py",
                        diff="@@ -1 +1 @@\n-old\n+new",
                    )
                ],
            )
        ],
        source_item_id="tool:call-1",
        file_ordinal=0,
        fact=_file_fact(),
        scope=TimelineScope(
            key="session:session-1",
            kind="session",
            session_id="session-1",
        ),
        projection_version="work-v7",
    )

    assert exact is not None
    assert exact.diff == "@@ -1 +1 @@\n-old\n+new"
    assert exact.source_item_id == "tool:call-1"
    assert exact.model_dump()["source_item_id"] == "tool:call-1"
    assert exact.path_generation_id == "generation-1"
    assert exact.path_id == "root:src/app.py"

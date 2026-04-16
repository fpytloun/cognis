from __future__ import annotations

import pytest

from cognis.core.tool_output_store import FilesystemToolOutputBackend, ToolOutputStore
from cognis.tools.builtin.tool_output import handle_tool_output_tool


@pytest.fixture
def store(tmp_path) -> ToolOutputStore:
    return ToolOutputStore(FilesystemToolOutputBackend(tmp_path), ttl_hours=1, max_size_mb=1)


@pytest.mark.asyncio
async def test_list_tool_output_anchors_returns_saved_anchors(store: ToolOutputStore) -> None:
    await store.save(
        "call_1",
        "[[result:1]]\nFirst\n\n[[result:2]]\nSecond",
        anchors=[
            {
                "anchor": "result:1",
                "label": "First",
                "kind": "search_result",
                "start_line": 1,
                "end_line": 2,
            },
            {
                "anchor": "result:2",
                "label": "Second",
                "kind": "search_result",
                "start_line": 4,
                "end_line": 5,
            },
        ],
    )

    result = await handle_tool_output_tool("list_tool_output_anchors", {"call_id": "call_1"}, store)
    assert not result.is_error
    assert "result:1" in result.output
    assert "search_result" in result.output


@pytest.mark.asyncio
async def test_read_tool_output_anchor_returns_only_requested_section(
    store: ToolOutputStore,
) -> None:
    await store.save(
        "call_2",
        "[[result:1]]\nFirst\n\n[[result:2]]\nSecond",
        anchors=[
            {
                "anchor": "result:1",
                "label": "First",
                "kind": "search_result",
                "start_line": 1,
                "end_line": 2,
            },
            {
                "anchor": "result:2",
                "label": "Second",
                "kind": "search_result",
                "start_line": 4,
                "end_line": 5,
            },
        ],
    )

    result = await handle_tool_output_tool(
        "read_tool_output_anchor",
        {"call_id": "call_2", "anchor": "result:2"},
        store,
    )
    assert not result.is_error
    assert "Anchor 'result:2'" in result.output
    assert "4: [[result:2]]" in result.output
    assert "5: Second" in result.output
    assert "1: [[result:1]]" not in result.output


@pytest.mark.asyncio
async def test_read_tool_output_anchor_reports_available_anchors(store: ToolOutputStore) -> None:
    await store.save(
        "call_3",
        "[[result:1]]\nFirst",
        anchors=[
            {
                "anchor": "result:1",
                "label": "First",
                "kind": "search_result",
                "start_line": 1,
                "end_line": 2,
            }
        ],
    )

    result = await handle_tool_output_tool(
        "read_tool_output_anchor",
        {"call_id": "call_3", "anchor": "result:2"},
        store,
    )
    assert result.is_error
    assert "Available anchors: result:1" in result.output


@pytest.mark.asyncio
async def test_read_tool_output_anchor_clamps_negative_context(store: ToolOutputStore) -> None:
    await store.save(
        "call_4",
        "[[result:1]]\nFirst",
        anchors=[
            {
                "anchor": "result:1",
                "label": "First",
                "kind": "search_result",
                "start_line": 1,
                "end_line": 2,
            }
        ],
    )

    result = await handle_tool_output_tool(
        "read_tool_output_anchor",
        {"call_id": "call_4", "anchor": "result:1", "before_lines": -5, "after_lines": -3},
        store,
    )
    assert not result.is_error
    assert "1: [[result:1]]" in result.output

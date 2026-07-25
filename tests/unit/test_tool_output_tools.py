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
    assert result.metadata == {"source_call_id": "call_1", "original_size": len(result.output)}


@pytest.mark.asyncio
async def test_read_tool_output_critical_pressure_clamps_large_default_slice(
    store: ToolOutputStore,
) -> None:
    content = "\n".join(f"line {index} {'x' * 120}" for index in range(1, 260))
    await store.save("call_large", content)

    result = await handle_tool_output_tool(
        "read_tool_output",
        {"call_id": "call_large"},
        store,
        pressure_mode="critical",
    )

    assert not result.is_error
    assert len(result.output) <= 12_000
    assert "Tool output recovery truncated under critical context pressure" in result.output
    assert "Use offset/limit" in result.output


@pytest.mark.asyncio
async def test_read_tool_output_critical_pressure_honors_explicit_small_limit(
    store: ToolOutputStore,
) -> None:
    content = "\n".join(f"line {index} {'x' * 120}" for index in range(1, 260))
    await store.save("call_small", content)

    result = await handle_tool_output_tool(
        "read_tool_output",
        {"call_id": "call_small", "limit": 5},
        store,
        pressure_mode="critical",
    )

    assert not result.is_error
    assert "Tool output recovery truncated under critical context pressure" not in result.output
    assert "1: line 1" in result.output
    assert "5: line 5" in result.output
    assert "6: line 6" not in result.output


@pytest.mark.asyncio
async def test_read_tool_output_normal_pressure_keeps_existing_large_slice(
    store: ToolOutputStore,
) -> None:
    content = "\n".join(f"line {index} {'x' * 120}" for index in range(1, 260))
    await store.save("call_normal", content)

    result = await handle_tool_output_tool(
        "read_tool_output",
        {"call_id": "call_normal"},
        store,
        pressure_mode="normal",
    )

    assert not result.is_error
    assert len(result.output) > 12_000
    assert "Tool output recovery truncated under" not in result.output


@pytest.mark.asyncio
async def test_list_tool_output_anchors_does_not_duplicate_saved_markdown_anchors(
    store: ToolOutputStore,
) -> None:
    content = "# Summary\nBody\n\n## Verdict\nDone\n"
    await store.save(
        "call_markdown",
        content,
        anchors=[
            {
                "anchor": "heading:summary",
                "label": "Summary",
                "kind": "markdown_heading",
                "start_line": 1,
                "end_line": 3,
            },
            {
                "anchor": "heading:verdict",
                "label": "Verdict",
                "kind": "markdown_heading",
                "start_line": 4,
                "end_line": 5,
            },
        ],
    )

    result = await handle_tool_output_tool(
        "list_tool_output_anchors",
        {"call_id": "call_markdown"},
        store,
    )

    assert not result.is_error
    assert result.output.count("heading:summary") == 1
    assert result.output.count("heading:verdict") == 1
    assert "heading:summary-2" not in result.output


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
async def test_delegate_assistant_message_anchors_can_be_listed_and_read(
    store: ToolOutputStore,
) -> None:
    content = "[assistant_message:1]\nFull report\n\n---\n\n[assistant_message:2]\nCleanup"
    anchors = [
        {
            "anchor": "assistant_message:1",
            "label": "Assistant message 1",
            "kind": "assistant_message",
            "start_line": 1,
            "end_line": 2,
        },
        {
            "anchor": "assistant_message:2",
            "label": "Assistant message 2",
            "kind": "assistant_message",
            "start_line": 6,
            "end_line": 7,
        },
    ]
    await store.save("delegate_call", content, anchors=anchors)

    listed = await handle_tool_output_tool(
        "list_tool_output_anchors", {"call_id": "delegate_call"}, store
    )
    assert not listed.is_error
    assert "assistant_message:1" in listed.output
    assert "assistant_message" in listed.output

    section = await handle_tool_output_tool(
        "read_tool_output_anchor",
        {"call_id": "delegate_call", "anchor": "assistant_message:1"},
        store,
    )
    assert not section.is_error
    assert "Full report" in section.output
    assert "Cleanup" not in section.output


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

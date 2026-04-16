"""Tests for the ToolOutputStore."""

from __future__ import annotations

import pytest

from cognis.core.tool_output_store import FilesystemToolOutputBackend, ToolOutputStore


@pytest.fixture
def store(tmp_path):
    backend = FilesystemToolOutputBackend(tmp_path)
    return ToolOutputStore(backend, ttl_hours=1, max_size_mb=1)


@pytest.mark.asyncio
async def test_save_and_read(store: ToolOutputStore) -> None:
    await store.save("call_1", "line1\nline2\nline3\nline4\nline5")
    result = await store.read("call_1")
    assert result is not None
    assert result.total_lines == 5
    assert "1: line1" in result.content
    assert "5: line5" in result.content
    assert result.has_more is False


@pytest.mark.asyncio
async def test_read_with_offset_and_limit(store: ToolOutputStore) -> None:
    lines = "\n".join(f"line {i}" for i in range(1, 101))
    await store.save("call_2", lines)

    result = await store.read("call_2", offset=10, limit=5)
    assert result is not None
    assert result.total_lines == 100
    assert result.offset == 10
    assert result.limit == 5
    assert result.has_more is True
    assert "10: line 10" in result.content
    assert "14: line 14" in result.content


@pytest.mark.asyncio
async def test_read_nonexistent_returns_none(store: ToolOutputStore) -> None:
    result = await store.read("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_search_finds_matches(store: ToolOutputStore) -> None:
    content = "apple\nbanana\ncherry\napricot\nblueberry"
    await store.save("call_3", content)

    result = await store.search("call_3", "ap")
    assert result is not None
    assert result.total_matches == 2
    assert result.matches[0].line_number == 1
    assert "apple" in result.matches[0].line
    assert result.matches[1].line_number == 4
    assert "apricot" in result.matches[1].line


@pytest.mark.asyncio
async def test_search_with_context_lines(store: ToolOutputStore) -> None:
    content = "\n".join(f"line {i}" for i in range(20))
    await store.save("call_4", content)

    result = await store.search("call_4", "line 10", context_lines=2)
    assert result is not None
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.line_number == 11  # 1-indexed
    assert len(match.context_before) == 2
    assert len(match.context_after) == 2


@pytest.mark.asyncio
async def test_search_nonexistent_returns_none(store: ToolOutputStore) -> None:
    result = await store.search("nonexistent", "pattern")
    assert result is None


@pytest.mark.asyncio
async def test_search_invalid_regex(store: ToolOutputStore) -> None:
    await store.save("call_5", "some content")
    result = await store.search("call_5", "[invalid")
    assert result is not None
    assert result.total_matches == 0


@pytest.mark.asyncio
async def test_exists_and_delete(store: ToolOutputStore) -> None:
    await store.save("call_6", "data")
    assert await store.exists("call_6") is True

    await store.delete("call_6")
    assert await store.exists("call_6") is False


@pytest.mark.asyncio
async def test_cleanup_session(store: ToolOutputStore) -> None:
    await store.save("call_a", "data a")
    await store.save("call_b", "data b")
    await store.save("call_c", "data c")

    await store.cleanup_session(["call_a", "call_c"])
    assert await store.exists("call_a") is False
    assert await store.exists("call_b") is True
    assert await store.exists("call_c") is False


@pytest.mark.asyncio
async def test_enforce_size_cap(store: ToolOutputStore) -> None:
    # Store is configured with max_size_mb=1 (1MB)
    # Write files that exceed the cap
    big_content = "x" * (600 * 1024)  # 600KB each
    await store.save("old_call", big_content)
    await store.save("new_call", big_content)

    deleted = await store.enforce_size_cap()
    assert deleted >= 1
    # The newer file should survive
    assert await store.exists("new_call") is True


@pytest.mark.asyncio
async def test_long_lines_truncated(store: ToolOutputStore) -> None:
    long_line = "x" * 5000
    await store.save("call_long", long_line)
    result = await store.read("call_long")
    assert result is not None
    assert "line truncated" in result.content


@pytest.mark.asyncio
async def test_save_and_list_anchors(store: ToolOutputStore) -> None:
    await store.save(
        "call_anchor",
        "[[result:1]]\nfirst\n[[result:2]]\nsecond",
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
                "start_line": 3,
                "end_line": 4,
            },
        ],
    )

    anchors = await store.list_anchors("call_anchor")
    assert anchors is not None
    assert [item.anchor for item in anchors] == ["result:1", "result:2"]
    assert anchors[0].label == "First"


@pytest.mark.asyncio
async def test_read_anchor_returns_section(store: ToolOutputStore) -> None:
    await store.save(
        "call_anchor_read",
        "[[answer]]\nAnswer: short\n\n[[result:1]]\n[1] Title\n    URL: https://example.com\n\n[[result:2]]\n[2] Other",
        anchors=[
            {
                "anchor": "answer",
                "label": "Answer",
                "kind": "answer",
                "start_line": 1,
                "end_line": 2,
            },
            {
                "anchor": "result:1",
                "label": "Title",
                "kind": "search_result",
                "start_line": 4,
                "end_line": 6,
            },
            {
                "anchor": "result:2",
                "label": "Other",
                "kind": "search_result",
                "start_line": 8,
                "end_line": 9,
            },
        ],
    )

    result = await store.read_anchor("call_anchor_read", "result:1")
    assert result is not None
    assert result.anchor.anchor == "result:1"
    assert "4: [[result:1]]" in result.content
    assert "6:     URL: https://example.com" in result.content
    assert "8: [[result:2]]" not in result.content


@pytest.mark.asyncio
async def test_list_anchors_falls_back_to_inline_markers(store: ToolOutputStore) -> None:
    await store.save(
        "call_inline",
        "[[result:1]]\nFirst\n\n[[result:2]]\nSecond",
        anchors=[],
    )

    anchors = await store.list_anchors("call_inline")
    assert anchors is not None
    assert [item.anchor for item in anchors] == ["result:1", "result:2"]
    assert anchors[0].kind == "search_result"

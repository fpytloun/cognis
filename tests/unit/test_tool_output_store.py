"""Tests for the ToolOutputStore."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

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
@pytest.mark.parametrize(
    ("content", "record", "expected"),
    [
        (
            '{"data":{"items":[1,2]}}',
            {
                "anchor_id": "anc_json",
                "anchor": "json:data",
                "kind": "json.array",
                "format": "json",
                "label": "Items",
                "locator": {"type": "stored_json", "pointer": "/data/items"},
                "recovery_op": "read_json",
                "priority": 80,
                "promote": True,
            },
            "[\n  1,\n  2\n]",
        ),
        (
            "name,value\na,1\nb,2\nc,3",
            {
                "anchor_id": "anc_rows",
                "anchor": "rows:1",
                "kind": "table.rows",
                "format": "table",
                "label": "Rows",
                "locator": {"type": "stored_rows", "start_row": 2, "end_row": 3},
                "recovery_op": "read_rows",
                "priority": 50,
                "promote": False,
            },
            "a,1\nb,2",
        ),
        (
            "binary attachment",
            {
                "anchor_id": "anc_page",
                "anchor": "page:2",
                "kind": "artifact.page",
                "format": "pdf",
                "label": "Page 2",
                "locator": {"type": "artifact_part", "page": 2},
                "recovery_op": "read_artifact_part",
                "priority": 50,
                "promote": False,
            },
            '"page": 2',
        ),
    ],
)
async def test_format_aware_manifest_round_trip_and_recovery(
    store: ToolOutputStore,
    content: str,
    record: dict[str, object],
    expected: str,
) -> None:
    manifest = {"schema_version": 1, "adapter_id": "test-v1", "anchors": [record]}
    persisted = await store.save(
        "call_format",
        content,
        anchors=[record],
        anchor_manifest=manifest,
    )

    assert persisted == [record]
    anchors = await store.list_anchors("call_format")
    assert anchors is not None
    assert anchors[0].anchor_id == record["anchor_id"]
    recovered = await store.read_anchor("call_format", str(record["anchor"]))
    assert recovered is not None
    assert expected in recovered.content


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
async def test_cleanup_expired_removes_only_stale_outputs(tmp_path: Path) -> None:
    backend = FilesystemToolOutputBackend(tmp_path)
    store = ToolOutputStore(backend, ttl_hours=1, max_size_mb=1)
    await store.save("stale_call", "stale")
    await store.save("fresh_call", "fresh")
    stale_time = time.time() - 7200
    os.utime(tmp_path / "tool-outputs" / "stale_call.txt", (stale_time, stale_time))

    deleted = await store.cleanup_expired()

    assert deleted == 1
    assert await store.exists("stale_call") is False
    assert await store.exists("fresh_call") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "sync_method_name", "argument"),
    [
        ("cleanup_expired", "_sync_cleanup_expired", 3600),
        ("enforce_size_cap", "_sync_enforce_size_cap", 1024),
    ],
)
async def test_filesystem_bulk_maintenance_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method_name: str,
    sync_method_name: str,
    argument: int,
) -> None:
    backend = FilesystemToolOutputBackend(tmp_path)
    release = threading.Event()

    def _slow_operation(_: int) -> int:
        release.wait(timeout=1.0)
        return 0

    monkeypatch.setattr(backend, sync_method_name, _slow_operation)
    operation = asyncio.create_task(getattr(backend, method_name)(argument))
    await asyncio.sleep(0.01)

    assert operation.done() is False
    release.set()
    assert await operation == 0


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


@pytest.mark.asyncio
async def test_list_anchors_derives_markdown_headings(store: ToolOutputStore) -> None:
    await store.save(
        "call_markdown",
        "# Summary\nTop-level text\n\n```md\n## Ignored\n```\n\n## Must Fix\nDetails\n\n#### Too Deep\nIgnored\n\n## Verdict\nDone",
        anchors=[],
    )

    anchors = await store.list_anchors("call_markdown")
    assert anchors is not None
    assert [item.anchor for item in anchors] == [
        "heading:summary",
        "heading:must-fix",
        "heading:verdict",
    ]
    assert anchors[0].kind == "markdown_heading"
    assert anchors[0].start_line == 1
    assert anchors[0].end_line == 15
    assert anchors[1].start_line == 8

    result = await store.read_anchor("call_markdown", "heading:must-fix")
    assert result is not None
    assert "8: ## Must Fix" in result.content
    assert "9: Details" in result.content
    assert "14: ## Verdict" not in result.content


@pytest.mark.asyncio
async def test_list_anchors_supplements_explicit_anchors_with_markdown(
    store: ToolOutputStore,
) -> None:
    await store.save(
        "call_explicit_markdown",
        "[[message:1]]\n--- Assistant message 1 ---\n### Summary\nBody",
        anchors=[
            {
                "anchor": "message:1",
                "label": "Assistant message 1",
                "kind": "section",
                "start_line": 1,
                "end_line": 4,
            }
        ],
    )

    anchors = await store.list_anchors("call_explicit_markdown")
    assert anchors is not None
    assert [item.anchor for item in anchors] == ["message:1", "heading:summary"]

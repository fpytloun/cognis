"""Tests for compaction recovery-handle summaries."""

from __future__ import annotations

from types import SimpleNamespace

from cognis.core.compaction.recovery import (
    append_recoverable_tool_output_handles,
    recoverable_tool_output_lines,
)
from cognis.core.context import _format_compaction_summary


def test_capped_recovery_handles_do_not_recommend_nonexistent_tool() -> None:
    events = [
        SimpleNamespace(
            type="tool_result",
            seq=index,
            data={
                "name": "read",
                "call_id": f"call-{index}",
                "recovery_call_id": f"call-{index}",
                "has_full_output": True,
                "output_size": 1_000 - index,
            },
        )
        for index in range(3)
    ]

    lines = recoverable_tool_output_lines(events, max_entries=1)

    assert len(lines) == 2
    assert "2 additional recoverable outputs omitted" in lines[-1]
    assert "list_tool_outputs" not in lines[-1]


def test_recovery_handle_pairs_bash_description_with_result() -> None:
    events = [
        SimpleNamespace(
            type="tool_call",
            seq=1,
            data={
                "name": "bash",
                "call_id": "call-build",
                "arguments": {
                    "command": "uv run pytest tests/unit/test_compaction.py",
                    "description": "Run focused compaction tests.",
                },
            },
        ),
        SimpleNamespace(
            type="tool_result",
            seq=2,
            data={
                "name": "bash",
                "call_id": "call-build",
                "recovery_call_id": "call-build",
                "has_full_output": True,
            },
        ),
    ]

    lines = recoverable_tool_output_lines(events)

    assert lines == [
        "- [2] bash — Run focused compaction tests. call_id='call-build'",
    ]
    assert "uv run pytest" not in lines[0]


def test_existing_recovery_section_is_replaced_by_current_index() -> None:
    summary = "## Recoverable Tool Evidence\n- (none)"
    events = [
        SimpleNamespace(
            type="tool_result",
            seq=2,
            data={
                "name": "bash",
                "call_id": "call-build",
                "recovery_call_id": "call-build",
                "has_full_output": True,
            },
        )
    ]

    result = append_recoverable_tool_output_handles(summary, events)

    assert result.count("## Recoverable Tool Evidence") == 1
    assert "- (none)" not in result
    assert "call_id='call-build'" in result


def test_fallback_replaces_previous_recovery_section() -> None:
    from cognis.core.compaction.fallback import build_sliding_window_summary

    previous_summary = "## Goal\n- Continue\n\n## Recoverable Tool Evidence\n- stale"
    events = [
        SimpleNamespace(
            type="tool_result",
            seq=2,
            data={
                "name": "bash",
                "call_id": "call-current",
                "recovery_call_id": "call-current",
                "has_full_output": True,
            },
        )
    ]

    result = build_sliding_window_summary(events, previous_summary=previous_summary)

    assert result.count("## Recoverable Tool Evidence") == 1
    assert "stale" not in result
    assert "call_id='call-current'" in result


def test_continuation_summary_explains_conversation_history_recovery() -> None:
    formatted = _format_compaction_summary("## Goal\n- Continue")

    assert formatted is not None
    assert "search_conversations" in formatted
    assert "read_conversation_messages" in formatted
    assert "only when the specific raw evidence is needed" in formatted

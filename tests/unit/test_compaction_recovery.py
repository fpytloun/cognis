"""Tests for compaction recovery-handle summaries."""

from __future__ import annotations

from types import SimpleNamespace

from cognis.core.compaction.recovery import recoverable_tool_output_lines


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

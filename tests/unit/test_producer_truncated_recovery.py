from cognis.core.compaction.input_format import tool_result_recovery_hint
from cognis.core.context_projection import build_compacted_tool_result_placeholder
from cognis.core.tool_output_presentation import build_transport_tool_output_preview


def test_producer_truncation_mentions_real_call_id_without_fabricating_recovery() -> None:
    data = {
        "call_id": "call_real",
        "producer_truncated": True,
        "result": "preview",
    }

    hint = tool_result_recovery_hint(data)
    assert hint is not None
    assert "call_real" in hint
    assert "no controller recovery handle" in hint

    placeholder = build_compacted_tool_result_placeholder(
        {
            "tool_name": "web_fetch",
            "tool_call_id": "call_real",
            "_producer_truncated": True,
        }
    )
    assert "call_real" in placeholder
    assert "diagnostic only" in placeholder
    assert "read_tool_output" not in placeholder


def test_transport_preview_carries_real_producer_call_id_guidance() -> None:
    preview = build_transport_tool_output_preview(
        "preview",
        200,
        metadata={
            "call_id": "call_real",
            "producer_truncated": True,
        },
    )

    assert "call_real" in preview.result
    assert "no controller recovery handle" in preview.result
    assert preview.producer_truncated is True

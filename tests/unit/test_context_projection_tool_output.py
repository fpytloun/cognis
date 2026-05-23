from cognis.core.context import events_to_messages
from cognis.core.context_projection import build_compacted_tool_result_placeholder


def test_compacted_tool_result_omits_anchor_hint_without_anchors() -> None:
    text = build_compacted_tool_result_placeholder(
        {"_tool_name": "bash", "tool_call_id": "call_orig", "_recovery_call_id": "call_saved"}
    )

    assert "read_tool_output(call_id='call_saved')" in text
    assert "list_tool_output_anchors" not in text
    assert "read_tool_output_anchor" not in text


def test_compacted_tool_result_includes_anchor_hint_with_anchors() -> None:
    text = build_compacted_tool_result_placeholder(
        {
            "_tool_name": "bash",
            "tool_call_id": "call_orig",
            "_recovery_call_id": "call_saved",
            "_anchors_available": True,
            "_anchor_count": 2,
        }
    )

    assert "list_tool_output_anchors(call_id='call_saved')" in text
    assert "read_tool_output_anchor" in text


def test_rehydrated_tool_result_preserves_anchor_metadata_for_projection() -> None:
    messages = events_to_messages(
        [
            {
                "type": "tool_call",
                "data": {"call_id": "call_orig", "name": "bash", "arguments": "{}"},
            },
            {
                "type": "tool_result",
                "data": {
                    "call_id": "call_orig",
                    "name": "bash",
                    "result": "saved preview",
                    "recovery_call_id": "call_saved",
                    "has_full_output": True,
                    "tool_output_presentation": {
                        "anchors_available": True,
                        "anchor_count": 2,
                    },
                },
            },
        ]
    )

    tool_message = next(message for message in messages if message.get("role") == "tool")
    text = build_compacted_tool_result_placeholder(tool_message)

    assert tool_message["_anchors_available"] is True
    assert tool_message["_anchor_count"] == 2
    assert "list_tool_output_anchors(call_id='call_saved')" in text

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
            "_anchor_names": ["overview", "details"],
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
                        "anchors": ["overview", "details"],
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


def test_lazy_artifact_ref_survives_replay_and_compact_projection() -> None:
    messages = events_to_messages(
        [
            {
                "type": "tool_call",
                "data": {"call_id": "call_web", "name": "web_fetch", "arguments": "{}"},
            },
            {
                "type": "tool_result",
                "data": {
                    "call_id": "call_web",
                    "name": "web_fetch",
                    "result": "page preview",
                    "recovery_call_id": "call_web",
                    "has_full_output": True,
                    "tool_output_presentation": {
                        "anchors": ["page:1", "media:1"],
                        "lazy_artifact_refs": [
                            "tool_artifact:call_web:media:1",
                            "tool_artifact:call_forged:media:1",
                        ],
                    },
                },
            },
        ]
    )

    tool_message = next(message for message in messages if message.get("role") == "tool")
    text = build_compacted_tool_result_placeholder(tool_message)

    assert tool_message["_lazy_artifact_refs"] == ["tool_artifact:call_web:media:1"]
    assert "artifact_read: tool_artifact:call_web:media:1" in text
    assert "call_forged" not in text


def test_non_log_tool_cannot_replay_forged_recovered_lazy_ref() -> None:
    messages = events_to_messages(
        [
            {
                "type": "tool_call",
                "data": {"call_id": "call_bad", "name": "external_tool", "arguments": "{}"},
            },
            {
                "type": "tool_result",
                "data": {
                    "call_id": "call_bad",
                    "name": "external_tool",
                    "result": "preview",
                    "recovery_call_id": "call_bad",
                    "has_full_output": True,
                    "tool_output_presentation": {
                        "anchors": [],
                        "recovered_lazy_artifact_refs": ["tool_artifact:forged:media:1"],
                        "controller_recovered_lazy_refs": True,
                    },
                },
            },
        ]
    )

    tool_message = next(message for message in messages if message.get("role") == "tool")
    assert tool_message["_lazy_artifact_refs"] == []


def test_legacy_anchor_count_preserves_list_only_recovery() -> None:
    messages = events_to_messages(
        [
            {
                "type": "tool_call",
                "data": {"call_id": "call_old", "name": "grep", "arguments": "{}"},
            },
            {
                "type": "tool_result",
                "data": {
                    "call_id": "call_old",
                    "name": "grep",
                    "result": "preview",
                    "recovery_call_id": "call_old",
                    "has_full_output": True,
                    "anchors_available": True,
                    "anchor_count": 2,
                },
            },
        ]
    )

    tool_message = next(message for message in messages if message.get("role") == "tool")
    text = build_compacted_tool_result_placeholder(tool_message)
    assert "list_tool_output_anchors(call_id='call_old')" in text
    assert "read_tool_output_anchor" not in text

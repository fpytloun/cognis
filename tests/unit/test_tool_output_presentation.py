from cognis.core.tool_output_presentation import (
    build_transport_tool_output_preview,
    present_tool_output,
)


def test_small_tool_output_remains_unchanged() -> None:
    presentation = present_tool_output("hello", 1000)

    assert presentation.result == "hello"
    assert not presentation.truncated
    assert presentation.output_size == 5


def test_large_tool_output_uses_middle_truncation() -> None:
    text = "A" * 800 + "B" * 800
    presentation = present_tool_output(text, 700, has_full_output=True, recovery_call_id="call_1")

    assert presentation.truncated
    assert presentation.result.startswith("A")
    assert presentation.result.endswith("B" * 100)
    assert "middle truncated" in presentation.result


def test_recovery_hint_omits_anchor_tools_without_anchors() -> None:
    text = "x" * 1600
    presentation = present_tool_output(text, 700, has_full_output=True, recovery_call_id="call_1")

    assert "read_tool_output(call_id='call_1')" in presentation.result
    assert "list_tool_output_anchors" not in presentation.result
    assert "read_tool_output_anchor" not in presentation.result


def test_recovery_hint_includes_anchor_tools_only_with_anchors() -> None:
    text = "x" * 1600
    presentation = present_tool_output(
        text,
        700,
        has_full_output=True,
        recovery_call_id="call_1",
        anchors=["errors"],
    )

    assert "list_tool_output_anchors(call_id='call_1')" in presentation.result
    assert "read_tool_output_anchor(call_id='call_1', anchor='errors')" in presentation.result
    assert presentation.anchors_available
    assert presentation.anchor_count == 1


def test_transport_preview_middle_truncates_and_marks_metadata() -> None:
    text = "head" + ("x" * 2000) + "tail"
    presentation = build_transport_tool_output_preview(text, 700)

    assert presentation.transport_truncated
    assert "middle truncated" in presentation.result
    assert presentation.result.startswith("head")
    assert presentation.result.endswith("tail")

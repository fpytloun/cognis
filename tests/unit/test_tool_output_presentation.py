import pytest

from cognis.core.tool_output_presentation import (
    build_transport_tool_output_preview,
    present_tool_output,
    safe_anchor_name,
    safe_output_anchors,
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


def test_anchor_names_are_bounded_while_candidates_remain_store_private() -> None:
    anchors = safe_output_anchors(
        [
            {
                "anchor": "  section/\nwith spaces  ",
                "label": "A" * 200,
                "kind": "media",
                "start_line": 1,
                "end_line": 2,
                "artifact_candidate": {"url": "https://remote.invalid/image.png"},
            },
            {
                "anchor": "section/with spaces",
                "kind": "duplicate",
                "start_line": 3,
                "end_line": 3,
            },
            {"anchor": "x" * 500, "kind": "section", "start_line": 4, "end_line": 5},
            {"anchor": "malformed"},
        ]
    )

    assert [item["anchor"] for item in anchors] == ["section-with-spaces", "x" * 120]
    assert anchors[0]["artifact_candidate"] == {"url": "https://remote.invalid/image.png"}
    assert safe_anchor_name("\x00 \t") is None


def test_transport_replay_uses_only_concrete_anchor_names() -> None:
    presentation = build_transport_tool_output_preview(
        "x" * 1600,
        700,
        metadata={"anchors_available": True, "anchor_count": 99, "anchors": ["summary"]},
    )

    assert "list_tool_output_anchors(call_id" not in presentation.result
    assert presentation.anchors == ("summary",)


@pytest.mark.parametrize("anchor_position", [0, 6_000, 11_900])
def test_presentation_promotes_lazy_ref_independent_of_raw_anchor_position(
    anchor_position: int,
) -> None:
    raw = ("x" * anchor_position) + "[[media:1]]" + ("y" * (12_000 - anchor_position))
    presentation = present_tool_output(
        raw,
        900,
        recovery_call_id="call_media",
        has_full_output=True,
        anchors=["page:1", "media:1"],
        lazy_artifact_anchors=["media:1"],
    )

    assert presentation.lazy_artifact_refs == ("tool_artifact:call_media:media:1",)
    assert presentation.metadata()["lazy_artifact_refs"] == ["tool_artifact:call_media:media:1"]


def test_transport_drops_forged_lazy_refs() -> None:
    presentation = build_transport_tool_output_preview(
        "content",
        900,
        metadata={
            "has_full_output": True,
            "recovery_call_id": "call_real",
            "anchors": ["media:1"],
            "lazy_artifact_refs": [
                "tool_artifact:call_real:media:1",
                "tool_artifact:call_other:media:1",
                "https://remote.invalid/image.png",
            ],
        },
    )

    assert presentation.lazy_artifact_refs == ("tool_artifact:call_real:media:1",)

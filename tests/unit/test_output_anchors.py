from __future__ import annotations

import pytest

from cognis.core.output_anchor_registry import build_anchor_manifest


@pytest.mark.parametrize(
    ("draft", "expected_format", "expected_recovery"),
    [
        (
            {"anchor": "tail:1", "kind": "warning", "start_line": 90, "end_line": 100},
            "log",
            "read_lines",
        ),
        (
            {"anchor": "json:data", "json_pointer": "/data/items", "kind": "array"},
            "json",
            "read_json",
        ),
        (
            {"anchor": "rows:1", "kind": "rows", "start_row": 1, "end_row": 20},
            "table",
            "read_rows",
        ),
        (
            {
                "anchor": "csv:1",
                "format": "csv",
                "kind": "rows",
                "start_row": 1,
                "end_row": 20,
            },
            "csv",
            "read_rows",
        ),
        (
            {"anchor": "result:3", "kind": "result", "start_line": 3, "end_line": 8},
            "search",
            "read_lines",
        ),
        (
            {"anchor": "media:1", "kind": "media", "start_line": 9, "end_line": 10},
            "web",
            "read_lines",
        ),
        (
            {"anchor": "file:src.py", "kind": "file", "start_line": 1, "end_line": 40},
            "code",
            "read_lines",
        ),
        (
            {"anchor": "hunk:1", "kind": "hunk", "start_line": 12, "end_line": 24},
            "diff",
            "read_lines",
        ),
        (
            {"anchor": "failure:case", "kind": "failure", "start_line": 30, "end_line": 50},
            "test",
            "read_lines",
        ),
        (
            {
                "anchor": "page:2",
                "kind": "page",
                "format": "pdf",
                "artifact_part": {"page": 2},
            },
            "pdf",
            "read_artifact_part",
        ),
        (
            {
                "anchor": "sheet:Costs",
                "kind": "sheet",
                "format": "spreadsheet",
                "artifact_part": {"sheet": "Costs", "range": "A1:D20"},
            },
            "spreadsheet",
            "read_artifact_part",
        ),
        (
            {
                "anchor": "slide:4",
                "kind": "slide",
                "format": "presentation",
                "artifact_part": {"slide": 4},
            },
            "presentation",
            "read_artifact_part",
        ),
        (
            {
                "anchor": "attachment:1",
                "kind": "attachment",
                "artifact_part": {"attachment_index": 1},
            },
            "binary",
            "read_artifact_part",
        ),
    ],
)
def test_registry_adapts_representative_output_formats(
    draft: dict, expected_format: str, expected_recovery: str
) -> None:
    manifest, private = build_anchor_manifest("call_real", "tool", [draft])

    assert len(manifest.anchors) == 1
    anchor = manifest.anchors[0]
    assert anchor.format == expected_format
    assert anchor.recovery_op == expected_recovery
    assert anchor.anchor_id.startswith("anc_")
    assert private[0]["anchor_id"] == anchor.anchor_id


def test_registry_identity_is_stable_and_collision_safe() -> None:
    drafts = [
        {"anchor": "a/b", "kind": "section", "start_line": 1, "end_line": 2},
        {"anchor": "a b", "kind": "section", "start_line": 3, "end_line": 4},
    ]
    first, _ = build_anchor_manifest("call_real", "tool", drafts)
    second, _ = build_anchor_manifest("call_real", "tool", drafts)

    assert [anchor.anchor_id for anchor in first.anchors] == [
        anchor.anchor_id for anchor in second.anchors
    ]
    assert len({anchor.anchor_id for anchor in first.anchors}) == 2
    assert len({anchor.key for anchor in first.anchors}) == 2


def test_materializable_candidate_stays_private() -> None:
    manifest, private = build_anchor_manifest(
        "call_real",
        "web_fetch",
        [
            {
                "anchor": "media:1",
                "kind": "media",
                "start_line": 1,
                "end_line": 2,
                "artifact_candidate": {
                    "source_type": "remote_url",
                    "url": "https://cdn.example/image.jpg",
                },
            }
        ],
    )

    assert manifest.anchors[0].recovery_op == "materialize_artifact"
    assert "artifact_candidate" not in manifest.anchors[0].to_dict()
    assert private[0]["artifact_candidate"]["url"] == "https://cdn.example/image.jpg"


def test_external_tool_cannot_publish_remote_lazy_candidate() -> None:
    manifest, private = build_anchor_manifest(
        "call_real",
        "external_tool",
        [
            {
                "anchor": "media:1",
                "kind": "media",
                "start_line": 1,
                "end_line": 2,
                "artifact_candidate": {
                    "source_type": "remote_url",
                    "url": "https://internal.invalid/secret",
                },
            }
        ],
    )

    assert manifest.anchors[0].recovery_op == "read_lines"
    assert "artifact_candidate" not in private[0]

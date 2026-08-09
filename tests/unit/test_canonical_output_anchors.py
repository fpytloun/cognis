from __future__ import annotations

import pytest

from cognis.core.anchored_output import markdown_heading_anchors
from cognis.core.output_anchor_registry import build_anchor_manifest


def test_heading_anchors_preserve_atx_setext_and_html_source_order() -> None:
    content = """# ATX

Setext
======

<h2>HTML heading</h2>

```markdown
# Not a heading
```
"""

    anchors = markdown_heading_anchors(content)

    assert [item["label"] for item in anchors] == ["ATX", "Setext", "HTML heading"]
    assert [item["start_line"] for item in anchors] == [1, 3, 6]


def test_heading_anchors_ignore_html_inside_fenced_code() -> None:
    anchors = markdown_heading_anchors(
        "\n".join(
            [
                "# Real heading",
                "```html",
                "<h2>Not a document heading</h2>",
                "```",
                "<h2>Real HTML heading</h2>",
            ]
        )
    )
    assert [item["label"] for item in anchors] == [
        "Real heading",
        "Real HTML heading",
    ]


def test_pdf_page_locator_is_preserved_as_canonical_anchor() -> None:
    manifest, private = build_anchor_manifest(
        "call_pdf",
        "document_generate",
        [
            {
                "anchor": "page:2",
                "kind": "page",
                "format": "pdf",
                "artifact_part": {"page": 2},
            }
        ],
    )

    assert manifest.anchors[0].locator == {"type": "artifact_part", "page": 2}
    assert manifest.anchors[0].recovery_op == "read_artifact_part"
    assert private[0]["locator"] == {"type": "artifact_part", "page": 2}


@pytest.mark.parametrize("heading", ["# title", "title\n=====", "<h1>title</h1>"])
def test_heading_anchor_has_recoverable_line_locator(heading: str) -> None:
    anchors = markdown_heading_anchors(heading)

    assert anchors
    assert anchors[0]["start_line"] <= anchors[0]["end_line"]

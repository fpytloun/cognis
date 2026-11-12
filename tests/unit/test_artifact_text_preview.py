from __future__ import annotations

import pytest
from fastapi import HTTPException

from cognis.api.routes.artifacts import (
    TEXT_PREVIEW_MAX_BYTES,
    _artifact_viewer_response,
    _assert_view_allowed,
    _text_preview_payload,
)
from cognis.artifacts.preview import (
    CSV_PREVIEW_MAX_CELL_CHARS,
    MERMAID_PREVIEW_MAX_DIAGRAMS,
    MERMAID_PREVIEW_MAX_SOURCE_BYTES,
)


def test_text_preview_accepts_json_with_generic_mime_type() -> None:
    payload = _text_preview_payload(
        filename="backup.json",
        content_type="application/octet-stream",
        content=b'{"enabled": true}',
    )

    assert payload["content"] == '{"enabled": true}'
    assert payload["truncated"] is False
    assert payload["preview_kind"] == "text"


def test_markdown_preview_is_classified_for_rendering() -> None:
    payload = _text_preview_payload(
        filename="report.markdown",
        content_type="application/octet-stream",
        content=b"# Report",
    )

    assert payload["preview_kind"] == "markdown"


def test_csv_preview_returns_bounded_table_data() -> None:
    payload = _text_preview_payload(
        filename="report.csv",
        content_type="text/csv",
        content=b'name,note\nAlice,"hello, world"\nBob,=1+1\n',
    )

    assert payload["preview_kind"] == "csv"
    assert payload["table"] == {
        "headers": ["name", "note"],
        "rows": [["Alice", "hello, world"], ["Bob", "=1+1"]],
    }


def test_csv_preview_reports_truncated_cells() -> None:
    payload = _text_preview_payload(
        filename="report.csv",
        content_type="text/csv",
        content=("name\n" + "x" * (CSV_PREVIEW_MAX_CELL_CHARS + 1)).encode(),
    )

    assert payload["truncated"] is True
    assert len(payload["table"]["rows"][0][0]) == CSV_PREVIEW_MAX_CELL_CHARS


def test_csv_preview_truncates_field_above_parser_default() -> None:
    payload = _text_preview_payload(
        filename="report.csv",
        content_type="text/csv",
        content=("name\n" + "x" * (192 * 1024)).encode(),
    )

    assert payload["truncated"] is True
    assert len(payload["table"]["rows"][0][0]) == CSV_PREVIEW_MAX_CELL_CHARS


def test_mermaid_preview_is_classified_for_strict_client_renderer() -> None:
    payload = _text_preview_payload(
        filename="flow.mmd",
        content_type="application/octet-stream",
        content=b"flowchart TD\nA-->B",
    )

    assert payload["preview_kind"] == "mermaid"


def test_oversized_mermaid_preview_falls_back_to_inert_source() -> None:
    payload = _text_preview_payload(
        filename="flow.mmd",
        content_type="text/vnd.mermaid",
        content=b"x" * (MERMAID_PREVIEW_MAX_SOURCE_BYTES + 1),
    )

    assert payload["rich_payload"]["blocks"][0]["type"] == "code"


def test_markdown_limits_rendered_mermaid_diagram_count() -> None:
    diagram = "```mermaid\nflowchart TD\nA-->B\n```\n"
    payload = _text_preview_payload(
        filename="report.md",
        content_type="text/markdown",
        content=(diagram * (MERMAID_PREVIEW_MAX_DIAGRAMS + 1)).encode(),
    )

    block_types = [block["type"] for block in payload["rich_payload"]["blocks"]]
    assert block_types.count("mermaid") == MERMAID_PREVIEW_MAX_DIAGRAMS
    assert block_types.count("code") == 1


def test_text_preview_truncates_large_content() -> None:
    payload = _text_preview_payload(
        filename="events.log",
        content_type="text/plain",
        content=b"a" * (TEXT_PREVIEW_MAX_BYTES + 1),
    )

    assert len(payload["content"]) == TEXT_PREVIEW_MAX_BYTES
    assert payload["truncated"] is True


def test_text_preview_does_not_split_utf8_character_at_limit() -> None:
    content = b"a" * (TEXT_PREVIEW_MAX_BYTES - 1) + "\N{EURO SIGN}".encode() + b"tail"

    payload = _text_preview_payload(
        filename="events.log",
        content_type="text/plain",
        content=content,
    )

    assert payload["content"] == "a" * (TEXT_PREVIEW_MAX_BYTES - 1)
    assert payload["truncated"] is True


@pytest.mark.parametrize(
    ("filename", "content_type", "content"),
    [
        ("archive.zip", "application/zip", b"binary"),
        ("fake.txt", "text/plain", b"text\x00binary"),
        ("invalid.txt", "text/plain", b"\xff"),
    ],
)
def test_text_preview_rejects_unsupported_or_binary_content(
    filename: str, content_type: str, content: bytes
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _text_preview_payload(
            filename=filename,
            content_type=content_type,
            content=content,
        )

    assert exc_info.value.status_code == 415


def test_pdf_view_returns_original_bytes_inline() -> None:
    response = _artifact_viewer_response(
        artifact_id="att_pdf",
        filename="report.pdf",
        content_type="application/pdf",
        content=b"%PDF-preview",
        standalone_url="https://cognis.test/view",
    )

    assert response.body == b"%PDF-preview"
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["referrer-policy"] == "no-referrer"


def test_html_view_wraps_an_opaque_raw_frame_without_injecting_source_code(monkeypatch) -> None:
    captured = {}

    def fake_shell(row, **kwargs):
        captured["row"] = row
        captured.update(kwargs)
        return "<html>trusted viewer</html>"

    monkeypatch.setattr("cognis.api.routes.artifacts.render_standalone_shell", fake_shell)
    url = "https://cognis.test/view?exp=123"
    content = b"<script>window.original=true</script>"
    response = _artifact_viewer_response(
        artifact_id="att_" + "1" * 32,
        filename="page.html",
        content_type="text/html",
        content=content,
        standalone_url=url,
    )
    assert captured["embedded_document_url"] == url + "&raw=1"
    assert captured["row"].content == ""
    assert captured["row"].rich_payload == {"blocks": []}
    assert "window.original" not in response.body.decode()
    raw = _artifact_viewer_response(
        artifact_id="att_" + "1" * 32,
        filename="page.html",
        content_type="text/html",
        content=content,
        standalone_url=url,
        raw=True,
    )
    assert raw.body == content
    assert "connect-src 'none'" in raw.headers["content-security-policy"]


def test_signed_view_rejects_active_svg_images() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _assert_view_allowed("payload.svg", "image/svg+xml")

    assert exc_info.value.status_code == 415


def test_markdown_view_uses_standalone_rich_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_shell(row: object, **kwargs: object) -> str:
        captured["row"] = row
        captured.update(kwargs)
        return "<html>preview</html>"

    monkeypatch.setattr("cognis.api.routes.artifacts.render_standalone_shell", fake_shell)
    response = _artifact_viewer_response(
        artifact_id="att_markdown",
        filename="report.md",
        content_type="text/markdown",
        content=b"# Report\n\n```mermaid\nflowchart TD\nA-->B\n```",
        standalone_url="https://cognis.test/view",
    )

    row = captured["row"]
    assert response.media_type == "text/html; charset=utf-8"
    assert vars(row)["rich_payload"]["blocks"] == [
        {"type": "markdown", "content": "# Report\n\n"},
        {"type": "mermaid", "source": "flowchart TD\nA-->B"},
    ]

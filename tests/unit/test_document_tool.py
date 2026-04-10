from __future__ import annotations

import base64
import types
from pathlib import Path

import pytest

from cognis.models.tool import ExecutorHandle
from cognis.tools.executor.document import handle_artifact_publish, handle_document_generate
from cognis.tools.registry import ToolExecutionContext

_DUMMY_CONTEXT = ToolExecutionContext(
    executor_handle=ExecutorHandle(executor_id="test", executor_type="in_process")
)


@pytest.mark.asyncio
async def test_document_generate_from_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_markdown(text: str, **_: object) -> str:
        return f"<h1>{text}</h1>"

    class _FakeHTML:
        def __init__(self, **_: object) -> None:
            pass

        def write_pdf(self, stylesheets: list[object]) -> bytes:
            assert stylesheets
            return b"%PDF-test"

    class _FakeCSS:
        def __init__(self, **_: object) -> None:
            pass

    monkeypatch.setitem(
        __import__("sys").modules, "markdown", types.SimpleNamespace(markdown=fake_markdown)
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "weasyprint",
        types.SimpleNamespace(HTML=_FakeHTML, CSS=_FakeCSS),
    )

    result = await handle_document_generate(
        {"content": "# Title", "input_format": "markdown", "title": "Doc"},
        _DUMMY_CONTEXT,
    )

    assert not result.is_error
    assert result.attachments is not None
    assert result.attachments[0]["mime_type"] == "application/pdf"
    assert base64.b64decode(result.attachments[0]["content_b64"]) == b"%PDF-test"


@pytest.mark.asyncio
async def test_document_generate_supports_local_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image_path = tmp_path / "diagram.png"
    image_path.write_bytes(b"png-bytes")

    def fake_markdown(text: str, **_: object) -> str:
        assert "data:image/png;base64," in text
        return text

    class _FakeHTML:
        def __init__(self, **_: object) -> None:
            pass

        def write_pdf(self, stylesheets: list[object]) -> bytes:
            return b"%PDF-asset"

    class _FakeCSS:
        def __init__(self, **_: object) -> None:
            pass

    monkeypatch.setitem(
        __import__("sys").modules, "markdown", types.SimpleNamespace(markdown=fake_markdown)
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "weasyprint",
        types.SimpleNamespace(HTML=_FakeHTML, CSS=_FakeCSS),
    )

    result = await handle_document_generate(
        {
            "content": "![Diagram](asset:diag)",
            "input_format": "markdown",
            "assets": [{"name": "diag", "path": str(image_path), "mime_type": "image/png"}],
        },
        _DUMMY_CONTEXT,
    )

    assert not result.is_error
    assert result.attachments is not None
    assert result.attachments[0]["filename"].endswith(".pdf")


@pytest.mark.asyncio
async def test_artifact_publish_reads_local_file(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-published")

    result = await handle_artifact_publish({"path": str(report)}, _DUMMY_CONTEXT)

    assert not result.is_error
    assert result.attachments is not None
    assert result.attachments[0]["filename"] == "report.pdf"
    assert result.attachments[0]["mime_type"] == "application/pdf"
    assert base64.b64decode(result.attachments[0]["content_b64"]) == b"%PDF-published"

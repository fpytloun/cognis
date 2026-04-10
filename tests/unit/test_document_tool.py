from __future__ import annotations

import ast
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
    payload = ast.literal_eval(result.output)
    assert payload["template"] == "default"
    assert payload["input_format"] == "markdown"


@pytest.mark.asyncio
async def test_document_generate_writes_output_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_markdown(text: str, **_: object) -> str:
        return f"<h1>{text}</h1>"

    class _FakeHTML:
        def __init__(self, **_: object) -> None:
            pass

        def write_pdf(self, stylesheets: list[object]) -> bytes:
            return b"%PDF-local"

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

    output_path = tmp_path / "out" / "doc.pdf"
    result = await handle_document_generate(
        {
            "content": "# Title",
            "input_format": "markdown",
            "output_path": str(output_path),
        },
        _DUMMY_CONTEXT,
    )

    assert not result.is_error
    assert output_path.read_bytes() == b"%PDF-local"
    payload = ast.literal_eval(result.output)
    assert payload["output_path"] == str(output_path)


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
async def test_document_generate_supports_injected_artifact_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_markdown(text: str, **_: object) -> str:
        assert "data:image/png;base64," in text
        return text

    class _FakeHTML:
        def __init__(self, **_: object) -> None:
            pass

        def write_pdf(self, stylesheets: list[object]) -> bytes:
            return b"%PDF-artifact"

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
            "template": "proposal",
            "assets": [
                {
                    "name": "diag",
                    "artifact_id": "art_123",
                    "filename": "diagram.png",
                    "mime_type": "image/png",
                    "content_b64": base64.b64encode(b"png-from-artifact").decode("ascii"),
                }
            ],
        },
        _DUMMY_CONTEXT,
    )

    assert not result.is_error
    payload = ast.literal_eval(result.output)
    assert payload["template"] == "proposal"
    assert payload["assets_used"] == ["diag"]


@pytest.mark.asyncio
async def test_document_generate_appends_pdf_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_markdown(text: str, **_: object) -> str:
        return text

    class _FakeHTML:
        def __init__(self, **_: object) -> None:
            pass

        def write_pdf(self, stylesheets: list[object]) -> bytes:
            return b"BASEPDF"

    class _FakeCSS:
        def __init__(self, **_: object) -> None:
            pass

    class _Page:
        pass

    class _Reader:
        def __init__(self, stream: object) -> None:
            data = stream.getvalue()
            self.pages = [data]

    class _Writer:
        def __init__(self) -> None:
            self.pages: list[bytes] = []

        def add_page(self, page: bytes) -> None:
            self.pages.append(page)

        def write(self, out: object) -> None:
            out.write(b"|".join(self.pages))

    monkeypatch.setitem(
        __import__("sys").modules, "markdown", types.SimpleNamespace(markdown=fake_markdown)
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "weasyprint",
        types.SimpleNamespace(HTML=_FakeHTML, CSS=_FakeCSS),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "pypdf",
        types.SimpleNamespace(PdfReader=_Reader, PdfWriter=_Writer),
    )

    result = await handle_document_generate(
        {
            "content": "hello",
            "input_format": "markdown",
            "append_pdf_assets": True,
            "assets": [
                {
                    "name": "appendix",
                    "filename": "appendix.pdf",
                    "mime_type": "application/pdf",
                    "content_b64": base64.b64encode(b"APPENDIX").decode("ascii"),
                }
            ],
        },
        _DUMMY_CONTEXT,
    )

    assert not result.is_error
    assert result.attachments is not None
    assert base64.b64decode(result.attachments[0]["content_b64"]) == b"BASEPDF|APPENDIX"
    payload = ast.literal_eval(result.output)
    assert payload["append_pdf_assets"] is True
    assert payload["appended_pdfs"] == ["appendix.pdf"]
    assert payload["companion_attachments"] == []


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

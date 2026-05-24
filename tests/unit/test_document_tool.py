from __future__ import annotations

import ast
import base64
import types
from pathlib import Path

import pytest

from cognis.models.tool import ExecutorHandle
from cognis.tools.executor import document
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
async def test_research_report_renders_mermaid_diagram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_render_mermaid(source: str) -> bytes:
        assert "flowchart TD" in source
        return b"<svg xmlns='http://www.w3.org/2000/svg'><text>Diagram</text></svg>"

    async def fake_render_pdf(
        *, title: str, html_body: str, css: str, base_dir: str | None
    ) -> bytes:
        captured["title"] = title
        captured["html_body"] = html_body
        captured["css"] = css
        captured["base_dir"] = str(base_dir)
        return b"%PDF-mermaid"

    monkeypatch.setattr(document, "_render_mermaid_to_svg", fake_render_mermaid)
    monkeypatch.setattr(document, "_render_pdf", fake_render_pdf)

    result = await handle_document_generate(
        {
            "content": _RESEARCH_REPORT_FIXTURE,
            "input_format": "markdown",
            "template": "research_report",
            "title": "Research",
        },
        _DUMMY_CONTEXT,
    )

    assert not result.is_error
    payload = ast.literal_eval(result.output)
    assert payload["warnings"] == []
    assert payload["template"] == "research_report"
    assert "data:image/svg+xml;base64," in captured["html_body"]
    assert "flowchart TD" not in captured["html_body"]
    assert "český text" in captured["html_body"]
    assert "—" in captured["html_body"]
    assert "“quotes”" in captured["html_body"]
    assert "<table>" in captured["html_body"]
    assert "<pre><code" in captured["html_body"]


@pytest.mark.asyncio
async def test_research_report_mermaid_fallback_warns_and_preserves_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_render_mermaid(source: str) -> bytes:
        raise document.DocumentGenerationError("renderer unavailable")

    async def fake_render_pdf(
        *, title: str, html_body: str, css: str, base_dir: str | None
    ) -> bytes:
        captured["html_body"] = html_body
        return b"%PDF-fallback"

    monkeypatch.setattr(document, "_render_mermaid_to_svg", fake_render_mermaid)
    monkeypatch.setattr(document, "_render_pdf", fake_render_pdf)

    result = await handle_document_generate(
        {
            "content": "```mermaid\nflowchart TD\n  A[Start] --> B[Done]\n```",
            "input_format": "markdown",
            "template": "research_report",
        },
        _DUMMY_CONTEXT,
    )

    assert not result.is_error
    payload = ast.literal_eval(result.output)
    assert payload["warnings"] == ["Mermaid diagram 1 could not be rendered: renderer unavailable"]
    assert 'class="mermaid-fallback"' in captured["html_body"]
    assert "flowchart TD" in captured["html_body"]
    assert "A[Start] --&gt; B[Done]" in captured["html_body"]


@pytest.mark.asyncio
async def test_default_template_does_not_preprocess_mermaid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fail_render_mermaid(source: str) -> bytes:
        raise AssertionError("default template must not render Mermaid")

    async def fake_render_pdf(
        *, title: str, html_body: str, css: str, base_dir: str | None
    ) -> bytes:
        captured["html_body"] = html_body
        return b"%PDF-default"

    monkeypatch.setattr(document, "_render_mermaid_to_svg", fail_render_mermaid)
    monkeypatch.setattr(document, "_render_pdf", fake_render_pdf)

    result = await handle_document_generate(
        {
            "content": "```mermaid\nflowchart TD\n  A --> B\n```",
            "input_format": "markdown",
            "template": "default",
        },
        _DUMMY_CONTEXT,
    )

    assert not result.is_error
    payload = ast.literal_eval(result.output)
    assert payload["warnings"] == []
    assert "data:image/svg+xml;base64," not in captured["html_body"]
    assert "flowchart TD" in captured["html_body"]


def test_research_report_css_wraps_tables_and_code_blocks() -> None:
    css = document._compose_css(
        template="research_report",
        page_size="A4",
        orientation="portrait",
    )

    assert "table-layout: fixed" in css
    assert "max-width: 100%" in css
    assert "overflow-wrap: anywhere" in css
    assert "word-break: break-word" in css
    assert "th { background: #1e3a8a; color: #ffffff" in css
    assert "tbody tr:nth-child(even)" in css
    assert "pre { white-space: pre-wrap" in css
    assert "a { color: #1d4ed8" in css
    assert ".mermaid-diagram" in css
    assert ".mermaid-fallback" in css


_RESEARCH_REPORT_FIXTURE = """# Výzkumná zpráva

UTF-8 punctuation — “quotes” a český text.

```mermaid
flowchart TD
  A[Začátek] --> B{Rozhodnutí}
  B --> C[Konec]
```

| Capability | Source URL | Notes |
| --- | --- | --- |
| Mermaid diagrams | https://example.com/really/long/source/path/that/should/wrap/in/pdf/reports?with=query&and=more | Long technical comparison text that must wrap inside the page rather than overflowing the printable width. |
| Tables | source-id-with-a-very-very-very-long-unbroken-token-1234567890abcdef | Another detailed row for zebra styling. |

```python
def example() -> str:
    return "long-code-token-that-should-wrap-in-the-generated-pdf-output"
```
"""


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

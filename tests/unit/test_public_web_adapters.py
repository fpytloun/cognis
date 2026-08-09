"""Deterministic mocked HTTP coverage for stable public-web adapters."""

from __future__ import annotations

import asyncio
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from cognis.tools.executor.web.backends.formatting import build_fetch_tool_result
from cognis.tools.executor.web.public_adapters import (
    _chunk_transcript,
    _fetch_youtube_transcript,
    _normalize_transcript_segments,
    _select_youtube_transcript,
    dispatch_public_adapter,
)


def _response(url: str, content: str, *, content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(
        200,
        content=content.encode(),
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


@pytest.mark.asyncio
async def test_github_readme_uses_anonymous_raw_rest_endpoint() -> None:
    request = AsyncMock(
        return_value=_response(
            "https://api.github.com/repos/acme/project/readme",
            "# Project\n\nActual README Markdown.",
            content_type="text/markdown",
        )
    )
    result = await dispatch_public_adapter(
        "https://github.com/acme/project", timeout=12, output_format="markdown", request=request
    )
    request.assert_awaited_once()
    assert request.await_args.args == ("https://api.github.com/repos/acme/project/readme",)
    assert request.await_args.kwargs["headers"]["Accept"] == "application/vnd.github.raw+json"
    assert result is not None
    assert result.output == "# Project\n\nActual README Markdown."
    assert result.metadata["adapter"] == "github_readme"
    assert result.metadata["provenance"]["source_url"].endswith("/readme")


@pytest.mark.asyncio
async def test_github_readme_suppresses_inline_media_when_requested() -> None:
    request = AsyncMock(
        return_value=_response(
            "https://api.github.com/repos/acme/project/readme",
            """# Project

<a href="https://ci.example"><img src="badge.svg" alt="build"></a>
<picture><source srcset="dark.png"><img src="light.png" alt="chart"></picture>
![Diagram](diagram_(dark).png)

Useful repository documentation.

`Literal ![inline](inline.png) and <img src="inline-code.png"> examples`

```markdown
![Fenced example](fenced_(dark).png)
<img src="fenced-code.png">
```
""",
            content_type="text/markdown",
        )
    )
    result = await dispatch_public_adapter(
        "https://github.com/acme/project",
        timeout=12,
        output_format="markdown",
        options={"include_media": "none"},
        request=request,
    )
    assert result is not None
    assert "Useful repository documentation." in result.output
    assert "<picture" not in result.output
    assert "badge.svg" not in result.output
    assert "light.png" not in result.output
    assert "\n![Diagram]" not in result.output
    assert "![inline](inline.png)" in result.output
    assert '<img src="inline-code.png">' in result.output
    assert "![Fenced example](fenced_(dark).png)" in result.output
    assert '<img src="fenced-code.png">' in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["html", "text"])
async def test_public_adapter_suppresses_media_in_rendered_formats(output_format: str) -> None:
    request = AsyncMock(
        return_value=_response(
            "https://api.github.com/repos/acme/project/readme",
            "# Project\n\n![Diagram](diagram_(dark).png)\n\nUseful prose.",
            content_type="text/markdown",
        )
    )
    result = await dispatch_public_adapter(
        "https://github.com/acme/project",
        timeout=12,
        output_format=output_format,
        options={"include_media": "none"},
        request=request,
    )
    assert result is not None
    assert "Useful prose." in result.output
    assert "<img" not in result.output
    assert "diagram_" not in result.output


@pytest.mark.asyncio
async def test_github_blob_readme_preserves_explicit_ref_and_path() -> None:
    request = AsyncMock(
        return_value=_response(
            "https://raw.githubusercontent.com/acme/project/release/docs/README.md",
            "# Release documentation",
            content_type="text/markdown",
        )
    )
    result = await dispatch_public_adapter(
        "https://github.com/acme/project/blob/release/docs/README.md",
        timeout=12,
        output_format="markdown",
        request=request,
    )
    assert request.await_args.args == (
        "https://raw.githubusercontent.com/acme/project/release/docs/README.md",
    )
    assert request.await_args.kwargs["headers"]["Accept"] == "text/plain"
    assert result is not None
    assert result.metadata["requested_url"].endswith("/blob/release/docs/README.md")


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["html", "text"])
async def test_public_adapter_honors_requested_output_format(output_format: str) -> None:
    request = AsyncMock(
        return_value=_response(
            "https://api.github.com/repos/acme/project/readme",
            "# Project\n\nRepository documentation.",
            content_type="text/markdown",
        )
    )
    result = await dispatch_public_adapter(
        "https://github.com/acme/project",
        timeout=12,
        output_format=output_format,
        request=request,
    )
    assert result is not None
    assert result.metadata["output_format"] == output_format
    if output_format == "html":
        assert result.output.startswith("<h1>")
    else:
        assert "<h1>" not in result.output
        assert "Project" in result.output


@pytest.mark.asyncio
async def test_stackoverflow_preserves_question_answer_hierarchy_and_fields() -> None:
    request = AsyncMock(
        side_effect=[
            _response(
                "https://api.stackexchange.com/2.3/questions/42",
                '{"items":[{"title":"How?","body":"<p>Question body</p>","score":7,"owner":{"display_name":"Q"}}]}',
            ),
            _response(
                "https://api.stackexchange.com/2.3/questions/42/answers",
                '{"items":[{"body":"<p>Answer body</p>","score":11,"is_accepted":true,"owner":{"display_name":"A"}}]}',
            ),
        ]
    )
    result = await dispatch_public_adapter(
        "https://stackoverflow.com/questions/42/how",
        timeout=12,
        output_format="markdown",
        request=request,
    )
    assert [call.args[0] for call in request.await_args_list] == [
        "https://api.stackexchange.com/2.3/questions/42",
        "https://api.stackexchange.com/2.3/questions/42/answers",
    ]
    assert all(call.kwargs["params"]["filter"] == "withbody" for call in request.await_args_list)
    assert result is not None
    assert "## Question: How?" in result.output
    assert "## Answer 1: Answer 1" in result.output
    assert "Accepted: yes" in result.output
    assert "Author: A" in result.output


@pytest.mark.asyncio
async def test_pubmed_efetch_parses_structured_xml() -> None:
    request = AsyncMock(
        return_value=_response(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            """<PubmedArticleSet><PubmedArticle><MedlineCitation><Article>
            <ArticleTitle>Study title</ArticleTitle><Abstract><AbstractText>Abstract text.</AbstractText></Abstract>
            <Journal><Title>Journal name</Title></Journal></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>""",
            content_type="application/xml",
        )
    )
    result = await dispatch_public_adapter(
        "https://pubmed.ncbi.nlm.nih.gov/12345/",
        timeout=12,
        output_format="markdown",
        request=request,
    )
    assert request.await_args.kwargs["params"] == {"db": "pubmed", "id": "12345", "retmode": "xml"}
    assert result is not None
    assert result.metadata["adapter"] == "pubmed_efetch"
    assert "# Study title" in result.output
    assert "Abstract text." in result.output


@pytest.mark.asyncio
async def test_arxiv_abs_uses_atom_api_but_pdf_is_not_intercepted() -> None:
    request = AsyncMock(
        return_value=_response(
            "https://export.arxiv.org/api/query",
            """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <title>Paper title</title><summary>Paper summary.</summary>
            <author><name>Author One</name></author></entry></feed>""",
            content_type="application/atom+xml",
        )
    )
    result = await dispatch_public_adapter(
        "https://arxiv.org/abs/2401.12345", timeout=12, output_format="markdown", request=request
    )
    assert request.await_args.kwargs["params"] == {"id_list": "2401.12345", "max_results": "1"}
    assert result is not None
    assert "Authors: Author One" in result.output
    request.reset_mock()
    assert (
        await dispatch_public_adapter(
            "https://arxiv.org/pdf/2401.12345",
            timeout=12,
            output_format="markdown",
            request=request,
        )
        is None
    )
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_youtube_oembed_is_metadata_only_with_honest_transcript_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cognis.tools.executor.web.public_adapters._fetch_youtube_transcript",
        AsyncMock(return_value={"status": "provider_blocked"}),
    )
    request = AsyncMock(
        return_value=_response(
            "https://www.youtube.com/oembed", '{"title":"Video title","author_name":"Channel"}'
        )
    )
    result = await dispatch_public_adapter(
        "https://www.youtube.com/watch?v=abc123",
        timeout=12,
        output_format="markdown",
        request=request,
    )
    assert request.await_args.kwargs["params"] == {
        "url": "https://www.youtube.com/watch?v=abc123",
        "format": "json",
    }
    assert result is not None
    assert result.metadata["adapter_status"] == "partial"
    assert result.metadata["transcript_availability"] == "provider_blocked"
    assert "Transcript: unavailable (provider_blocked)" in result.output


@pytest.mark.asyncio
async def test_youtube_transcript_is_timestamped_and_anchor_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cognis.tools.executor.web.public_adapters._fetch_youtube_transcript",
        AsyncMock(
            return_value={
                "status": "available",
                "language": "Čeština",
                "language_code": "cs",
                "is_generated": False,
                "segment_count": 3,
                "chunks": [
                    {
                        "anchor": "transcript:00:00",
                        "label": "Transcript 00:00–05:00",
                        "lines": ["[00:01] Začátek videa.", "[04:59] Konec první části."],
                    },
                    {
                        "anchor": "transcript:05:00",
                        "label": "Transcript 05:00–10:00",
                        "lines": ["[05:01] Pozdní důležitý obsah."],
                    },
                ],
            }
        ),
    )
    request = AsyncMock(
        return_value=_response(
            "https://www.youtube.com/oembed",
            '{"title":"České video","author_name":"Kanál"}',
        )
    )
    adapter_result = await dispatch_public_adapter(
        "https://www.youtube.com/watch?v=abc123",
        timeout=12,
        output_format="markdown",
        request=request,
    )
    assert adapter_result is not None
    assert adapter_result.metadata["adapter"] == "youtube_transcript"
    assert adapter_result.metadata["adapter_status"] == "complete"
    assert adapter_result.metadata["transcript_language_code"] == "cs"
    assert adapter_result.metadata["transcript_generated"] is False

    result = build_fetch_tool_result(
        url="https://www.youtube.com/watch?v=abc123",
        content=adapter_result.output,
        metadata=adapter_result.metadata,
    )
    anchors = [item["anchor"] for item in result.metadata["output_anchors"]]
    assert "transcript:00:00" in anchors
    assert "transcript:05:00" in anchors
    assert "Pozdní důležitý obsah." in result.metadata["stored_output"]


def test_transcript_normalization_deduplicates_and_chunks_every_five_minutes() -> None:
    normalized = _normalize_transcript_segments(
        [
            {"text": " repeated ", "start": 1.2, "duration": 1.0},
            {"text": "repeated", "start": 2.0, "duration": 1.0},
            {"text": "late", "start": 301.8, "duration": 1.0},
        ]
    )
    chunks = _chunk_transcript(normalized)
    assert len(normalized) == 2
    assert [chunk["anchor"] for chunk in chunks] == [
        "transcript:00:00",
        "transcript:05:00",
    ]
    assert chunks[1]["lines"] == ["[05:01] late"]
    repeated_later = _normalize_transcript_segments(
        [
            {"text": "again", "start": 1.0, "duration": 1.0},
            {"text": "again", "start": 8.0, "duration": 1.0},
        ]
    )
    assert len(repeated_later) == 2


def test_transcript_selection_prefers_manual_native_track() -> None:
    generated = SimpleNamespace(is_generated=True, language_code="cs")
    translated_manual = SimpleNamespace(is_generated=False, language_code="en")
    manual = SimpleNamespace(is_generated=False, language_code="cs")
    assert _select_youtube_transcript([translated_manual, manual, generated]) is manual
    generated_native = SimpleNamespace(is_generated=True, language_code="es")
    manual_other = SimpleNamespace(is_generated=False, language_code="en")
    assert _select_youtube_transcript([manual_other, generated_native]) is generated_native


@pytest.mark.asyncio
async def test_transcript_worker_is_terminated_at_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_queue = MagicMock()
    result_queue.get.side_effect = queue.Empty
    process = MagicMock()
    process.is_alive.side_effect = [True, False]
    context = MagicMock()
    context.Queue.return_value = result_queue
    context.Process.return_value = process
    monkeypatch.setattr(
        "cognis.tools.executor.web.public_adapters.multiprocessing.get_context",
        lambda _method: context,
    )
    result = await _fetch_youtube_transcript("abc123", timeout=0.01)
    assert result == {"status": "transcript_fetch_timeout"}
    process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_transcript_worker_is_terminated_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_queue = MagicMock()
    process = MagicMock()
    process.is_alive.side_effect = [True, False]
    context = MagicMock()
    context.Queue.return_value = result_queue
    context.Process.return_value = process
    monkeypatch.setattr(
        "cognis.tools.executor.web.public_adapters.multiprocessing.get_context",
        lambda _method: context,
    )
    monkeypatch.setattr(
        "cognis.tools.executor.web.public_adapters.asyncio.to_thread",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    with pytest.raises(asyncio.CancelledError):
        await _fetch_youtube_transcript("abc123", timeout=5)
    process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_delayed_oembed_still_returns_honest_transcript_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def delayed_request(*_args: object, **_kwargs: object) -> httpx.Response:
        await asyncio.sleep(0.02)
        return _response(
            "https://www.youtube.com/oembed",
            '{"title":"Video title","author_name":"Channel"}',
        )

    monkeypatch.setattr(
        "cognis.tools.executor.web.public_adapters._PUBLIC_ADAPTER_TIMEOUT_SECONDS",
        0.03,
    )
    monkeypatch.setattr(
        "cognis.tools.executor.web.public_adapters._fetch_youtube_transcript",
        AsyncMock(return_value={"status": "transcript_fetch_timeout"}),
    )
    result = await dispatch_public_adapter(
        "https://www.youtube.com/watch?v=abc123",
        timeout=12,
        output_format="markdown",
        request=delayed_request,
    )
    assert result is not None
    assert result.metadata["adapter_status"] == "partial"
    assert result.metadata["transcript_availability"] == "transcript_fetch_timeout"


@pytest.mark.asyncio
async def test_adapter_transport_failure_returns_none_for_generic_fetch_fallback() -> None:
    request = AsyncMock(side_effect=httpx.ConnectError("offline"))
    result = await dispatch_public_adapter(
        "https://github.com/acme/project", timeout=12, output_format="markdown", request=request
    )
    assert result is None
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_public_adapter_fails_fast_to_generic_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _slow_request(*_args: object, **_kwargs: object) -> httpx.Response:
        await asyncio.sleep(1)
        raise AssertionError("adapter timeout did not cancel request")

    monkeypatch.setattr(
        "cognis.tools.executor.web.public_adapters._PUBLIC_ADAPTER_TIMEOUT_SECONDS",
        0.01,
    )
    result = await dispatch_public_adapter(
        "https://arxiv.org/abs/2606.00103",
        timeout=120,
        output_format="markdown",
        request=_slow_request,
    )
    assert result is None

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cognis.core.anchored_output import markdown_heading_anchors
from cognis.core.output_anchor_registry import build_anchor_manifest
from cognis.core.tool_output_store import FilesystemToolOutputBackend, ToolOutputStore
from cognis.tools.builtin.tool_output import handle_tool_output_tool
from cognis.tools.executor.web.backends.formatting import (
    build_fetch_tool_result,
    build_search_tool_result,
)
from cognis.tools.executor.web.headers import format_response_result

_CORPUS_PATH = Path(__file__).parents[1] / "fixtures" / "web_quality" / "corpus.json"


def _response(url: str, body: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=body,
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


async def _persist_result(
    store: ToolOutputStore,
    *,
    call_id: str,
    tool_name: str,
    output: str,
    metadata: dict[str, object],
) -> None:
    stored_output = str(metadata["stored_output"])
    drafts = metadata.get("output_anchors")
    raw_drafts = list(drafts) if isinstance(drafts, list) else []
    raw_drafts.extend(markdown_heading_anchors(stored_output, existing_anchors=raw_drafts))
    manifest, private = build_anchor_manifest(call_id, tool_name, raw_drafts)
    payload = manifest.to_dict()
    payload["anchors"] = private
    await store.save(
        call_id,
        stored_output,
        anchors=private,
        anchor_manifest=payload,
    )
    assert output


@pytest.mark.asyncio
async def test_sanitized_fixture_corpus_runs_through_recovery_pipeline(tmp_path: Path) -> None:
    corpus = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    store = ToolOutputStore(FilesystemToolOutputBackend(tmp_path))

    for case in corpus:
        response = _response(case["url"], case["html"].encode(), "text/html; charset=utf-8")
        extracted = format_response_result(
            response,
            "markdown",
            requested_url=case["url"],
            source_url=case["url"],
        )
        document = (extracted.metadata or {}).get("extracted_document")
        assert isinstance(document, dict)
        quality = document.get("semantic_quality")
        assert isinstance(quality, dict)
        assert quality["status"] == case["expected_status"], case["name"]

        if extracted.is_error:
            assert case["expected_status"] in {"blocked", "interstitial"}
            continue

        result = build_fetch_tool_result(
            url=case["url"],
            content=extracted.output,
            metadata=extracted.metadata,
        )
        call_id = f"call_{case['name']}"
        assert result.metadata is not None
        await _persist_result(
            store,
            call_id=call_id,
            tool_name="web_fetch",
            output=result.output,
            metadata=result.metadata,
        )

        anchors = await handle_tool_output_tool(
            "list_tool_output_anchors", {"call_id": call_id}, store
        )
        assert not anchors.is_error
        assert "page:1" in anchors.output

        search = await handle_tool_output_tool(
            "search_tool_output",
            {"call_id": call_id, "pattern": case["late_marker"], "context_lines": 1},
            store,
        )
        assert not search.is_error
        assert case["late_marker"] in search.output

        page = await handle_tool_output_tool(
            "read_tool_output",
            {"call_id": call_id, "offset": 1, "limit": 5},
            store,
        )
        assert not page.is_error
        assert "Showing lines" in page.output or "Total:" in page.output


@pytest.mark.asyncio
async def test_arxiv_pdf_compact_preview_recovers_late_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_texts = [
        f"Page {index} content "
        + ("evidence " * 120)
        + (f"PDF_LATE_PAGE_{index}" if index == 20 else "")
        for index in range(1, 21)
    ]
    monkeypatch.setattr(
        "cognis.tools.executor.web.headers._extract_pdf_text",
        lambda _content: (page_texts, {"title": "Fixture paper", "author": "Researcher"}),
    )
    url = "https://arxiv.org/pdf/1706.03762"
    extracted = format_response_result(
        _response(url, b"%PDF-1.7 fixture", "application/pdf"),
        "markdown",
        requested_url=url,
        source_url=url,
    )
    result = build_fetch_tool_result(
        url=url,
        content=extracted.output,
        metadata=extracted.metadata,
    )
    assert result.metadata is not None
    assert result.metadata["producer_truncated"] is True
    assert "PDF_LATE_PAGE_20" not in result.output
    assert "PDF_LATE_PAGE_20" in str(result.metadata["stored_output"])

    store = ToolOutputStore(FilesystemToolOutputBackend(tmp_path))
    await _persist_result(
        store,
        call_id="call_arxiv_pdf",
        tool_name="web_fetch",
        output=result.output,
        metadata=result.metadata,
    )
    anchors = await store.list_anchors("call_arxiv_pdf")
    assert anchors is not None
    page_names = [anchor.anchor for anchor in anchors if anchor.anchor.startswith("page:")]
    assert page_names == [f"page:{index}" for index in range(1, 21)]
    late = next(anchor for anchor in anchors if anchor.anchor == "page:20")
    assert late.format == "pdf"
    assert late.locator is not None
    assert late.locator["page"] == 20

    recovered = await handle_tool_output_tool(
        "read_tool_output_anchor",
        {"call_id": "call_arxiv_pdf", "anchor": "page:20"},
        store,
    )
    assert not recovered.is_error
    assert "PDF_LATE_PAGE_20" in recovered.output


@pytest.mark.asyncio
async def test_youtube_transcript_compact_preview_recovers_late_timestamp(
    tmp_path: Path,
) -> None:
    chunks = []
    for index in range(70):
        start = index * 300
        total_minutes = start // 60
        hours, minutes = divmod(total_minutes, 60)
        timestamp = f"{hours:02d}:{minutes:02d}:00" if hours else f"{minutes:02d}:00"
        end_total_minutes = total_minutes + 5
        end_hours, end_minutes = divmod(end_total_minutes, 60)
        end_timestamp = (
            f"{end_hours:02d}:{end_minutes:02d}:00" if end_hours else f"{end_minutes:02d}:00"
        )
        marker = "YOUTUBE_LATE_TRANSCRIPT_MARKER" if index == 69 else ""
        chunks.append(
            {
                "anchor": f"transcript:{timestamp}",
                "label": f"Transcript {timestamp}–{end_timestamp}",
                "lines": [
                    f"[{timestamp}] transcript evidence {index} segment {second} "
                    + ("details " * 20)
                    + marker
                    for second in range(60)
                ],
            }
        )
    result = build_fetch_tool_result(
        url="https://www.youtube.com/watch?v=fixture",
        content="# Fixture video\nAuthor: Channel\n\n## Transcript",
        metadata={
            "adapter": "youtube_transcript",
            "transcript_chunks": chunks,
            "extracted_document": {
                "canonical_url": "https://www.youtube.com/watch?v=fixture",
                "extractor": "adapter_youtube_transcript",
            },
        },
    )
    assert result.metadata is not None
    assert result.metadata["producer_truncated"] is True
    assert result.metadata["stored_output_size"] > result.metadata["compact_output_size"]
    assert result.metadata["compact_output_size"] == len(result.output)
    assert result.metadata["stored_output_size"] == len(result.metadata["stored_output"])
    assert "YOUTUBE_LATE_TRANSCRIPT_MARKER" not in result.output
    assert "YOUTUBE_LATE_TRANSCRIPT_MARKER" in str(result.metadata["stored_output"])

    store = ToolOutputStore(FilesystemToolOutputBackend(tmp_path))
    await _persist_result(
        store,
        call_id="call_youtube_transcript",
        tool_name="web_fetch",
        output=result.output,
        metadata=result.metadata,
    )
    anchors = await store.list_anchors("call_youtube_transcript")
    transcript_names = [
        anchor.anchor for anchor in anchors if anchor.anchor.startswith("transcript:")
    ]
    assert len(transcript_names) == 70
    assert transcript_names[-1] == "transcript:05:45:00"
    recovered = await handle_tool_output_tool(
        "read_tool_output_anchor",
        {"call_id": "call_youtube_transcript", "anchor": "transcript:05:45:00"},
        store,
    )
    assert not recovered.is_error
    assert "YOUTUBE_LATE_TRANSCRIPT_MARKER" in recovered.output


@pytest.mark.asyncio
async def test_product_items_get_stable_recoverable_item_anchors(tmp_path: Path) -> None:
    html = """
    <html><head><title>Comparison</title>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"ItemList","itemListElement":[
      {"@type":"Product","name":"Product One","url":"/one",
       "offers":{"@type":"Offer","price":"100","priceCurrency":"EUR"}},
      {"@type":"Product","name":"Product Two","url":"/two",
       "offers":{"@type":"Offer","price":"120","priceCurrency":"EUR"}}
    ]}</script></head><body></body></html>
    """
    url = "https://shop.example/products"
    extracted = format_response_result(
        _response(url, html.encode(), "text/html; charset=utf-8"),
        "markdown",
        requested_url=url,
        source_url=url,
    )
    result = build_fetch_tool_result(
        url=url,
        content=extracted.output,
        metadata=extracted.metadata,
    )
    store = ToolOutputStore(FilesystemToolOutputBackend(tmp_path))
    await _persist_result(
        store,
        call_id="call_products",
        tool_name="web_fetch",
        output=result.output,
        metadata=result.metadata or {},
    )
    anchors = await store.list_anchors("call_products")
    assert [anchor.anchor for anchor in anchors if anchor.anchor.startswith("item:")] == [
        "item:1",
        "item:2",
    ]
    item = await handle_tool_output_tool(
        "read_tool_output_anchor",
        {"call_id": "call_products", "anchor": "item:2"},
        store,
    )
    assert not item.is_error
    assert "Product Two" in item.output
    assert "Product One" not in item.output
    assert "Price: 120" in item.output
    assert "URL: https://shop.example/two" in item.output


@pytest.mark.asyncio
async def test_search_result_anchors_keep_natural_source_order(tmp_path: Path) -> None:
    result = build_search_tool_result(
        answer=None,
        results=[
            {
                "title": f"Result {index}",
                "url": f"https://example.test/{index}",
                "snippet": f"Snippet {index}",
            }
            for index in range(1, 13)
        ],
    )
    assert result.metadata is not None
    store = ToolOutputStore(FilesystemToolOutputBackend(tmp_path))
    await _persist_result(
        store,
        call_id="call_search_order",
        tool_name="web_search",
        output=result.output,
        metadata=result.metadata,
    )
    anchors = await store.list_anchors("call_search_order")
    assert anchors is not None
    assert [anchor.anchor for anchor in anchors] == [f"result:{index}" for index in range(1, 13)]
    result_12 = await handle_tool_output_tool(
        "read_tool_output_anchor",
        {"call_id": "call_search_order", "anchor": "result:12"},
        store,
    )
    assert not result_12.is_error
    assert "[12] Result 12" in result_12.output
    assert "[11] Result 11" not in result_12.output


@pytest.mark.asyncio
async def test_large_article_beyond_legacy_cap_keeps_late_section_recoverable(
    tmp_path: Path,
) -> None:
    paragraphs = "".join(
        f"<p>Substantive article paragraph {index}. " + ("evidence " * 18) + "</p>"
        for index in range(4_000)
    )
    late_marker = "LEGACY_CAP_LATE_REFERENCE_MARKER"
    html = (
        "<html><head><title>Large reference article</title></head><body><article>"
        "<h1>Large reference article</h1>"
        f"{paragraphs}<h2>References</h2><p>{late_marker}</p>"
        "</article></body></html>"
    )
    assert len(html) > 500_000
    url = "https://example.test/large-reference"
    extracted = format_response_result(
        _response(url, html.encode(), "text/html; charset=utf-8"),
        "markdown",
        requested_url=url,
        source_url=url,
    )
    assert not extracted.is_error
    assert (extracted.metadata or {}).get("source_truncated") is False
    result = build_fetch_tool_result(
        url=url,
        content=extracted.output,
        metadata=extracted.metadata,
    )
    assert result.metadata is not None
    assert late_marker in str(result.metadata["stored_output"])

    store = ToolOutputStore(FilesystemToolOutputBackend(tmp_path))
    await _persist_result(
        store,
        call_id="call_large_reference",
        tool_name="web_fetch",
        output=result.output,
        metadata=result.metadata,
    )
    recovered = await handle_tool_output_tool(
        "search_tool_output",
        {
            "call_id": "call_large_reference",
            "pattern": late_marker,
            "context_lines": 1,
        },
        store,
    )
    assert not recovered.is_error
    assert late_marker in recovered.output

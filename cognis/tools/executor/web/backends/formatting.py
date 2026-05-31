"""Helpers for compact structured web tool outputs."""

from __future__ import annotations

from urllib.parse import urlparse

from cognis.core.anchored_output import AnchoredTextBuilder, compact_snippet
from cognis.models.tool import ToolResult

_FETCH_COMPACT_CHARS = 12_000
_CRAWL_COMPACT_CHARS = 4_000
_CRAWL_STORED_CHARS = 12_000


def url_domain(url: str) -> str:
    """Return a normalized host name for display."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def build_search_tool_result(
    *,
    answer: str | None,
    results: list[dict[str, object]],
) -> ToolResult:
    """Build compact anchored text for search-style results."""
    compact_builder = AnchoredTextBuilder()
    stored_builder = AnchoredTextBuilder()
    if answer:
        compact_answer = compact_snippet(answer, max_chars=500)
        full_answer = compact_snippet(answer, max_chars=max(len(answer), 500))
        compact_builder.add_section(
            "answer",
            kind="answer",
            label="Answer",
            lines=[f"Answer: {compact_answer}"],
        )
        stored_builder.add_section(
            "answer",
            kind="answer",
            label="Answer",
            lines=[f"Answer: {full_answer}"],
        )

    for index, result in enumerate(results, start=1):
        title = str(result.get("title") or "")
        url = str(result.get("url") or "")
        snippet = str(result.get("snippet") or "")
        score = result.get("score")
        compact_lines = [f"[{index}] {title}", f"    URL: {url}"]
        stored_lines = [f"[{index}] {title}", f"    URL: {url}"]
        domain = url_domain(url)
        if domain:
            compact_lines.append(f"    Domain: {domain}")
            stored_lines.append(f"    Domain: {domain}")
        if isinstance(score, int | float):
            compact_lines.append(f"    Relevance: {score:.2f}")
            stored_lines.append(f"    Relevance: {score:.2f}")
        if snippet:
            compact_lines.append(f"    Snippet: {compact_snippet(snippet)}")
            stored_lines.append(
                f"    Snippet: {compact_snippet(snippet, max_chars=max(len(snippet), 600))}"
            )
        compact_builder.add_section(
            f"result:{index}",
            kind="search_result",
            label=title or url or f"Result {index}",
            lines=compact_lines,
        )
        stored_builder.add_section(
            f"result:{index}",
            kind="search_result",
            label=title or url or f"Result {index}",
            lines=stored_lines,
        )

    output, anchors = compact_builder.build()
    stored_output, _ = stored_builder.build()
    if not output or not stored_output:
        return ToolResult(output="No search results found.")
    return ToolResult(
        output=output,
        metadata={
            "output_anchors": anchors,
            "stored_output": stored_output,
        },
    )


def build_fetch_tool_result(
    *,
    url: str,
    content: str,
    metadata: dict[str, object] | None = None,
) -> ToolResult:
    """Build compact + stored anchored output for a single fetched page."""

    compact_builder = AnchoredTextBuilder()
    stored_builder = AnchoredTextBuilder()
    document = metadata.get("extracted_document") if isinstance(metadata, dict) else None
    document_data = document if isinstance(document, dict) else {}
    display_url = str(document_data.get("canonical_url") or document_data.get("url") or url)

    metadata_lines = _document_metadata_lines(document_data)
    if metadata_lines:
        compact_builder.add_section(
            "metadata",
            kind="metadata",
            label=str(document_data.get("title") or display_url),
            lines=metadata_lines,
        )
        stored_builder.add_section(
            "metadata",
            kind="metadata",
            label=str(document_data.get("title") or display_url),
            lines=metadata_lines,
        )

    page_lines = [f"URL: {display_url}"]
    if display_url != url:
        page_lines.append(f"Requested URL: {url}")
    domain = url_domain(display_url)
    if domain:
        page_lines.append(f"Domain: {domain}")
    extractor = document_data.get("extractor")
    if isinstance(extractor, str) and extractor:
        score = document_data.get("extraction_score")
        score_suffix = f" (score {score:.1f})" if isinstance(score, int | float) else ""
        page_lines.append(f"Extractor: {extractor}{score_suffix}")
    page_lines.append("")

    compact_builder.add_section(
        "page:1",
        kind="page",
        label=url,
        lines=page_lines + [_truncate_block(content, max_chars=_FETCH_COMPACT_CHARS)],
    )
    stored_builder.add_section(
        "page:1",
        kind="page",
        label=url,
        lines=page_lines + [content],
    )

    for index, image in enumerate(_document_images(document_data), start=1):
        lines = _image_lines(image)
        anchor = f"media:{index}"
        lines.append(f"Lazy artifact: tool_artifact:<tool_call_id>:{anchor}")
        lines.append(
            "To inspect this image, call artifact_read with "
            f'artifact_id="tool_artifact:<tool_call_id>:{anchor}".'
        )
        label = str(
            image.get("caption") or image.get("alt") or image.get("url") or f"Media {index}"
        )
        artifact_candidate = _image_artifact_candidate(image, source_url=display_url)
        compact_builder.add_section(
            anchor,
            kind="media",
            label=label,
            lines=lines,
            artifact_candidate=artifact_candidate,
        )
        stored_builder.add_section(
            anchor,
            kind="media",
            label=label,
            lines=lines,
            artifact_candidate=artifact_candidate,
        )

    output, _compact_anchors = compact_builder.build()
    stored_output, stored_anchors = stored_builder.build()
    merged_metadata = dict(metadata or {})
    merged_metadata.update(
        {
            "output_anchors": stored_anchors,
            "stored_output": stored_output or output,
            "source_url": display_url,
            "requested_url": url if display_url != url else None,
            "original_size": len(content),
        }
    )
    return ToolResult(output=output, metadata=merged_metadata)


def build_crawl_tool_result(
    *,
    root_url: str,
    pages: list[dict[str, object]],
    metadata: dict[str, object] | None = None,
) -> ToolResult:
    """Build compact + stored anchored output for crawl results."""

    if not pages:
        return ToolResult(output=f"No pages crawled from {root_url}.", metadata=metadata)

    compact_builder = AnchoredTextBuilder()
    stored_builder = AnchoredTextBuilder()
    compact_builder.add_line(f"# Crawl results for {root_url}")
    compact_builder.add_line("")
    compact_builder.add_line(f"Pages: {len(pages)}")
    compact_builder.add_line("")
    stored_builder.add_line(f"# Crawl results for {root_url}")
    stored_builder.add_line("")
    stored_builder.add_line(f"Pages: {len(pages)}")
    stored_builder.add_line("")

    for index, page in enumerate(pages, start=1):
        url = str(page.get("url") or "")
        depth = page.get("depth")
        is_error = bool(page.get("is_error"))
        title = str(page.get("title") or url or f"Page {index}")
        content = str(page.get("content") or "")
        page_lines = [f"URL: {url}"] if url else []
        if depth is not None:
            page_lines.append(f"Depth: {depth}")
        page_lines.append(f"Status: {'error' if is_error else 'ok'}")
        page_lines.append("")
        compact_builder.add_section(
            f"page:{index}",
            kind="page",
            label=title,
            lines=page_lines + [_truncate_block(content, max_chars=_CRAWL_COMPACT_CHARS)],
        )
        stored_builder.add_section(
            f"page:{index}",
            kind="page",
            label=title,
            lines=page_lines + [_truncate_block(content, max_chars=_CRAWL_STORED_CHARS)],
        )

    output, _compact_anchors = compact_builder.build()
    stored_output, stored_anchors = stored_builder.build()
    merged_metadata = dict(metadata or {})
    merged_metadata.update(
        {
            "crawl_pages": len(pages),
            "output_anchors": stored_anchors,
            "stored_output": stored_output or output,
        }
    )
    return ToolResult(output=output, metadata=merged_metadata)


def _truncate_block(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n[truncated]"
    return text[: max_chars - len(suffix)].rstrip() + suffix


def _document_metadata_lines(document: dict[str, object]) -> list[str]:
    fields = (
        ("Title", "title"),
        ("Description", "description"),
        ("Site", "site_name"),
        ("Author", "author"),
        ("Published", "published_at"),
        ("Modified", "modified_at"),
        ("Canonical URL", "canonical_url"),
        ("Language", "language"),
    )
    lines: list[str] = []
    extraction_status = document.get("extraction_status")
    if isinstance(extraction_status, str) and extraction_status.strip():
        lines.append(
            f"Extraction status: {_compact_field(extraction_status.strip(), max_chars=200)}"
        )
    for label, key in fields:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}: {_compact_field(value.strip(), max_chars=800)}")
    images = _document_images(document)
    hero = next((image for image in images if image.get("role") == "hero"), None)
    if hero and isinstance(hero.get("url"), str):
        lines.append(f"Hero image: {_compact_field(str(hero['url']), max_chars=1000)}")
    return lines


def _document_images(document: dict[str, object]) -> list[dict[str, object]]:
    raw = document.get("images")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and isinstance(item.get("url"), str)]


def _image_lines(image: dict[str, object]) -> list[str]:
    lines = [f"URL: {_compact_field(str(image.get('url') or ''), max_chars=1000)}"]
    for label, key in (
        ("Role", "role"),
        ("Source", "source"),
        ("Alt", "alt"),
        ("Caption", "caption"),
        ("Width", "width"),
        ("Height", "height"),
    ):
        value = image.get(key)
        if value is not None and str(value).strip():
            lines.append(f"{label}: {_compact_field(str(value), max_chars=600)}")
    return lines


def _image_artifact_candidate(
    image: dict[str, object], *, source_url: str
) -> dict[str, object] | None:
    url = image.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    metadata: dict[str, object] = {
        "source_tool": "web_fetch",
        "source_page_url": source_url,
    }
    for key in ("role", "source", "alt", "caption", "width", "height"):
        value = image.get(key)
        if value is not None and str(value).strip():
            metadata[key] = value
    return {
        "source_type": "remote_url",
        "url": url.strip(),
        "mime_hint": _mime_hint_from_url(url.strip()),
        "filename_hint": _filename_hint_from_url(url.strip()),
        "metadata": metadata,
    }


def _mime_hint_from_url(url: str) -> str | None:
    path = urlparse(url).path.lower()
    if path.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".gif"):
        return "image/gif"
    if path.endswith(".avif"):
        return "image/avif"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return None


def _filename_hint_from_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name or None


def _compact_field(value: str, *, max_chars: int) -> str:
    return compact_snippet(value, max_chars=max_chars)

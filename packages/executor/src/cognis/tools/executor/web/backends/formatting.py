"""Helpers for compact structured web tool outputs."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from cognis.core.anchored_output import AnchoredTextBuilder, compact_snippet
from cognis.models.tool import ToolResult
from cognis.tools.executor.web.semantic_quality import url_provenance

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
    images: list[dict[str, object]] | None = None,
    metadata: dict[str, object] | None = None,
) -> ToolResult:
    """Build compact anchored text for search-style results."""
    metadata = dict(metadata or {})
    results = _apply_freshness_contract(results, metadata)
    metadata["normalized_results"] = results
    metadata["returned_result_count"] = len(results)
    compact_builder = AnchoredTextBuilder()
    stored_builder = AnchoredTextBuilder()
    if metadata.get("search_degraded"):
        reason = metadata.get("degraded_reason")
        failures = metadata.get("engine_failures")
        failure_count = len(failures) if isinstance(failures, list) else 0
        if isinstance(reason, str) and reason.strip():
            warning = f"Search degraded: {reason.strip()}"
            if failure_count:
                warning += f" {failure_count} engine failure(s) also reduced coverage."
        else:
            warning = (
                f"Search degraded: {failure_count} engine failure(s). "
                "Treat freshness and coverage as incomplete."
            )
        failure_details = _engine_failure_details(failures)
        if failure_details:
            warning += f" Failed engines: {failure_details}."
        requested_mode = metadata.get("requested_search_mode")
        effective_mode = metadata.get("effective_search_mode")
        if (
            isinstance(requested_mode, str)
            and isinstance(effective_mode, str)
            and requested_mode != effective_mode
        ):
            warning += f" Search mode changed from {requested_mode} to {effective_mode}."
        compact_builder.add_section(
            "search:status",
            kind="status",
            label="Search status",
            lines=[warning],
        )
        stored_builder.add_section(
            "search:status",
            kind="status",
            label="Search status",
            lines=[warning],
        )
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
        score = result.get("cognis_score", result.get("score"))
        compact_lines = [f"[{index}] {title}", f"    URL: {url}"]
        stored_lines = [f"[{index}] {title}", f"    URL: {url}"]
        domain = url_domain(url)
        if domain:
            compact_lines.append(f"    Domain: {domain}")
            stored_lines.append(f"    Domain: {domain}")
        if isinstance(score, int | float):
            compact_lines.append(f"    Relevance: {score:.2f}")
            stored_lines.append(f"    Relevance: {score:.2f}")
        provider_score = result.get("provider_score")
        if isinstance(provider_score, int | float):
            stored_lines.append(f"    Provider score: {provider_score:.4g}")
        result_type = result.get("result_type")
        if result_type:
            compact_lines.append(f"    Content type: {result_type}")
            stored_lines.append(f"    Content type: {result_type}")
        published_date = result.get("published_date")
        if published_date:
            compact_lines.append(f"    Published: {published_date}")
            stored_lines.append(f"    Published: {published_date}")
        freshness = result.get("freshness")
        if freshness:
            compact_lines.append(f"    Freshness: {freshness}")
            stored_lines.append(f"    Freshness: {freshness}")
        engine = result.get("engine")
        engines = result.get("engines")
        if engine or engines:
            provider = str(engine) if engine else ""
            if not provider and isinstance(engines, list):
                provider = ", ".join(str(item) for item in engines)
            compact_lines.append(f"    Source engine: {provider}")
            stored_lines.append(f"    Search engine: {provider}")
        recommendation = result.get("fetch_recommendation")
        recommendation_reason = result.get("recommendation_reason")
        if recommendation:
            compact_lines.append(f"    Fetch recommendation: {recommendation}")
            stored_lines.append(f"    Fetch recommendation: {recommendation}")
        if recommendation_reason:
            compact_lines.append(f"    Why: {recommendation_reason}")
            stored_lines.append(f"    Recommendation reason: {recommendation_reason}")
        source_metadata = result.get("source_metadata")
        if isinstance(source_metadata, dict):
            for key in ("author", "maintainer", "license", "popularity", "duration"):
                value = source_metadata.get(key)
                if value not in (None, "", [], {}):
                    label = key.replace("_", " ").title()
                    compact_lines.append(f"    {label}: {value}")
                    stored_lines.append(f"    {label}: {value}")
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

    for index, image in enumerate(images or [], start=1):
        lines = _image_lines(image)
        anchor = f"media:{index}"
        lines.append(f"Lazy artifact: tool_artifact:<tool_call_id>:{anchor}")
        lines.append(
            "To inspect this image, call artifact_read with "
            f'artifact_id="tool_artifact:<tool_call_id>:{anchor}".'
        )
        label = str(
            image.get("caption") or image.get("alt") or image.get("url") or f"Image {index}"
        )
        artifact_candidate = _image_artifact_candidate(image, source_url="")
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
    if not output or not stored_output:
        return ToolResult(output="No search results found.", metadata=metadata)
    return ToolResult(
        output=output,
        metadata={
            "output_anchors": stored_anchors,
            "stored_output": stored_output,
            **metadata,
        },
    )


def _apply_freshness_contract(
    results: list[dict[str, object]],
    metadata: dict[str, object],
) -> list[dict[str, object]]:
    requested = metadata.get("requested_time_range")
    if not isinstance(requested, str) or requested not in {"day", "week", "month", "year"}:
        return results
    windows = {
        "day": timedelta(days=1),
        "week": timedelta(days=7),
        "month": timedelta(days=30),
        "year": timedelta(days=365),
    }
    cutoff = datetime.now(UTC) - windows[requested]
    retained: list[dict[str, object]] = []
    dated_count = 0
    in_window_count = 0
    unknown_count = 0
    for result in results:
        published = _parse_search_date(result.get("published_date"))
        if published is None:
            unknown_count += 1
            retained.append(result)
            continue
        dated_count += 1
        if published >= cutoff:
            in_window_count += 1
            retained.append(result)
    verified = in_window_count > 0
    metadata.update(
        {
            "freshness_requested": requested,
            "freshness_verified": verified,
            "dated_result_count": dated_count,
            "in_window_result_count": in_window_count,
            "unknown_date_result_count": unknown_count,
        }
    )
    if results and not verified:
        metadata["search_degraded"] = True
        reason = (
            f"Freshness '{requested}' could not be verified because no returned result "
            "had an in-window publication date."
        )
        existing = metadata.get("degraded_reason")
        metadata["degraded_reason"] = (
            f"{existing} {reason}" if isinstance(existing, str) and existing else reason
        )
    return retained


def _parse_search_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    lowered = text.lower()
    if lowered == "yesterday":
        return datetime.now(UTC) - timedelta(days=1)
    match = re.fullmatch(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", lowered)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {
        "minute": timedelta(minutes=1),
        "hour": timedelta(hours=1),
        "day": timedelta(days=1),
        "week": timedelta(weeks=1),
        "month": timedelta(days=30),
        "year": timedelta(days=365),
    }
    return datetime.now(UTC) - amount * multipliers[unit]


def _engine_failure_details(value: object, *, limit: int = 3) -> str:
    if not isinstance(value, list):
        return ""
    details: list[str] = []
    for failure in value[:limit]:
        if isinstance(failure, list | tuple) and failure:
            name = compact_snippet(re.sub(r"\s+", " ", str(failure[0])), max_chars=80)
            reason = compact_snippet(
                re.sub(r"\s+", " ", str(failure[1]) if len(failure) > 1 else "failed"),
                max_chars=160,
            )
            details.append(f"{name} — {reason}")
        elif isinstance(failure, dict):
            name = compact_snippet(
                re.sub(
                    r"\s+",
                    " ",
                    str(failure.get("engine") or failure.get("name") or "unknown"),
                ),
                max_chars=80,
            )
            reason = compact_snippet(
                re.sub(
                    r"\s+",
                    " ",
                    str(failure.get("reason") or failure.get("error") or "failed"),
                ),
                max_chars=160,
            )
            details.append(f"{name} — {reason}")
        elif isinstance(failure, str):
            details.append(compact_snippet(re.sub(r"\s+", " ", failure), max_chars=240))
    remaining = len(value) - len(details)
    if remaining > 0:
        details.append(f"+{remaining} more")
    return "; ".join(details)


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
    requested_url = str((metadata or {}).get("requested_url") or url)
    display_url = str(document_data.get("canonical_url") or document_data.get("url") or url)
    if metadata and metadata.get("binary_kind") == "pdf":
        stored_output = content.strip()
        output = _truncate_block(stored_output, max_chars=_FETCH_COMPACT_CHARS)
        merged_metadata = dict(metadata)
        merged_metadata.update(
            {
                "output_anchors": _pdf_page_anchors(stored_output),
                "stored_output": stored_output,
                "source_url": str(metadata.get("source_url") or url),
                "requested_url": str(metadata.get("requested_url") or url),
                "producer_truncated": output != stored_output,
                "producer_output_size": len(stored_output),
                "producer_truncation_reason": (
                    "compact_preview" if output != stored_output else None
                ),
            }
        )
        return ToolResult(output=output, metadata=merged_metadata)

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
    if display_url != requested_url:
        page_lines.append(f"Requested URL: {requested_url}")
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

    transcript_chunks = (metadata or {}).get("transcript_chunks")
    if isinstance(transcript_chunks, list):
        compact_chars = len(content)
        for chunk in transcript_chunks:
            if not isinstance(chunk, dict):
                continue
            anchor = str(chunk.get("anchor") or "")
            label = str(chunk.get("label") or anchor)
            raw_lines = chunk.get("lines")
            lines = [str(line) for line in raw_lines] if isinstance(raw_lines, list) else []
            if not anchor.startswith("transcript:") or not lines:
                continue
            stored_builder.add_section(
                anchor,
                kind="transcript",
                label=label,
                lines=lines,
            )
            chunk_chars = sum(len(line) + 1 for line in lines)
            if compact_chars + chunk_chars <= _FETCH_COMPACT_CHARS:
                compact_builder.add_section(
                    anchor,
                    kind="transcript",
                    label=label,
                    lines=lines,
                )
                compact_chars += chunk_chars

    commerce_items = document_data.get("commerce_items")
    if isinstance(commerce_items, list):
        for index, item in enumerate(commerce_items, start=1):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            lines = [f"# {item['name']}"]
            for label, key in (
                ("Type", "type"),
                ("Brand", "brand"),
                ("Price", "price"),
                ("Currency", "currency"),
                ("Availability", "availability"),
                ("Seller", "seller"),
                ("Rating", "rating"),
                ("Review count", "review_count"),
                ("SKU", "sku"),
                ("URL", "url"),
            ):
                value = item.get(key)
                if value:
                    lines.append(f"{label}: {value}")
            properties = item.get("properties")
            if isinstance(properties, dict):
                lines.extend(f"{name}: {value}" for name, value in properties.items())
            if item.get("description"):
                lines.extend(["", str(item["description"])])
            stored_builder.add_section(
                f"item:{index}",
                kind="item",
                label=str(item["name"]),
                lines=lines,
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
            "requested_url": requested_url,
            "fetched_url": str((metadata or {}).get("fetched_url") or display_url),
            "canonical_url": document_data.get("canonical_url"),
            "original_size": len(content),
            "producer_truncated": output != stored_output,
            "compact_output_size": len(output),
            "stored_output_size": len(stored_output),
            "producer_output_size": len(stored_output),
            "producer_truncation_reason": ("compact_preview" if output != stored_output else None),
            "url_provenance": document_data.get("url_provenance")
            or url_provenance(url, fetched_url=display_url),
        }
    )
    merged_metadata.pop("transcript_chunks", None)
    return ToolResult(output=output, metadata=merged_metadata)


def _pdf_page_anchors(content: str) -> list[dict[str, object]]:
    lines = content.splitlines()
    found: list[tuple[str, int]] = []
    for line_number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"\[\[(metadata|page:(\d+))\]\]", line.strip())
        if match:
            found.append((match.group(1), line_number))
    anchors: list[dict[str, object]] = []
    for index, (name, start_line) in enumerate(found):
        end_line = found[index + 1][1] - 1 if index + 1 < len(found) else len(lines)
        item: dict[str, object] = {
            "anchor": name,
            "label": "PDF metadata" if name == "metadata" else f"PDF page {name.split(':')[1]}",
            "kind": "metadata" if name == "metadata" else "page",
            "format": "pdf",
            "start_line": start_line,
            "end_line": end_line,
        }
        if name.startswith("page:"):
            item["artifact_part"] = {"page": int(name.split(":", 1)[1])}
        anchors.append(item)
    return anchors


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
    browser_fetch_mode = document.get("browser_fetch_mode")
    if isinstance(browser_fetch_mode, str) and browser_fetch_mode.strip():
        lines.append(f"Browser mode: {browser_fetch_mode.strip()}")
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
        ("Source page", "source_page_url"),
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
    metadata: dict[str, object] = {"source_tool": "web_fetch" if source_url else "web_search"}
    if source_url:
        metadata["source_page_url"] = source_url
    for key in ("role", "source", "source_page_url", "alt", "caption", "width", "height"):
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

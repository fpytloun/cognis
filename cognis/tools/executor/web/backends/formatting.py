"""Helpers for compact structured web tool outputs."""

from __future__ import annotations

from urllib.parse import urlparse

from cognis.core.anchored_output import AnchoredTextBuilder, compact_snippet
from cognis.models.tool import ToolResult


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

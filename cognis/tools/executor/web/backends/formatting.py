"""Helpers for compact structured web tool outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from cognis.models.tool import ToolResult

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class OutputAnchor:
    """Structured anchor metadata for a saved tool output section."""

    anchor: str
    label: str | None
    kind: str
    start_line: int
    end_line: int


class AnchoredTextBuilder:
    """Build line-based output while recording stable section anchors."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._anchors: list[OutputAnchor] = []

    def add_line(self, line: str = "") -> None:
        self._lines.append(line)

    def add_section(self, anchor: str, *, kind: str, label: str | None, lines: list[str]) -> None:
        if not lines:
            return
        start_line = len(self._lines) + 1
        self._lines.append(f"[[{anchor}]]")
        self._lines.extend(lines)
        end_line = len(self._lines)
        self._anchors.append(
            OutputAnchor(
                anchor=anchor,
                label=label,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
            )
        )
        self._lines.append("")

    def build(self) -> tuple[str, list[dict[str, object]]]:
        text = "\n".join(self._lines).rstrip()
        anchors = [
            {
                "anchor": item.anchor,
                "label": item.label,
                "kind": item.kind,
                "start_line": item.start_line,
                "end_line": item.end_line,
            }
            for item in self._anchors
        ]
        return text, anchors


def compact_snippet(text: str, *, max_chars: int = 600) -> str:
    """Normalize and hard-cap a snippet for prompt-friendly search output."""
    normalized = _WHITESPACE_RE.sub(" ", text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - len(" [snippet truncated]")].rstrip() + " [snippet truncated]"


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

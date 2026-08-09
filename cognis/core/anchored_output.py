"""Shared helpers for compact anchored tool outputs."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from html import unescape
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")
_MARKDOWN_ATX_HEADING_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marks>#{1,6})(?!#)(?P<title>\s+.+?)\s*$"
)
_MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
_HTML_HEADING_RE = re.compile(
    r"<h(?P<level>[1-6])(?:\s[^>]*)?>(?P<title>.*?)</h(?P=level)>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SETEXT_RE = re.compile(r"^(?P<marks>=+|-+)\s*$")


@dataclass(slots=True)
class OutputAnchor:
    """Structured anchor metadata for a saved tool output section."""

    anchor: str
    label: str | None
    kind: str
    start_line: int
    end_line: int
    artifact_candidate: dict[str, Any] | None = None


class AnchoredTextBuilder:
    """Build line-based output while recording stable section anchors."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._anchors: list[OutputAnchor] = []

    def add_line(self, line: str = "") -> None:
        self._lines.append(line)

    def add_section(
        self,
        anchor: str,
        *,
        kind: str,
        label: str | None,
        lines: list[str],
        artifact_candidate: dict[str, Any] | None = None,
    ) -> None:
        if not lines:
            return
        start_line = len(self._lines) + 1
        self._lines.append(f"[[{anchor}]]")
        for line in lines:
            # Callers commonly pass extracted Markdown as one string. Anchor
            # locators are physical line numbers, so embedded newlines must
            # contribute to the builder's line count.
            self._lines.extend(str(line).splitlines() or [""])
        end_line = len(self._lines)
        self._anchors.append(
            OutputAnchor(
                anchor=anchor,
                label=label,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                artifact_candidate=artifact_candidate,
            )
        )
        self._lines.append("")

    def build(self) -> tuple[str, list[dict[str, object]]]:
        text = "\n".join(self._lines).rstrip()
        anchors: list[dict[str, object]] = []
        for item in self._anchors:
            anchor: dict[str, object] = {
                "anchor": item.anchor,
                "label": item.label,
                "kind": item.kind,
                "start_line": item.start_line,
                "end_line": item.end_line,
            }
            if item.artifact_candidate:
                anchor["artifact_candidate"] = item.artifact_candidate
            anchors.append(anchor)
        return text, anchors


def compact_snippet(text: str, *, max_chars: int = 600) -> str:
    """Normalize and hard-cap a snippet for prompt-friendly output."""

    normalized = _WHITESPACE_RE.sub(" ", text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - len(" [snippet truncated]")].rstrip() + " [snippet truncated]"


def markdown_heading_anchors(
    content: str | None,
    *,
    existing_anchors: Iterable[Mapping[str, Any]] | None = None,
    prefix: str = "heading",
    kind: str = "markdown_heading",
    max_anchors: int = 100,
    max_heading_level: int = 3,
) -> list[dict[str, object]]:
    """Derive line-based anchors from Markdown and HTML headings.

    The extractor recognizes ATX and Setext headings outside fenced code blocks,
    plus HTML ``h1``–``h6`` elements. All candidates are returned in source order
    and ranges include the heading through the next heading with the same or
    higher level.
    """

    if not content or max_anchors <= 0 or max_heading_level <= 0:
        return []

    existing_names: set[str] = set()
    existing_signatures: set[tuple[str, str, int, int]] = set()
    if existing_anchors is not None:
        for item in existing_anchors:
            candidate = item.get("anchor")
            if isinstance(candidate, str) and candidate:
                existing_names.add(candidate)
            label = item.get("label")
            kind_value = item.get("kind")
            start_line = item.get("start_line")
            end_line = item.get("end_line")
            if (
                isinstance(label, str)
                and isinstance(kind_value, str)
                and isinstance(start_line, int)
                and isinstance(end_line, int)
            ):
                existing_signatures.add((kind_value, label, start_line, end_line))

    lines = content.splitlines()
    headings: list[tuple[int, int, str]] = []
    fence_char: str | None = None
    fence_len = 0
    fenced_lines: set[int] = set()

    for line_no, line in enumerate(lines, start=1):
        fence_match = _MARKDOWN_FENCE_RE.match(line)
        if fence_match:
            fenced_lines.add(line_no)
            fence = fence_match.group("fence")
            char = fence[0]
            if fence_char is None:
                fence_char = char
                fence_len = len(fence)
                continue
            if char == fence_char and len(fence) >= fence_len:
                fence_char = None
                fence_len = 0
                continue

        if fence_char is not None:
            fenced_lines.add(line_no)
            continue

        match = _MARKDOWN_ATX_HEADING_RE.match(line)
        if match:
            level = len(match.group("marks"))
            title = re.sub(r"\s+#+\s*$", "", match.group("title").strip()).strip()
            if level <= max_heading_level and title:
                headings.append((line_no, level, title))
                continue
        if line_no > 1 and line.strip() and _SETEXT_RE.fullmatch(line):
            title = lines[line_no - 2].strip()
            if title and not _MARKDOWN_FENCE_RE.match(title):
                level = 1 if line.lstrip().startswith("=") else 2
                if level <= max_heading_level:
                    headings.append((line_no - 1, level, title))

    for match in _HTML_HEADING_RE.finditer(content):
        level = int(match.group("level"))
        if level > max_heading_level:
            continue
        start_line = content.count("\n", 0, match.start()) + 1
        end_line = content.count("\n", 0, match.end()) + 1
        if any(line_number in fenced_lines for line_number in range(start_line, end_line + 1)):
            continue
        title = _HTML_TAG_RE.sub("", unescape(match.group("title"))).strip()
        if title:
            headings.append((start_line, level, title))

    headings.sort(key=lambda item: (item[0], item[1], item[2]))
    deduped: list[tuple[int, int, str]] = []
    seen_heading_positions: set[tuple[int, int, str]] = set()
    for heading in headings:
        if heading in seen_heading_positions:
            continue
        seen_heading_positions.add(heading)
        deduped.append(heading)
        if len(deduped) >= max_anchors:
            break
    headings = deduped

    anchors: list[dict[str, object]] = []
    used_names = set(existing_names)
    for index, (line_no, level, title) in enumerate(headings):
        end_line = len(lines)
        for next_line, next_level, _ in headings[index + 1 :]:
            if next_level <= level:
                end_line = next_line - 1
                break
        label = title[:120]
        signature = (kind, label, line_no, end_line)
        if signature in existing_signatures:
            continue
        base_anchor = f"{prefix}:{_slugify_heading(title, index + 1)}"
        if base_anchor in existing_names:
            continue
        anchor = _unique_anchor_name(base_anchor, used_names)
        anchors.append(
            {
                "anchor": anchor,
                "label": label,
                "kind": kind,
                "start_line": line_no,
                "end_line": end_line,
            }
        )
    return anchors


def _slugify_heading(title: str, index: int) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[`*_~\[\](){}<>]", " ", normalized)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    return slug or f"section-{index}"


def _unique_anchor_name(base: str, used_names: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used_names:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used_names.add(candidate)
    return candidate

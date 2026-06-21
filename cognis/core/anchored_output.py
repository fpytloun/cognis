"""Shared helpers for compact anchored tool outputs."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")
_MARKDOWN_ATX_HEADING_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marks>#{1,6})(?!#)(?P<title>\s+.+?)\s*$"
)
_MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")


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
        self._lines.extend(lines)
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
        anchors = []
        for item in self._anchors:
            anchor = {
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
    """Derive line-based anchors from Markdown ATX headings.

    The extractor is intentionally conservative: it recognizes only ATX headings
    outside fenced code blocks and does not mutate the source text. Returned
    ranges include the heading line and continue until the next heading with the
    same or higher level, so parent sections include their nested subsections.
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

    for line_no, line in enumerate(lines, start=1):
        fence_match = _MARKDOWN_FENCE_RE.match(line)
        if fence_match:
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
            continue

        match = _MARKDOWN_ATX_HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group("marks"))
        if level > max_heading_level:
            continue
        title = match.group("title").strip()
        title = re.sub(r"\s+#+\s*$", "", title).strip()
        if title:
            headings.append((line_no, level, title))
            if len(headings) >= max_anchors:
                break

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

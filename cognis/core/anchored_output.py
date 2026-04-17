"""Shared helpers for compact anchored tool outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
    """Normalize and hard-cap a snippet for prompt-friendly output."""

    normalized = _WHITESPACE_RE.sub(" ", text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - len(" [snippet truncated]")].rstrip() + " [snippet truncated]"

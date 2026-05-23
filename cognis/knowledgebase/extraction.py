"""Artifact text extraction for knowledgebase indexing."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SourceSpan:
    text: str
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractedDocument:
    spans: list[SourceSpan]
    extraction_method: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "text/vtt",
    "application/x-subrip",
}


def _timestamp_ms(raw: str) -> int:
    parts = raw.replace(",", ".").split(":")
    hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    return int(((hours * 60 + minutes) * 60 + seconds) * 1000)


def _extract_transcript(text: str) -> ExtractedDocument:
    spans: list[SourceSpan] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), None)
        if time_line is None:
            continue
        start_raw, end_raw = [part.strip().split()[0] for part in time_line.split("-->", 1)]
        payload = [
            line for line in lines if line != time_line and not line.isdigit() and line != "WEBVTT"
        ]
        if not payload:
            continue
        spans.append(
            SourceSpan(
                text=" ".join(payload),
                locator={
                    "timestamp_start_ms": _timestamp_ms(start_raw),
                    "timestamp_end_ms": _timestamp_ms(end_raw),
                },
            )
        )
    return ExtractedDocument(spans=spans, extraction_method="transcript")


def extract_artifact_bytes(content: bytes, *, filename: str, mime_type: str) -> ExtractedDocument:
    lower_name = filename.lower()
    if lower_name.endswith((".srt", ".vtt")) or mime_type in {"text/vtt", "application/x-subrip"}:
        return _extract_transcript(content.decode("utf-8", errors="replace"))

    if mime_type.startswith(_TEXT_MIME_PREFIXES) or mime_type in _TEXT_MIME_TYPES:
        text = content.decode("utf-8", errors="replace")
        spans: list[SourceSpan] = []
        char_pos = 0
        for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
            line_text = line.rstrip("\r\n")
            spans.append(
                SourceSpan(
                    text=line_text,
                    locator={
                        "line_start": line_no,
                        "line_end": line_no,
                        "char_start": char_pos,
                        "char_end": char_pos + len(line_text),
                    },
                )
            )
            char_pos += len(line)
        return ExtractedDocument(spans=spans, extraction_method="text")

    if mime_type == "application/pdf" or lower_name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - dependency is project-default
            raise RuntimeError("pypdf is required for PDF knowledgebase indexing") from exc
        reader = PdfReader(io.BytesIO(content))
        spans = []
        for page_index, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            if page_text.strip():
                spans.append(
                    SourceSpan(
                        text=page_text,
                        locator={"page_start": page_index, "page_end": page_index},
                    )
                )
        return ExtractedDocument(spans=spans, extraction_method="pdf")

    if lower_name.endswith(".docx") or mime_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    }:
        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError("python-docx is required for DOCX knowledgebase indexing") from exc
        document = Document(io.BytesIO(content))
        spans = [
            SourceSpan(
                text=paragraph.text,
                locator={"paragraph_start": index, "paragraph_end": index},
            )
            for index, paragraph in enumerate(document.paragraphs, start=1)
            if paragraph.text.strip()
        ]
        return ExtractedDocument(spans=spans, extraction_method="docx")

    raise RuntimeError(f"Unsupported artifact type for knowledgebase indexing: {mime_type}")

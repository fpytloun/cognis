"""Token-aware, source-aware chunking for extracted knowledgebase documents."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cognis.knowledgebase.extraction import ExtractedDocument, SourceSpan

DEFAULT_TARGET_TOKENS = 800
DEFAULT_OVERLAP_TOKENS = 100
FALLBACK_CHARS_PER_TOKEN = 4

_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


class KnowledgebaseChunkLimitExceeded(RuntimeError):
    """Raised when an artifact would produce more chunks than allowed."""

    def __init__(self, *, max_chunks: int) -> None:
        super().__init__(
            "knowledgebase_artifact_exceeds_max_chunks: "
            f"artifact would produce more than {max_chunks} chunks; split it into smaller "
            "artifacts or increase COGNIS_KNOWLEDGEBASE_MAX_CHUNKS_PER_ARTIFACT"
        )
        self.max_chunks = max_chunks


@dataclass(slots=True)
class KnowledgebaseChunk:
    text: str
    chunk_index: int
    locator: dict[str, Any]
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _merge_locators(
    locators: list[dict[str, Any]],
    *,
    artifact_id: str,
    artifact_hash: str | None,
    chunk_id: str,
    chunk_index: int,
    extraction_method: str,
) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "artifact_id": artifact_id,
        "artifact_hash": artifact_hash,
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "extraction_method": extraction_method,
    }
    for key in (
        "char_start",
        "byte_start",
        "line_start",
        "page_start",
        "paragraph_start",
        "timestamp_start_ms",
    ):
        values = [locator[key] for locator in locators if locator.get(key) is not None]
        if values:
            merged[key] = min(values)
    for key in (
        "char_end",
        "byte_end",
        "line_end",
        "page_end",
        "paragraph_end",
        "timestamp_end_ms",
    ):
        values = [locator[key] for locator in locators if locator.get(key) is not None]
        if values:
            merged[key] = max(values)
    heading_stacks = [
        locator["heading_stack"]
        for locator in locators
        if isinstance(locator.get("heading_stack"), list)
    ]
    if heading_stacks:
        merged["heading_stack"] = heading_stacks[-1]
    return merged


def _default_token_count(text: str) -> int:
    try:
        import tiktoken
    except ImportError:
        return max(1, (len(text) + FALLBACK_CHARS_PER_TOKEN - 1) // FALLBACK_CHARS_PER_TOKEN)
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        return max(1, (len(text) + FALLBACK_CHARS_PER_TOKEN - 1) // FALLBACK_CHARS_PER_TOKEN)
    return max(1, len(encoding.encode(text)))


def _join_parts(parts: list[str]) -> str:
    return "\n".join(part for part in parts if part.strip()).strip()


def _with_heading_context(text: str, heading_stack: list[str]) -> str:
    if not heading_stack:
        return text
    prefix = "\n".join(heading_stack)
    if text.startswith(prefix):
        return text
    return f"{prefix}\n\n{text}"


def _split_long_text(
    span: SourceSpan,
    *,
    target_tokens: int,
    count_tokens: Callable[[str], int],
) -> list[SourceSpan]:
    text = span.text.strip()
    if not text or count_tokens(text) <= target_tokens:
        return [span]

    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(parts) <= 1:
        parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    if len(parts) <= 1:
        words = text.split()
        parts = []
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if current and count_tokens(candidate) > target_tokens:
                parts.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            parts.append(" ".join(current))
    if len(parts) <= 1 and count_tokens(text) > target_tokens:
        target_chars = max(1, target_tokens * FALLBACK_CHARS_PER_TOKEN)
        parts = [text[index : index + target_chars] for index in range(0, len(text), target_chars)]
        while any(count_tokens(part) > target_tokens for part in parts) and target_chars > 1:
            target_chars = max(1, target_chars // 2)
            parts = [
                text[index : index + target_chars] for index in range(0, len(text), target_chars)
            ]

    result: list[SourceSpan] = []
    current_parts: list[str] = []
    for part in parts:
        candidate = _join_parts([*current_parts, part])
        if current_parts and count_tokens(candidate) > target_tokens:
            result.append(SourceSpan(text=_join_parts(current_parts), locator=dict(span.locator)))
            current_parts = [part]
        else:
            current_parts.append(part)
    if current_parts:
        result.append(SourceSpan(text=_join_parts(current_parts), locator=dict(span.locator)))
    return result


def _prepare_spans(
    document: ExtractedDocument,
    *,
    is_markdown: bool,
    target_tokens: int,
    count_tokens: Callable[[str], int],
) -> list[SourceSpan]:
    prepared: list[SourceSpan] = []
    heading_stack: list[str] = []

    for span in document.spans:
        text = span.text.strip()
        if not text:
            continue
        locator = dict(span.locator)
        if is_markdown:
            match = _MARKDOWN_HEADING_RE.match(text)
            if match is not None:
                level = len(match.group(1))
                heading_stack = [*heading_stack[: level - 1], text]
                locator["heading_level"] = level
                locator["heading_stack"] = list(heading_stack)
                prepared.append(SourceSpan(text=text, locator=locator))
                continue
            if heading_stack:
                locator["heading_stack"] = list(heading_stack)
        contextual_text = _with_heading_context(text, heading_stack) if is_markdown else text
        prepared.extend(
            _split_long_text(
                SourceSpan(text=contextual_text, locator=locator),
                target_tokens=target_tokens,
                count_tokens=count_tokens,
            )
        )
    return prepared


def _is_markdown_document(metadata: dict[str, Any]) -> bool:
    filename = str(metadata.get("filename") or "").lower()
    mime_type = str(metadata.get("mime_type") or "").lower().split(";", 1)[0].strip()
    return mime_type == "text/markdown" or filename.endswith((".md", ".mdown", ".markdown"))


def chunk_document(
    document: ExtractedDocument,
    *,
    artifact_id: str,
    artifact_hash: str | None,
    chunk_id_prefix: str,
    metadata: dict[str, Any] | None = None,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    max_chunks: int = 2000,
    token_counter: Callable[[str], int] | None = None,
) -> list[KnowledgebaseChunk]:
    chunks: list[KnowledgebaseChunk] = []
    current_text: list[str] = []
    current_locators: list[dict[str, Any]] = []
    chunk_meta = dict(metadata or {})
    count_tokens = token_counter or _default_token_count
    target_tokens = max(1, target_tokens)
    overlap_tokens = max(0, min(overlap_tokens, target_tokens // 2))
    prepared_spans = _prepare_spans(
        document,
        is_markdown=_is_markdown_document(chunk_meta),
        target_tokens=target_tokens,
        count_tokens=count_tokens,
    )
    chunks_exhausted = False

    def flush() -> None:
        nonlocal chunks_exhausted
        if not current_text:
            return
        if len(chunks) >= max_chunks:
            raise KnowledgebaseChunkLimitExceeded(max_chunks=max_chunks)
        text = _join_parts(current_text)
        if not text:
            current_text.clear()
            current_locators.clear()
            return
        chunk_index = len(chunks)
        chunk_id = f"{chunk_id_prefix}_{chunk_index:06d}"
        token_count = count_tokens(text)
        chunks.append(
            KnowledgebaseChunk(
                text=text,
                chunk_index=chunk_index,
                token_count=token_count,
                locator=_merge_locators(
                    current_locators,
                    artifact_id=artifact_id,
                    artifact_hash=artifact_hash,
                    chunk_id=chunk_id,
                    chunk_index=chunk_index,
                    extraction_method=document.extraction_method,
                ),
                metadata=chunk_meta,
            )
        )
        chunks_exhausted = len(chunks) >= max_chunks
        overlap_text: list[str] = []
        overlap_locators: list[dict[str, Any]] = []
        if overlap_tokens > 0:
            for part, locator in zip(
                reversed(current_text),
                reversed(current_locators),
                strict=True,
            ):
                candidate = _join_parts([part, *overlap_text])
                if overlap_text and count_tokens(candidate) > overlap_tokens:
                    break
                overlap_text.insert(0, part)
                overlap_locators.insert(0, locator)
                if count_tokens(candidate) >= overlap_tokens:
                    break
        current_text[:] = overlap_text
        current_locators[:] = overlap_locators
        if chunks_exhausted:
            current_text.clear()
            current_locators.clear()

    for span in prepared_spans:
        if chunks_exhausted:
            raise KnowledgebaseChunkLimitExceeded(max_chunks=max_chunks)
        prospective = _join_parts([*current_text, span.text])
        if count_tokens(prospective) > target_tokens and current_text:
            flush()
            prospective = _join_parts([*current_text, span.text])
            if count_tokens(prospective) > target_tokens and current_text:
                flush()
        current_text.append(span.text)
        current_locators.append(span.locator)
    flush()
    return chunks

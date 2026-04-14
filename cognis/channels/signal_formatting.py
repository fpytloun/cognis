"""Signal-specific outbound rich text formatting.

Converts assistant markdown-ish output into:
- rendered plain text
- Signal text style ranges
- Signal REST ``styled`` text syntax

Signal style offsets use UTF-16 code units, but this module keeps internal
offsets in Python string indices and converts them at the transport boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_UNORDERED_LIST_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_ORDERED_LIST_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")

_STYLE_TO_SIGNAL = {
    "bold": "BOLD",
    "italic": "ITALIC",
    "strikethrough": "STRIKETHROUGH",
    "monospace": "MONOSPACE",
    "spoiler": "SPOILER",
}

_STYLE_TO_PRIORITY = {
    "spoiler": 0,
    "strikethrough": 1,
    "bold": 2,
    "italic": 3,
    "monospace": 4,
}


@dataclass(slots=True)
class SignalStyleSpan:
    start: int
    end: int
    style: str
    block: bool = False


@dataclass(slots=True)
class SignalFormattedChunk:
    plain_text: str
    markdown_text: str
    styles: list[SignalStyleSpan]


@dataclass(slots=True)
class _RenderedText:
    text: str
    spans: list[SignalStyleSpan]


def format_for_signal(content: str, max_length: int) -> list[SignalFormattedChunk]:
    """Format assistant output into Signal-ready chunks."""
    rendered = _render_blocks(content)
    if not rendered.text and not rendered.spans:
        return []

    chunks = _chunk_rendered_text(rendered, max_length)
    return [
        SignalFormattedChunk(
            plain_text=chunk.text,
            markdown_text=_render_signal_markdown(chunk.text, chunk.spans),
            styles=chunk.spans,
        )
        for chunk in chunks
    ]


def utf16_length(text: str) -> int:
    """Return the UTF-16 code unit length of *text*."""
    return len(text.encode("utf-16-le")) // 2


def to_signal_text_styles(text: str, spans: list[SignalStyleSpan]) -> list[str]:
    """Encode style spans for signal-cli JSON-RPC ``send`` params."""
    encoded: list[str] = []
    for span in sorted(
        spans, key=lambda span: (span.start, span.end, _STYLE_TO_PRIORITY[span.style])
    ):
        start = utf16_length(text[: span.start])
        length = utf16_length(text[span.start : span.end])
        if length <= 0:
            continue
        encoded.append(f"{start}:{length}:{_STYLE_TO_SIGNAL[span.style]}")
    return encoded


def _render_blocks(content: str) -> _RenderedText:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return _RenderedText(text="", spans=[])

    lines = normalized.split("\n")
    blocks: list[_RenderedText] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        if line.lstrip().startswith("```"):
            fence_indent = len(line) - len(line.lstrip())
            code_lines: list[str] = []
            i += 1
            while i < len(lines):
                candidate = lines[i]
                if (
                    candidate.lstrip().startswith("```")
                    and (len(candidate) - len(candidate.lstrip())) <= fence_indent
                ):
                    i += 1
                    break
                code_lines.append(candidate)
                i += 1
            code_text = "\n".join(code_lines).strip("\n")
            blocks.append(_RenderedText(code_text, _full_span(code_text, "monospace", block=True)))
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            rendered = _render_inline(heading.group(2).strip())
            if rendered.text:
                rendered.spans.extend(_full_span(rendered.text, "bold"))
                blocks.append(_merge_adjacent_spans(rendered))
            i += 1
            continue

        if _HR_RE.match(stripped):
            i += 1
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote_lines.append(lines[i])
                i += 1
            rendered_lines: list[_RenderedText] = []
            for quote_line in quote_lines:
                body = re.sub(r"^\s*>\s?", "", quote_line)
                line_rendered = _render_inline(body)
                rendered_lines.append(_prefix_rendered(line_rendered, "> "))
            blocks.append(_join_rendered(rendered_lines, "\n"))
            continue

        unordered = _UNORDERED_LIST_RE.match(line)
        if unordered:
            items: list[_RenderedText] = []
            while i < len(lines):
                match = _UNORDERED_LIST_RE.match(lines[i])
                if match is None:
                    break
                rendered = _render_inline(match.group(3).strip())
                items.append(_prefix_rendered(rendered, f"{match.group(1)}{match.group(2)} "))
                i += 1
            blocks.append(_join_rendered(items, "\n"))
            continue

        ordered = _ORDERED_LIST_RE.match(line)
        if ordered:
            items = []
            while i < len(lines):
                match = _ORDERED_LIST_RE.match(lines[i])
                if match is None:
                    break
                rendered = _render_inline(match.group(3).strip())
                items.append(_prefix_rendered(rendered, f"{match.group(1)}{match.group(2)}. "))
                i += 1
            blocks.append(_join_rendered(items, "\n"))
            continue

        paragraph_lines: list[str] = []
        while i < len(lines):
            candidate = lines[i]
            candidate_stripped = candidate.strip()
            if not candidate_stripped:
                break
            if candidate.lstrip().startswith("```"):
                break
            if _HEADING_RE.match(candidate_stripped):
                break
            if _HR_RE.match(candidate_stripped):
                break
            if candidate_stripped.startswith(">"):
                break
            if _UNORDERED_LIST_RE.match(candidate) or _ORDERED_LIST_RE.match(candidate):
                break
            paragraph_lines.append(candidate_stripped)
            i += 1
        blocks.append(_render_inline("\n".join(paragraph_lines)))

    result = _join_rendered(blocks, "\n\n")
    return _merge_adjacent_spans(result)


def _render_inline(text: str) -> _RenderedText:
    parsed, _, _ = _parse_inline(text, 0, None)
    return _merge_adjacent_spans(parsed)


def _parse_inline(text: str, start: int, stop: str | None) -> tuple[_RenderedText, int, bool]:
    parts: list[str] = []
    spans: list[SignalStyleSpan] = []
    i = start
    while i < len(text):
        if stop and text.startswith(stop, i):
            return _RenderedText("".join(parts), spans), i + len(stop), True

        if text[i] == "\\":
            if i + 1 < len(text):
                parts.append(text[i + 1])
                i += 2
            else:
                parts.append("\\")
                i += 1
            continue

        if text.startswith("[", i):
            linked = _consume_link(text, i)
            if linked is not None:
                rendered, next_i = linked
                spans.extend(_shift_spans(rendered.spans, len("".join(parts))))
                parts.append(rendered.text)
                i = next_i
                continue

        token = _next_inline_token(text, i)
        if token is not None:
            style, delimiter = token
            if style == "monospace":
                code_span = _consume_code_span(text, i)
                if code_span is not None:
                    inner_text, next_i = code_span
                    offset = len("".join(parts))
                    parts.append(inner_text)
                    if inner_text:
                        spans.append(SignalStyleSpan(offset, offset + len(inner_text), style))
                    i = next_i
                    continue
            inner, next_i, closed = _parse_inline(text, i + len(delimiter), delimiter)
            if closed:
                offset = len("".join(parts))
                parts.append(inner.text)
                spans.extend(_shift_spans(inner.spans, offset))
                if inner.text:
                    spans.append(SignalStyleSpan(offset, offset + len(inner.text), style))
                i = next_i
                continue

        parts.append(text[i])
        i += 1

    return _RenderedText("".join(parts), spans), i, stop is None


def _next_inline_token(text: str, index: int) -> tuple[str, str] | None:
    if text.startswith("`", index):
        return "monospace", "`"
    if text.startswith("**", index) and _can_use_emphasis_delimiter(text, index, 2):
        return "bold", "**"
    if text.startswith("~~", index):
        return "strikethrough", "~~"
    if text.startswith("||", index):
        return "spoiler", "||"
    if text[index] == "*" and _can_use_emphasis_delimiter(text, index, 1):
        return "italic", "*"
    if text[index] == "_" and _can_use_emphasis_delimiter(text, index, 1):
        return "italic", "_"
    return None


def _consume_link(text: str, index: int) -> tuple[_RenderedText, int] | None:
    if text[index] != "[":
        return None
    label_start = index + 1
    label_end = _find_unescaped(text, "]", label_start)
    if label_end == -1 or label_end + 1 >= len(text) or text[label_end + 1] != "(":
        return None
    url_end = _find_link_url_end(text, label_end + 2)
    if url_end == -1:
        return None

    label = text[label_start:label_end]
    url = text[label_end + 2 : url_end].strip()
    rendered = _render_inline(label)
    if not url:
        return rendered, url_end + 1

    suffix = "" if rendered.text.strip() == url else f" ({url})"
    return _append_text(rendered, suffix), url_end + 1


def _consume_code_span(text: str, index: int) -> tuple[str, int] | None:
    if text[index] != "`":
        return None
    end = _find_unescaped(text, "`", index + 1)
    if end == -1:
        return None
    return text[index + 1 : end], end + 1


def _can_use_emphasis_delimiter(text: str, index: int, delimiter_length: int) -> bool:
    previous_char = text[index - 1] if index > 0 else ""
    next_index = index + delimiter_length
    next_char = text[next_index] if next_index < len(text) else ""

    if delimiter_length == 1 and text[index] == "_":
        if previous_char.isalnum() and next_char.isalnum():
            return False

    if delimiter_length == 1 and text[index] == "*":
        if previous_char.isdigit() and next_char.isdigit():
            return False

    if not next_char:
        return False
    if next_char.isspace():
        return False
    if previous_char and previous_char.isspace() and next_char in ",.!?:;)]}":
        return False
    return True


def _find_link_url_end(text: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            if depth == 0:
                return index
            depth -= 1
        index += 1
    return -1


def _find_unescaped(text: str, needle: str, start: int) -> int:
    index = start
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text.startswith(needle, index):
            return index
        index += 1
    return -1


def _append_text(rendered: _RenderedText, suffix: str) -> _RenderedText:
    if not suffix:
        return rendered
    return _RenderedText(f"{rendered.text}{suffix}", rendered.spans.copy())


def _prefix_rendered(rendered: _RenderedText, prefix: str) -> _RenderedText:
    if not prefix:
        return rendered
    return _RenderedText(prefix + rendered.text, _shift_spans(rendered.spans, len(prefix)))


def _join_rendered(parts: list[_RenderedText], separator: str) -> _RenderedText:
    text_parts: list[str] = []
    spans: list[SignalStyleSpan] = []
    offset = 0
    for index, part in enumerate(parts):
        if index > 0:
            text_parts.append(separator)
            offset += len(separator)
        text_parts.append(part.text)
        spans.extend(_shift_spans(part.spans, offset))
        offset += len(part.text)
    return _RenderedText("".join(text_parts), spans)


def _shift_spans(spans: list[SignalStyleSpan], offset: int) -> list[SignalStyleSpan]:
    return [
        SignalStyleSpan(span.start + offset, span.end + offset, span.style, span.block)
        for span in spans
    ]


def _full_span(text: str, style: str, *, block: bool = False) -> list[SignalStyleSpan]:
    return [SignalStyleSpan(0, len(text), style, block)] if text else []


def _merge_adjacent_spans(rendered: _RenderedText) -> _RenderedText:
    ordered = sorted(rendered.spans, key=lambda span: (span.style, span.start, span.end))
    merged: list[SignalStyleSpan] = []
    for span in ordered:
        if span.start >= span.end:
            continue
        if (
            merged
            and merged[-1].style == span.style
            and merged[-1].block == span.block
            and merged[-1].end >= span.start
        ):
            merged[-1] = SignalStyleSpan(
                merged[-1].start,
                max(merged[-1].end, span.end),
                span.style,
                span.block,
            )
            continue
        merged.append(span)
    return _RenderedText(rendered.text, merged)


def _chunk_rendered_text(rendered: _RenderedText, max_length: int) -> list[_RenderedText]:
    if len(rendered.text) <= max_length:
        return [rendered]

    chunks: list[_RenderedText] = []
    start = 0
    text = rendered.text
    while start < len(text):
        remaining = text[start:]
        if len(remaining) <= max_length:
            chunks.append(_slice_rendered(rendered, start, len(text)))
            break

        split_at = _find_split_point(remaining, max_length)
        absolute_split = start + split_at
        absolute_split = _adjust_split_for_spans(
            rendered.spans, start, absolute_split, len(text), max_length
        )
        if absolute_split <= start:
            absolute_split = min(start + max_length, len(text))
        chunks.append(_slice_rendered(rendered, start, absolute_split))
        start = absolute_split
        while start < len(text) and text[start].isspace():
            start += 1
    return chunks


def _slice_rendered(rendered: _RenderedText, start: int, end: int) -> _RenderedText:
    while (
        end > start
        and rendered.text[end - 1].isspace()
        and not any(span.start <= end - 1 < span.end for span in rendered.spans)
    ):
        end -= 1
    text = rendered.text[start:end]
    spans: list[SignalStyleSpan] = []
    for span in rendered.spans:
        overlap_start = max(span.start, start)
        overlap_end = min(span.end, end)
        if overlap_start >= overlap_end:
            continue
        spans.append(
            SignalStyleSpan(overlap_start - start, overlap_end - start, span.style, span.block)
        )
    return _RenderedText(text, spans)


def _find_split_point(text: str, max_length: int) -> int:
    idx = text.rfind("\n\n", 0, max_length)
    if idx >= max_length // 6:
        return idx + 2
    idx = text.rfind("\n", 0, max_length)
    if idx >= max_length // 6:
        return idx + 1
    for pattern in (". ", "! ", "? "):
        idx = text.rfind(pattern, 0, max_length)
        if idx > max_length // 4:
            return idx + len(pattern)
    idx = text.rfind(" ", 0, max_length)
    if idx > max_length // 4:
        return idx + 1
    return max_length


def _adjust_split_for_spans(
    spans: list[SignalStyleSpan],
    chunk_start: int,
    split_at: int,
    text_length: int,
    max_length: int,
) -> int:
    containing = [span for span in spans if span.start < split_at < span.end]
    if not containing:
        return split_at

    safe_before = max((span.start for span in containing if span.start > chunk_start), default=-1)
    if safe_before != -1 and safe_before - chunk_start >= max_length // 4:
        return safe_before

    safe_after = min(
        (span.end for span in containing if span.end < chunk_start + max_length), default=-1
    )
    if safe_after != -1 and safe_after < text_length:
        return safe_after

    return split_at


def _render_signal_markdown(text: str, spans: list[SignalStyleSpan]) -> str:
    if not text:
        return ""

    open_markers: dict[int, list[tuple[SignalStyleSpan, str]]] = {}
    close_markers: dict[int, list[tuple[SignalStyleSpan, str]]] = {}
    for span in spans:
        if span.start >= span.end:
            continue
        opening, closing = _markers_for_span(text[span.start : span.end], span)
        open_markers.setdefault(span.start, []).append((span, opening))
        close_markers.setdefault(span.end, []).append((span, closing))

    result: list[str] = []
    for index, char in enumerate(text):
        if index in close_markers:
            for _, marker in sorted(
                close_markers[index],
                key=lambda item: _STYLE_TO_PRIORITY[item[0].style],
                reverse=True,
            ):
                result.append(marker)
        if index in open_markers:
            for _, marker in sorted(
                open_markers[index], key=lambda item: _STYLE_TO_PRIORITY[item[0].style]
            ):
                result.append(marker)
        result.append(
            _escape_signal_plain_text(char, in_monospace=_is_monospace_index(spans, index))
        )

    final_index = len(text)
    if final_index in close_markers:
        for _, marker in sorted(
            close_markers[final_index],
            key=lambda item: _STYLE_TO_PRIORITY[item[0].style],
            reverse=True,
        ):
            result.append(marker)
    return "".join(result)


def _markers_for_span(content: str, span: SignalStyleSpan) -> tuple[str, str]:
    style = span.style
    if style == "bold":
        return "**", "**"
    if style == "italic":
        return "*", "*"
    if style == "strikethrough":
        return "~", "~"
    if style == "spoiler":
        return "||", "||"
    if style == "monospace":
        fence_len = max(1, _max_backtick_run(content) + 1)
        if span.block or "\n" in content:
            fence_len = max(3, fence_len)
            fence = "`" * fence_len
            return f"{fence}\n", f"\n{fence}"
        fence = "`" * fence_len
        return fence, fence
    raise ValueError(f"Unsupported Signal style: {style}")


def _max_backtick_run(text: str) -> int:
    runs = re.findall(r"`+", text)
    return max((len(run) for run in runs), default=0)


def _is_monospace_index(spans: list[SignalStyleSpan], index: int) -> bool:
    return any(span.style == "monospace" and span.start <= index < span.end for span in spans)


def _escape_signal_plain_text(char: str, *, in_monospace: bool = False) -> str:
    if in_monospace:
        return char
    if char == "\\":
        return "\\"
    if char in {"*", "~", "|", "`"}:
        return f"\\{char}"
    return char

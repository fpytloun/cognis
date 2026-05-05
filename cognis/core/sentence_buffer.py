"""Incremental sentence boundary detection for streamed assistant tokens.

Used by ``WebSocketTurnObserver`` in conversation mode to emit
``tts_sentence_ready`` frames as soon as each sentence is complete, so the
client can synthesize and play audio with minimal perceived latency.

Design choices:

- Sentences inside fenced code blocks are excluded (no value in reading
  code aloud, and the result would sound jarring).
- Markdown formatting (links, bold, italics, headings, inline code) is
  stripped before emitting so the TTS provider sees clean prose.
- Boundary detection is tuned for low-latency voice: full sentence
  terminators emit at whitespace and can also emit at the current stream
  boundary once the fragment is long enough. Long clauses and oversized
  fragments emit at soft boundaries so a voice reply can start before a
  very long sentence is complete.
- Buffering is per-message-id; ``feed`` returns a list of newly completed
  sentences so callers can yield zero, one, or many frames per token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Inline markdown patterns to strip before TTS.
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MARKDOWN_ITALIC = re.compile(r"(?<![*_])[*_]([^*_]+)[*_](?![*_])")
_MARKDOWN_INLINE_CODE = re.compile(r"`([^`]+)`")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MARKDOWN_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)

_FENCE = "```"

_MIN_SENTENCE_CHARS = 8
_EARLY_TERMINATOR_MIN_CHARS = 24
_SOFT_BOUNDARY_MIN_CHARS = 80
_MAX_SEGMENT_CHARS = 180


def strip_markdown_for_tts(text: str) -> str:
    """Strip inline markdown so the TTS engine reads natural prose."""
    text = _MARKDOWN_HEADING.sub("", text)
    text = _MARKDOWN_BULLET.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _MARKDOWN_BOLD.sub(r"\1", text)
    text = _MARKDOWN_ITALIC.sub(r"\1", text)
    text = _MARKDOWN_INLINE_CODE.sub(r"\1", text)
    return text.strip()


@dataclass
class SentenceBuffer:
    """Stateful sentence boundary detector for streamed assistant tokens."""

    _buffer: str = ""
    _in_code_block: bool = False
    _next_index: int = 0
    emitted_sentences: list[str] = field(default_factory=list)

    def feed(self, token: str) -> list[tuple[int, str]]:
        """Append ``token`` and return any newly completed sentences.

        Each tuple is ``(index, text)`` where ``index`` is a per-buffer
        monotonically increasing counter starting at zero. The returned
        text is markdown-stripped and ready for synthesis.
        """
        if not token:
            return []
        self._buffer += token
        ready: list[tuple[int, str]] = []
        while True:
            sentence, remainder, found = self._extract_next_sentence()
            if not found:
                break
            self._buffer = remainder
            cleaned = strip_markdown_for_tts(sentence)
            if cleaned and len(cleaned) >= _MIN_SENTENCE_CHARS:
                ready.append((self._next_index, cleaned))
                self.emitted_sentences.append(cleaned)
                self._next_index += 1
        return ready

    def flush(self) -> tuple[int, str] | None:
        """Emit any trailing partial sentence after the stream ends.

        Returns ``None`` when there is nothing to flush or the trailing
        text is empty/too short. Otherwise returns the same
        ``(index, text)`` shape as ``feed``.
        """
        if not self._buffer.strip():
            self._buffer = ""
            return None
        cleaned = strip_markdown_for_tts(self._buffer)
        self._buffer = ""
        if not cleaned or len(cleaned) < _MIN_SENTENCE_CHARS:
            return None
        result = (self._next_index, cleaned)
        self.emitted_sentences.append(cleaned)
        self._next_index += 1
        return result

    def _extract_next_sentence(self) -> tuple[str, str, bool]:
        """Walk ``_buffer`` and return ``(sentence, remainder, found)``.

        Skips over fenced code blocks entirely — when we encounter a
        ``` we toggle ``_in_code_block`` and consume up to the closing
        fence (or wait for it). No sentence is emitted while inside
        a code block.
        """
        index = 0
        buffer = self._buffer
        n = len(buffer)
        while index < n:
            if buffer.startswith(_FENCE, index):
                if not self._in_code_block:
                    # Discard everything before the fence (it's already
                    # been considered for boundaries) and enter code mode.
                    self._in_code_block = True
                    index += len(_FENCE)
                    continue
                # Closing fence — exit code mode and discard the code block.
                self._in_code_block = False
                index += len(_FENCE)
                # Keep walking the post-fence remainder so we don't drop
                # text that follows on the same chunk.
                buffer = buffer[index:]
                self._buffer = buffer
                index = 0
                n = len(buffer)
                continue
            if self._in_code_block:
                index += 1
                continue
            char = buffer[index]
            if char in ".!?":
                # Look ahead: either whitespace/newline, or end of buffer.
                next_char = buffer[index + 1] if index + 1 < n else ""
                if next_char and next_char.isspace():
                    sentence = buffer[: index + 1]
                    # Skip the trailing whitespace too.
                    rest_start = index + 1
                    while rest_start < n and buffer[rest_start].isspace():
                        rest_start += 1
                    return sentence, buffer[rest_start:], True
                if next_char == "":
                    # Streamed chunks often end exactly after punctuation.
                    # Emit long-enough fragments now instead of waiting for
                    # the next token or final message_complete.
                    sentence = buffer[: index + 1]
                    if len(strip_markdown_for_tts(sentence)) >= _EARLY_TERMINATOR_MIN_CHARS:
                        return sentence, buffer[index + 1 :], True
                    return "", buffer, False
            if char in ",;:\n" and index >= _SOFT_BOUNDARY_MIN_CHARS:
                sentence = buffer[: index + 1]
                if len(strip_markdown_for_tts(sentence)) >= _SOFT_BOUNDARY_MIN_CHARS:
                    rest_start = index + 1
                    while rest_start < n and buffer[rest_start].isspace():
                        rest_start += 1
                    return sentence, buffer[rest_start:], True
            if index >= _MAX_SEGMENT_CHARS and char.isspace():
                sentence = buffer[:index]
                if len(strip_markdown_for_tts(sentence)) >= _SOFT_BOUNDARY_MIN_CHARS:
                    rest_start = index + 1
                    while rest_start < n and buffer[rest_start].isspace():
                        rest_start += 1
                    return sentence, buffer[rest_start:], True
            index += 1
        return "", buffer, False

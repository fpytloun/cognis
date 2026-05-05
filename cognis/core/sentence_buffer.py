"""Incremental sentence boundary detection for streamed assistant tokens.

Used by ``WebSocketTurnObserver`` in conversation mode to emit
``tts_sentence_ready`` frames as soon as each sentence is complete, so the
client can synthesize and play audio with minimal perceived latency.

Design choices:

- Sentences inside fenced code blocks are excluded (no value in reading
  code aloud, and the result would sound jarring).
- Markdown formatting (links, bold, italics, headings, inline code) is
  stripped before emitting so the TTS provider sees clean prose.
- Boundary detection is strict: only ``.``, ``!``, ``?`` followed by
  whitespace counts as a mid-stream boundary. This guarantees each
  emitted segment is a full sentence so multilingual TTS can detect
  language correctly. The ``flush`` call at turn end emits any trailing
  fragment regardless of length so short closing sentences ("Yes.",
  "Sure!") are still spoken.
- A small minimum length (``_MIN_MIDSTREAM_CHARS``) is applied to
  mid-stream emissions so abbreviations like ``Mr.`` keep accumulating
  with the rest of the sentence instead of being emitted on their own.
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

# Mid-stream emissions below this length are treated as likely abbreviations
# (e.g. "Mr.", "Dr.", initials) and held back so they merge with the rest of
# the sentence on the next boundary or at flush time.
_MIN_MIDSTREAM_CHARS = 8


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
            if not cleaned:
                continue
            ready.append((self._next_index, cleaned))
            self.emitted_sentences.append(cleaned)
            self._next_index += 1
        return ready

    def flush(self) -> tuple[int, str] | None:
        """Emit any trailing partial sentence after the stream ends.

        Returns ``None`` when the buffer is empty or contains only
        whitespace/markdown. Otherwise returns the same ``(index, text)``
        shape as ``feed``. Unlike ``feed``, ``flush`` does NOT enforce a
        minimum length so short closing sentences ("Yes.", "Sure!") are
        still spoken.
        """
        if not self._buffer.strip():
            self._buffer = ""
            return None
        cleaned = strip_markdown_for_tts(self._buffer)
        self._buffer = ""
        if not cleaned:
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

        Boundaries are strict: only sentence terminators followed by
        whitespace count. The end of the current buffer is NOT a
        boundary on its own; ``flush`` handles the final fragment.

        Boundaries that would yield a sentence shorter than
        ``_MIN_MIDSTREAM_CHARS`` are treated as abbreviation noise
        (e.g. ``Mr.``, ``Dr.``, initials) and skipped so the walker
        keeps searching for the next real terminator.
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
                next_char = buffer[index + 1] if index + 1 < n else ""
                if next_char and next_char.isspace():
                    sentence = buffer[: index + 1]
                    cleaned = strip_markdown_for_tts(sentence)
                    if len(cleaned) >= _MIN_MIDSTREAM_CHARS:
                        rest_start = index + 1
                        while rest_start < n and buffer[rest_start].isspace():
                            rest_start += 1
                        return sentence, buffer[rest_start:], True
                    # Too short — likely an abbreviation. Continue
                    # walking so the surrounding sentence is captured
                    # whole on the next real boundary.
            index += 1
        return "", buffer, False

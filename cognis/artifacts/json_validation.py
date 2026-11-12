"""Incremental JSON syntax validation without materializing values or large strings."""

from __future__ import annotations

import codecs
import re

_STRING_RUN = re.compile(r'[^"\\\x00-\x1f]+')
_DIGITS = re.compile(r"[0-9]+")
_WHITESPACE = re.compile(r"[ \t\r\n]+")


def is_json_mime_type(mime_type: str) -> bool:
    """Return whether publication requires JSON syntax validation."""
    base = mime_type.split(";", 1)[0].strip().lower()
    return base == "application/json" or base.endswith("+json")


class JSONStreamValidator:
    """Validate UTF-8 JSON with bounded lexical state and a bounded nesting stack."""

    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.stack = [["root", "value"]]
        self.mode: str | None = None
        self.number = ""
        self.literal = ""
        self.literal_index = 0
        self.hex_left = 0

    def _token(self, token: str) -> None:
        frame = self.stack[-1]
        container, state = frame
        if token == "," and state == "comma":
            frame[1] = "key" if container == "object" else "value"
        elif token == ":" and container == "object" and state == "colon":
            frame[1] = "value"
        elif token in {"}", "]"}:
            if container != ("object" if token == "}" else "array") or state not in {
                "first_key",
                "first_value",
                "comma",
            }:
                raise ValueError("Invalid JSON closing delimiter")
            self.stack.pop()
        elif token == "string" and container == "object" and state in {"key", "first_key"}:
            frame[1] = "colon"
        elif token in {"string", "scalar", "{", "["} and state in {"value", "first_value"}:
            frame[1] = "end" if container == "root" else "comma"
            if token in {"{", "["}:
                if len(self.stack) >= 1000:
                    raise ValueError("JSON nesting exceeds the parser depth limit")
                self.stack.append(
                    ["object", "first_key"] if token == "{" else ["array", "first_value"]
                )
        else:
            raise ValueError("Invalid JSON token order")

    def _number_char(self, text: str, index: int) -> int:
        char = text[index]
        state = self.number
        if state in {"integer", "fraction", "exponent"} and char.isascii() and char.isdigit():
            match = _DIGITS.match(text, index)
            assert match is not None
            return match.end()
        if state in {"sign", "dot", "exponent_start", "exponent_sign"}:
            if char in "0123456789":
                self.number = (
                    ("zero" if char == "0" else "integer")
                    if state == "sign"
                    else ("fraction" if state == "dot" else "exponent")
                )
                return index + 1
            if state == "exponent_start" and char in "+-":
                self.number = "exponent_sign"
                return index + 1
            raise ValueError("Invalid JSON number")
        if char == "." and state in {"zero", "integer"}:
            self.number = "dot"
            return index + 1
        if char in "eE" and state in {"zero", "integer", "fraction"}:
            self.number = "exponent_start"
            return index + 1
        self.mode = None
        self._token("scalar")
        return index

    def feed(self, content: bytes) -> None:
        """Consume bounded input chunks; no token value is retained."""
        self._text(self.decoder.decode(content))

    def _text(self, text: str) -> None:
        index = 0
        while index < len(text):
            char = text[index]
            if self.mode == "string":
                run = _STRING_RUN.match(text, index)
                if run is not None:
                    index = run.end()
                    continue
                if char == '"':
                    self.mode = None
                    self._token("string")
                elif char == "\\":
                    self.mode = "escape"
                else:
                    raise ValueError("Unescaped control character in JSON string")
            elif self.mode == "escape":
                if char == "u":
                    self.hex_left = 4
                    self.mode = "unicode"
                elif char in '"\\/bfnrt':
                    self.mode = "string"
                else:
                    raise ValueError("Invalid JSON escape")
            elif self.mode == "unicode":
                if char not in "0123456789abcdefABCDEF":
                    raise ValueError("Invalid JSON Unicode escape")
                self.hex_left -= 1
                if self.hex_left == 0:
                    self.mode = "string"
            elif self.mode == "number":
                index = self._number_char(text, index)
                continue
            elif self.mode == "literal":
                if char != self.literal[self.literal_index]:
                    raise ValueError("Invalid JSON literal")
                self.literal_index += 1
                if self.literal_index == len(self.literal):
                    self.mode = None
                    self._token("scalar")
            elif char in " \t\r\n":
                match = _WHITESPACE.match(text, index)
                assert match is not None
                index = match.end()
                continue
            elif char in "{}[],:":
                self._token(char)
            elif char == '"':
                self.mode = "string"
            elif char in "-0123456789":
                self.mode = "number"
                self.number = "sign" if char == "-" else ("zero" if char == "0" else "integer")
            elif char in "tfn":
                self.mode = "literal"
                self.literal = {"t": "true", "f": "false", "n": "null"}[char]
                self.literal_index = 1
            else:
                raise ValueError("Invalid JSON syntax")
            index += 1

    def finish(self) -> None:
        """Reject incomplete documents before any staged object can be published."""
        self._text(self.decoder.decode(b"", final=True))
        if self.mode == "number" and self.number in {"zero", "integer", "fraction", "exponent"}:
            self.mode = None
            self._token("scalar")
        if self.mode is not None or self.stack != [["root", "end"]]:
            raise ValueError("JSON document is incomplete")

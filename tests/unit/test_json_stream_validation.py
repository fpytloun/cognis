"""Differential, boundary and invalid-token checks for bounded JSON validation."""

from __future__ import annotations

import json
import random

import pytest

from cognis.artifacts.json_validation import JSONStreamValidator


@pytest.mark.parametrize("width", [1, 2, 7, 65536])
@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        -1234,
        1.25e-30,
        [],
        {},
        {"key": ["žluťoučký 🖖", 'a"b\\c\n', {"nested": -1.23e100}]},
    ],
)
def test_valid_documents_at_every_lexical_boundary(value, width: int) -> None:
    content = json.dumps(value, ensure_ascii=False).encode()
    validator = JSONStreamValidator()
    for index in range(0, len(content), width):
        validator.feed(content[index : index + width])
    validator.finish()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b" ",
        b"{",
        b"[1,]",
        b'{"x":}',
        b'{"x" 1}',
        b'{"x":1,}',
        b"[}",
        b"01",
        b"-",
        b"1.",
        b"1e",
        b"1e+",
        b"tru",
        b"truefalse",
        b"null 1",
        b'"unclosed',
        b'"raw\nnewline"',
        b'"\\z"',
        b'"\\u123x"',
        b'"\xff"',
        b"NaN",
    ],
)
@pytest.mark.parametrize("width", [1, 5])
def test_invalid_documents_never_finish(content: bytes, width: int) -> None:
    validator = JSONStreamValidator()
    with pytest.raises((ValueError, UnicodeDecodeError)):
        for index in range(0, len(content), width):
            validator.feed(content[index : index + width])
        validator.finish()


def test_differential_random_documents() -> None:
    randomizer = random.Random(87431)
    for _ in range(500):
        value = {
            "numbers": [randomizer.uniform(-1e100, 1e100) for _ in range(8)],
            "text": "".join(randomizer.choice('abc"\\\n\tž🖖') for _ in range(70)),
            "nested": [True, None, {"value": randomizer.randint(-10000, 10000)}],
        }
        content = json.dumps(value, ensure_ascii=randomizer.choice([True, False])).encode()
        validator = JSONStreamValidator()
        index = 0
        while index < len(content):
            width = randomizer.randint(1, 31)
            validator.feed(content[index : index + width])
            index += width
        validator.finish()


def test_large_scalar_is_not_retained() -> None:
    validator = JSONStreamValidator()
    validator.feed(b'"')
    for _ in range(128):
        validator.feed(b"a" * (64 * 1024))
    validator.feed(b'"')
    validator.finish()
    assert validator.stack == [["root", "end"]]
    assert validator.mode is None

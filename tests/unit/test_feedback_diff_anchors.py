from __future__ import annotations

import hashlib

import pytest

from cognis.core.feedback.diff_anchors import validate_diff_lines
from cognis.core.feedback.primitives import FeedbackError
from cognis.models.feedback import DiffLocator

PATCH = "@@ -1,3 +1,3 @@\n def handler():\n-    return None\n+    return {'status': 'ok'}\n "


def anchor(**changes: object) -> DiffLocator:
    return DiffLocator.model_validate(
        {
            "kind": "diff",
            "scope": "lines",
            "side": "new",
            "start_line": 1,
            "end_line": 3,
            "hunk_digest": hashlib.sha256(PATCH.encode()).hexdigest(),
            "quote": "def handler():\n    return {'status': 'ok'}\n",
            **changes,
        }
    )


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_multiline_diff_quote_uses_side_text_not_raw_patch(ending: str) -> None:
    text = PATCH.replace("\n", "\r\n") if ending == "\r\n" else PATCH
    validate_diff_lines(text + ending, anchor())
    validate_diff_lines(
        text + ending, anchor(side="old", quote="def handler():\n    return None\n")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"side": "old"},
        {"start_line": 2},
        {"end_line": 4},
        {"hunk_digest": hashlib.sha256(b"another hunk").hexdigest()},
        {"quote": "return None"},
        {"quote": None},
        {"quote": "def handler():\n+    return {'status': 'ok'}\n"},
    ],
)
def test_wrong_side_range_digest_or_quote_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(FeedbackError, match="exact hunk"):
        validate_diff_lines(PATCH, anchor(**changes))


@pytest.mark.parametrize(
    "text",
    [
        PATCH.rsplit("\n", 1)[0],
        PATCH + "\n+unproven extra line",
        PATCH + "\n" + PATCH,
        "@@ -1,1 +1,1 @@\n first\n@@ -2,1 +2,1 @@\n second",
        "@@ -999999999999999999999,1 +1,1 @@\n first",
        "@@ -0,1 +1,1 @@\n first",
    ],
)
def test_incomplete_ambiguous_or_cross_hunk_evidence_is_rejected(text: str) -> None:
    with pytest.raises(FeedbackError):
        validate_diff_lines(text, anchor())


def test_blank_line_and_no_newline_marker_remain_exact_evidence() -> None:
    marker = PATCH + "\n\\ No newline at end of file"
    validate_diff_lines(marker, anchor(hunk_digest=hashlib.sha256(marker.encode()).hexdigest()))
    validate_diff_lines(PATCH, anchor(start_line=3, end_line=3, quote=""))


def test_line_quote_is_bounded_in_utf8_bytes() -> None:
    text = "@@ -1 +1 @@\n " + "😀" * 600
    with pytest.raises(FeedbackError):
        validate_diff_lines(
            text,
            anchor(
                start_line=1,
                end_line=1,
                quote="😀" * 600,
                hunk_digest=hashlib.sha256(text.encode()).hexdigest(),
            ),
        )


def test_repeated_quotes_still_require_the_correct_hunk_and_line_numbers() -> None:
    first = "@@ -1 +1 @@\n same"
    second = "@@ -7 +7 @@\n same"
    text = first + "\n" + second
    selected = anchor(
        start_line=7,
        end_line=7,
        quote="same",
        hunk_digest=hashlib.sha256(second.encode()).hexdigest(),
    )
    validate_diff_lines(text, selected)
    with pytest.raises(FeedbackError):
        validate_diff_lines(
            text,
            anchor(
                start_line=7,
                end_line=7,
                quote="same",
                hunk_digest=hashlib.sha256(first.encode()).hexdigest(),
            ),
        )
    with pytest.raises(FeedbackError):
        validate_diff_lines(
            text,
            anchor(
                start_line=1,
                end_line=7,
                quote="same\nsame",
                hunk_digest=hashlib.sha256(first.encode()).hexdigest(),
            ),
        )


@pytest.mark.parametrize("side", ["old", "new"])
def test_zero_count_opposite_side_and_unicode_are_preserved(side: str) -> None:
    text = "@@ -0,0 +1,2 @@\n+É😀\n+Å" if side == "new" else "@@ -1,2 +0,0 @@\n-É😀\n-Å"
    validate_diff_lines(
        text,
        anchor(
            side=side,
            start_line=1,
            end_line=2,
            quote="É😀\nÅ",
            hunk_digest=hashlib.sha256(text.encode()).hexdigest(),
        ),
    )


def test_an_incomplete_other_hunk_invalidates_line_evidence() -> None:
    with pytest.raises(FeedbackError):
        validate_diff_lines(PATCH + "\n@@ -8,2 +8,2 @@\n only one", anchor())

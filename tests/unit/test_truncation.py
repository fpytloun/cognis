"""Tests for middle-truncation utility."""

from __future__ import annotations

from cognis.core.truncation import middle_truncate


def test_short_text_not_truncated() -> None:
    text = "Hello, world!"
    result, was_truncated = middle_truncate(text, 1000)
    assert result == text
    assert was_truncated is False


def test_exact_limit_not_truncated() -> None:
    text = "x" * 500
    result, was_truncated = middle_truncate(text, 500)
    assert result == text
    assert was_truncated is False


def test_long_text_middle_truncated() -> None:
    text = "A" * 200 + "B" * 600 + "C" * 200
    result, was_truncated = middle_truncate(text, 500)
    assert was_truncated is True
    assert result.startswith("A")
    assert result.endswith("C")
    assert "middle truncated" in result
    assert "1,000 chars total" in result
    assert len(result) <= 500


def test_call_id_included_in_marker() -> None:
    text = "x" * 2000
    result, was_truncated = middle_truncate(text, 500, call_id="call_abc123")
    assert was_truncated is True
    assert "read_tool_output(call_id='call_abc123')" in result


def test_call_id_omitted_when_none() -> None:
    text = "x" * 2000
    result, _ = middle_truncate(text, 500, call_id=None)
    assert "read_tool_output" not in result


def test_head_ratio_adjusts_split() -> None:
    text = "H" * 500 + "M" * 500 + "T" * 500
    # 70% head, 30% tail
    result, _ = middle_truncate(text, 600, head_ratio=0.7)
    head_portion = result.split("...")[0]
    tail_portion = result.split("...")[-1]
    # Head should have more H's than tail has T's
    assert head_portion.count("H") > tail_portion.count("T")


def test_preserves_head_and_tail_content() -> None:
    head = "=== START OF OUTPUT ===\nLine 1\nLine 2\n"
    middle = "x\n" * 500
    tail = "\nLine 999\nLine 1000\n=== END OF OUTPUT ==="
    text = head + middle + tail
    result, was_truncated = middle_truncate(text, 500)
    assert was_truncated is True
    assert "START OF OUTPUT" in result
    assert "END OF OUTPUT" in result


def test_very_small_max_chars_falls_back_to_head() -> None:
    text = "x" * 1000
    result, was_truncated = middle_truncate(text, 200)
    # Below _MIN_TRUNCATION_SIZE, returns unchanged
    assert result == text
    assert was_truncated is False

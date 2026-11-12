"""Content-aware planning is deterministic, bounded, and independent of providers."""

from __future__ import annotations

import pytest

from cognis.core.audio_planning import podcast_length_estimates, spoken_word_count
from cognis.core.speech_models import AudioJobCreate


@pytest.mark.parametrize("words", [1, 30, 300, 3000, 15000])
def test_three_ordered_content_aware_budgets(words):
    plans = podcast_length_estimates("facts " * words)
    assert [plan.length for plan in plans] == ["shorter", "default", "longer"]
    assert plans[0].target_words < plans[1].target_words < plans[2].target_words
    assert 8 <= plans[0].target_words < plans[-1].target_words <= 2400
    assert all(0 < plan.seconds_min <= plan.seconds_max for plan in plans)


def test_default_adapts_to_the_source_instead_of_hiding_a_six_minute_target():
    short = podcast_length_estimates("facts " * 30)
    long = podcast_length_estimates("facts " * 3000)
    assert short[1].seconds_max < 60
    assert long[1].target_words > short[1].target_words * 20
    assert long[1].seconds_min > 300


@pytest.mark.parametrize("text", ["", " \n", "x" * 120001])
def test_rejects_empty_or_oversized_source(text):
    with pytest.raises(ValueError):
        podcast_length_estimates(text)


def test_estimates_handle_unicode_and_unspaced_text():
    assert spoken_word_count("Přehled důležitých zpráv") == 3
    assert spoken_word_count("这是一个没有空格的中文段落") > 1


def test_relative_and_legacy_options_are_mutually_exclusive():
    with pytest.raises(ValueError, match="not both"):
        AudioJobCreate(
            content_ref="dlv_11111111111111111111111111111111",
            mode="podcast",
            length="default",
            target_minutes=6,
            idempotency_key="relative-and-explicit-budget",
        )

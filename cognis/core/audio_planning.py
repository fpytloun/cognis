"""Pure, source-aware podcast budgets shared by estimates and job admission."""

from __future__ import annotations

import math
import re

from cognis.core.speech_models import PodcastLength, PodcastLengthEstimate

LENGTH_FACTORS: tuple[tuple[PodcastLength, float], ...] = (
    ("shorter", 0.6),
    ("default", 1.0),
    ("longer", 1.5),
)


def spoken_word_count(text: str) -> int:
    """Count Unicode words, treating unspaced CJK text as approximate word pairs."""
    cjk = re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text)
    remainder = re.sub(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", " ", text)
    return len(re.findall(r"\w+", remainder)) + math.ceil(len(cjk) / 2)


def podcast_length_estimates(text: str) -> list[PodcastLengthEstimate]:
    """Plan without inference; short sources are not stretched to a fixed runtime."""
    if not text or len(text) > 120_000:
        raise ValueError("Document must contain between 1 and 120000 readable text characters.")
    words = spoken_word_count(text)
    if not words:
        raise ValueError("Document must contain readable words.")
    baseline = max(12, min(1600, round(words * 0.85)))
    source_ceiling = max(18, round(words * 1.1))
    return [
        PodcastLengthEstimate(
            length=length,
            target_words=(target := max(8, min(source_ceiling, round(baseline * factor)))),
            seconds_min=max(1, math.floor(target * 0.85 / 165 * 60)),
            seconds_max=math.ceil(max(40, target * 1.25) / 115 * 60),
        )
        for length, factor in LENGTH_FACTORS
    ]

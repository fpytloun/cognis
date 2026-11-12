"""Prove that phase evidence is retained without hiding any inference traffic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.usage_checkpoint.phases import assert_no_analytics_inference, retain_history


def test_keeps_complete_startup_payload_and_allows_only_zero_analytics_delta(
    tmp_path: Path,
) -> None:
    payload = {
        "history": [
            {"request": {"model": "fixture-memory-model", "messages": ["Synthetic identity"]}}
        ]
    }
    baseline = retain_history(tmp_path, "fixture", payload)
    assert json.loads((tmp_path / "mock-inference-fixture.json").read_text()) == payload
    assert_no_analytics_inference(baseline, list(baseline))


@pytest.mark.parametrize("current", [[{"request": "new"}], [], [{"request": "rewritten"}]])
def test_rejects_added_removed_and_rewritten_history_without_echoing_content(
    current: list[dict[str, str]],
) -> None:
    baseline = [{"request": "retained synthetic identity"}]
    with pytest.raises(AssertionError, match="Analytics changed full mock history") as error:
        assert_no_analytics_inference(baseline, current)
    assert "retained synthetic identity" not in str(error.value)


def test_preserves_saturated_history_but_refuses_to_claim_complete_phase_attribution(
    tmp_path: Path,
) -> None:
    payload = {"history": [{"request": {"ordinal": number}} for number in range(50)]}
    with pytest.raises(AssertionError, match="inconclusive"):
        retain_history(tmp_path, "fixture", payload)
    assert json.loads((tmp_path / "mock-inference-fixture.json").read_text()) == payload

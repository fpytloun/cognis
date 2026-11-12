"""Retain complete bounded mock histories and prove an inference-free analytics phase."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HISTORY_CAPACITY = 50


def retain_history(root: Path, phase: str, payload: dict[str, Any]) -> list[Any]:
    """Save the unfiltered response before validating its complete-history boundary."""
    (root / f"mock-inference-{phase}.json").write_text(json.dumps(payload, indent=2))
    history = payload.get("history")
    if not isinstance(history, list):
        raise AssertionError("The mock history response is not a complete list")
    if len(history) >= HISTORY_CAPACITY:
        raise AssertionError(
            "Mock history reached its retention bound; phase attribution is inconclusive"
        )
    return history


def assert_no_analytics_inference(baseline: list[Any], current: list[Any]) -> None:
    """Reject new, removed or rewritten entries without logging request contents."""
    if current != baseline:
        raise AssertionError(
            f"Analytics changed full mock history: baseline={len(baseline)}, current={len(current)}"
        )

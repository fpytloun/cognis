"""L2 golden event-stream tests.

For each scenario:
1. Inject the scenario into the mock-llm server.
2. Send the trigger message via WebSocket.
3. Capture the full WS event stream.
4. Assert backend-contract invariants (Python).
5. Write the captured stream to tests/e2e/golden/<scenario>.jsonl.

The golden files are then replayed by the vitest golden tests
(ui/src/lib/chat-timeline.golden.test.ts) to assert client-store invariants.

These tests require the e2e_stack fixture (live stack + mock-llm).
Mark with @pytest.mark.e2e to skip in normal CI.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import (
    GOLDEN_DIR,
    E2EStack,
    capture_ws_events,
    clear_active_scenario,
    inject_scenario,
)
from tests.e2e.invariants import check_all

# Scenarios to test — each maps to a scenario id and trigger message
SCENARIOS = [
    # Original baseline scenarios
    ("single-phase-stream", "scenario:single-phase-stream"),
    ("thinking-multiblock", "scenario:thinking-multiblock"),
    ("multiphase-thinking-tool-assistant", "scenario:multiphase-thinking-tool-assistant"),
    ("tool-args-then-result", "scenario:tool-args-then-result"),
    ("rapid-tokens", "scenario:rapid-tokens"),
    # Real-world inspired scenarios (from production session analysis)
    ("coding-session-multiphase", "scenario:coding-session-multiphase"),
    ("research-multiphase", "scenario:research-multiphase"),
    ("tool-error-recovery", "scenario:tool-error-recovery"),
    ("long-streaming-response", "scenario:long-streaming-response"),
    ("thinking-then-tools-then-answer", "scenario:thinking-then-tools-then-answer"),
    # Production-shaped multi-phase workflow (3 LLM calls, phase 0→1→2, thinking)
    ("prod-multiphase-workflow", "scenario:prod-multiphase-workflow"),
    # Reconnect re-injection bug: thinking + tool_calls + text, then reconnect
    # captures a post-turn conversation_runtime_snapshot with has_active_turn:false.
    # INV-RECONNECT-NO-HANG asserts no streaming items survive the reconnect.
    ("reconnect-stale-thinking", "scenario:reconnect-stale-thinking"),
    # Phase-order ordering bugs (Bug 1 + Bug 2):
    # Bug 1: completion item's real seq jumps it above sentinel-seq earlier-phase siblings.
    # Bug 2: phase-1 thinking after a finalized phase-0 assistant (missing canonical_items).
    # INV-PHASE-ORDER asserts items are ordered by (phase, kind_rank) within a turn.
    ("multiphase-completion-ordering", "scenario:multiphase-completion-ordering"),
    # Duplicate thinking block / stuck progress bug:
    # Two thinking blocks in one segment; first block completes and is popped from
    # session_cache. Without the first_block_id anchor, the id shifts → orphan + duplicate.
    # INV-NO-DUP + INV-FINAL-PRESENCE + INV-NO-HANG assert no duplicate, no disappear, no hang.
    ("thinking-multiblock-complete", "scenario:thinking-multiblock-complete"),
    # Tool-output progress ordering: thinking + tool_call + final text.
    # INV-PHASE-ORDER asserts tool_call sorts correctly relative to thinking and assistant.
    # Validates the canonical_items fix for on_tool_progress/on_tool_output_chunk.
    ("tool-output-progress", "scenario:tool-output-progress"),
]

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def clear_scenario_after(e2e_stack: E2EStack) -> Any:
    """Clear the active scenario after each test."""
    yield
    with contextlib.suppress(Exception):
        clear_active_scenario(e2e_stack.mock_llm_url)


def _write_golden(scenario_id: str, events: list[dict[str, Any]]) -> Path:
    """Write captured events to a golden file."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden_path = GOLDEN_DIR / f"{scenario_id}.jsonl"
    with golden_path.open("w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return golden_path


@pytest.mark.parametrize("scenario_id,trigger", SCENARIOS)
def test_scenario_stream_invariants(
    e2e_stack: E2EStack,
    scenario_id: str,
    trigger: str,
) -> None:
    """Capture a scenario stream and assert backend-contract invariants."""
    if e2e_stack.e2e_conversation_id is None:
        pytest.skip("No e2e conversation available")

    # Inject the scenario
    inject_scenario(e2e_stack.mock_llm_url, scenario_id)

    # Send trigger message and capture events
    events = capture_ws_events(
        e2e_stack,
        e2e_stack.e2e_conversation_id,
        trigger,
        timeout=90,
    )

    assert len(events) > 0, f"No events received for scenario {scenario_id!r}"

    # Check for error events
    error_events = [e for e in events if e.get("type") == "error"]
    assert not error_events, (
        f"Error events received for scenario {scenario_id!r}: "
        + json.dumps(error_events, indent=2)
    )

    # Assert backend-contract invariants
    violations = check_all(events)
    assert not violations, (
        f"Invariant violations for scenario {scenario_id!r}:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )

    # Write golden file for vitest replay
    golden_path = _write_golden(scenario_id, events)
    print(f"Written golden: {golden_path} ({len(events)} events)")

    # Assert message_complete was received
    assert any(e.get("type") == "message_complete" for e in events), (
        f"No message_complete event received for scenario {scenario_id!r}"
    )


def test_mock_llm_health(e2e_stack: E2EStack) -> None:
    """Verify the mock-llm server is healthy and has scenarios loaded."""
    import httpx
    resp = httpx.get(f"{e2e_stack.mock_llm_url}/health", timeout=5.0)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["scenarios"] >= len(SCENARIOS)


def test_mock_llm_control_plane(e2e_stack: E2EStack) -> None:
    """Verify the control-plane endpoints work."""
    import httpx

    # List scenarios
    resp = httpx.get(f"{e2e_stack.mock_llm_url}/__mock/scenarios", timeout=5.0)
    assert resp.status_code == 200
    scenarios = resp.json()["scenarios"]
    assert len(scenarios) >= len(SCENARIOS)

    # Set active scenario
    resp = httpx.post(
        f"{e2e_stack.mock_llm_url}/__mock/active",
        json={"id": "single-phase-stream"},
        timeout=5.0,
    )
    assert resp.status_code == 200
    assert resp.json()["active"] == "single-phase-stream"

    # Clear active
    resp = httpx.post(
        f"{e2e_stack.mock_llm_url}/__mock/active",
        json={"id": None},
        timeout=5.0,
    )
    assert resp.status_code == 200

    # Inject a custom scenario
    custom = {
        "id": "custom-test",
        "description": "Custom test scenario",
        "trigger": "scenario:custom-test",
        "turns": [{"role": "assistant", "steps": [
            {"type": "text", "chunks": ["Custom ", "response."], "delay_ms": 10}
        ]}],
    }
    resp = httpx.post(
        f"{e2e_stack.mock_llm_url}/__mock/scenario",
        json=custom,
        timeout=5.0,
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "custom-test"

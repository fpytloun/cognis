"""L2 scoped ChatV2 event capture tests.

For each scenario:
1. Inject the scenario into the mock-llm server.
2. Send the trigger message via WebSocket.
3. Capture the full WS event stream.
 4. Persist the captured stream for canonical ChatV2 diagnostics.
5. Write the captured stream to tests/e2e/golden/<scenario>.jsonl.

Client ordering and reconciliation invariants are covered by the canonical
ui/src/lib/chat-v2/sync-engine.invariants.test.ts suite.

These tests require the e2e_stack fixture (live stack + mock-llm).
Mark with @pytest.mark.e2e to skip in normal CI.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import (
    CANONICAL_CAPTURE_DIR,
    GOLDEN_DIR,
    E2EStack,
    _assert_reset_recovery_snapshot,
    capture_scoped_scope_events,
    capture_ws_events,
    clear_active_scenario,
    inject_scenario,
)

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


def _write_canonical_capture(scenario_id: str, events: list[dict[str, Any]]) -> Path | None:
    """Promote canonical ChatV2 records emitted by the live WS stream.

    Raw lifecycle events remain in ``tests/e2e/golden``.  Only records carrying
    the canonical ChatV2 contract are copied to the UI replay corpus, so the
    replay target never silently starts depending on legacy transport events.
    """
    canonical = [
        event
        for event in events
        if event.get("type") in {"snapshot", "sync", "frame", "chat_v2_frame", "reconnect"}
    ]
    if not any(event.get("type") in {"frame", "chat_v2_frame"} for event in canonical):
        return None
    CANONICAL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    capture_path = CANONICAL_CAPTURE_DIR / f"promoted-{scenario_id}.jsonl"
    with capture_path.open("w") as f:
        for event in canonical:
            f.write(json.dumps(event) + "\n")
    return capture_path


def _write_scoped_capture(name: str, events: list[dict[str, Any]]) -> Path:
    """Promote a live scoped stream to the canonical UI replay corpus."""
    path = CANONICAL_CAPTURE_DIR / f"promoted-{name}.jsonl"
    CANONICAL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    replayable_types = {"snapshot", "sync", "frame", "chat_v2_frame", "reconnect"}
    canonical = [event for event in events if event.get("type") in replayable_types]
    assert any(event.get("type") == "chat_v2_frame" for event in canonical), name
    assert any(
        event.get("type") == "sync" and event.get("reset_required") for event in canonical
    ), name
    assert len([event for event in canonical if event.get("type") == "snapshot"]) >= 2, name
    reset_index = next(
        index
        for index, event in enumerate(canonical)
        if event.get("type") == "sync" and event.get("reset_required")
    )
    assert canonical[reset_index + 1].get("type") == "snapshot", name
    pre_reset = next(
        event for event in reversed(canonical[:reset_index]) if event.get("type") == "snapshot"
    )
    _assert_reset_recovery_snapshot(
        pre_reset=pre_reset,
        reset=canonical[reset_index],
        recovery=canonical[reset_index + 1],
    )
    with path.open("w") as capture:
        for event in canonical:
            capture.write(json.dumps(event) + "\n")
    return path


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
    assert not error_events, f"Error events received for scenario {scenario_id!r}: " + json.dumps(
        error_events, indent=2
    )

    # Persist the capture for scoped ChatV2 diagnostics.
    golden_path = _write_golden(scenario_id, events)
    print(f"Written golden: {golden_path} ({len(events)} events)")
    canonical_path = _write_canonical_capture(scenario_id, events)
    if canonical_path:
        print(f"Promoted canonical capture: {canonical_path}")

    # Assert message_complete was received
    assert any(e.get("type") == "message_complete" for e in events), (
        f"No message_complete event received for scenario {scenario_id!r}"
    )

    chat_v2_frames = [event for event in events if event.get("type") == "chat_v2_frame"]
    assert chat_v2_frames, f"No ChatV2 frames captured for scenario {scenario_id!r}"
    expected_scope_key = f"conversation:{e2e_stack.e2e_conversation_id}"
    cursor = chat_v2_frames[0]["cursor_before"]
    for frame in chat_v2_frames:
        assert frame["scope"]["key"] == expected_scope_key
        assert frame["scope"]["kind"] == "conversation"
        assert frame["scope"]["conversation_id"] == e2e_stack.e2e_conversation_id
        assert frame["conversation_id"] == e2e_stack.e2e_conversation_id
        assert frame["cursor_before"] == cursor
        cursor = frame["cursor_after"]


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
        "turns": [
            {
                "role": "assistant",
                "steps": [{"type": "text", "chunks": ["Custom ", "response."], "delay_ms": 10}],
            }
        ],
    }
    resp = httpx.post(
        f"{e2e_stack.mock_llm_url}/__mock/scenario",
        json=custom,
        timeout=5.0,
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "custom-test"


def test_live_scoped_capture_promotes_and_replays(
    e2e_stack: E2EStack,
) -> None:
    """Generate session/task-step captures from linked backend resources.

    This deliberately runs the frontend replay command immediately after
    promotion so a capture cannot pass merely because it was written to disk.
    """
    task_scenario = {
        "id": "live-task-step-capture",
        "description": "Completes the workflow step through the native controller tool.",
        "trigger": "scenario:live-task-step-capture",
        "turns": [
            {
                "role": "assistant",
                "steps": [
                    {"type": "text", "chunks": ["Starting lifecycle capture."], "delay_ms": 3000},
                    {
                        # The E2E executor exposes a real bounded bash tool.
                        # Sleeping keeps the tool in-progress long enough for
                        # native scoped snapshots to record start and result.
                        "type": "tool_call",
                        "call_id": "live-running-tool",
                        "name": "bash",
                        "arguments": {
                            "command": "sleep 2; printf lifecycle-complete",
                            "workdir": "/tmp",
                            "description": "Exercise live scoped tool lifecycle.",
                            "timeout": 5000,
                        },
                        "delay_ms": 500,
                    },
                ],
            },
            {
                "role": "assistant",
                "steps": [
                    {"type": "text", "chunks": ["Task output."], "delay_ms": 1000},
                    {
                        "type": "tool_call",
                        "call_id": "live-step-complete",
                        "name": "step_complete",
                        "arguments": {"summary": "Captured live task step output."},
                        "delay_ms": 500,
                    },
                ],
            },
        ],
    }
    scenario_response = e2e_stack.http.post(
        f"{e2e_stack.mock_llm_url}/__mock/scenario",
        json=task_scenario,
        timeout=5.0,
    )
    assert scenario_response.status_code == 200, scenario_response.text
    inject_scenario(e2e_stack.mock_llm_url, "live-task-step-capture")
    created = e2e_stack.post(
        "/api/v1/tasks",
        json={
            "agent_id": e2e_stack.e2e_agent_id,
            "title": "Live ChatV2 scoped capture",
            "description": "Produce one deterministic answer.",
            "workflow_id": "system:direct",
            "delivery_mode": "silent",
            "status": "draft",
        },
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    submitted = e2e_stack.post(f"/api/v1/tasks/{task_id}/submit")
    assert submitted.status_code == 200, submitted.text

    # Discover the native scope while the task is still executing.  Waiting
    # for terminal status before opening the scoped stream only produces a
    # post-hoc snapshot and misses tool-start/runtime transitions.
    deadline = time.monotonic() + 30
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = e2e_stack.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        step_runs = detail.get("step_runs") or []
        if step_runs and step_runs[-1].get("session_id") and step_runs[-1].get("conversation_id"):
            break
        time.sleep(1)

    step = next(
        (
            row
            for row in reversed(detail.get("step_runs") or [])
            if row.get("session_id") and row.get("conversation_id")
        ),
        None,
    )
    assert step is not None, f"task did not create a linked step session: {detail}"
    conversation_scope = {
        "kind": "conversation",
        "conversation_id": step["conversation_id"],
    }
    session_scope = {
        "kind": "session",
        "key": f"session:{step['session_id']}",
        "session_id": step["session_id"],
        "conversation_id": step["conversation_id"],
    }
    task_step_scope = {
        "kind": "task_step",
        "key": f"task_step:{step['step_run_id']}",
        "task_id": task_id,
        "step_run_id": step["step_run_id"],
        "session_id": step["session_id"],
        "conversation_id": step["conversation_id"],
    }
    scopes = {
        "live-conversation": conversation_scope,
        "live-session": session_scope,
        "live-task-step": task_step_scope,
    }
    # Capture all native streams concurrently. Sequential capture allowed the
    # task to complete before the session/task-step sockets were attached,
    # producing terminal snapshots plus empty subscription frames.
    with ThreadPoolExecutor(max_workers=len(scopes)) as executor:
        futures = {
            name: executor.submit(
                capture_scoped_scope_events,
                e2e_stack,
                scope,
                timeout=12,
            )
            for name, scope in scopes.items()
        }
        captures = {name: future.result() for name, future in futures.items()}

    for name, events in captures.items():
        assert events and events[0]["type"] == "snapshot"
        assert any(event["type"] in {"frame", "chat_v2_frame"} for event in events)
        initial_status = events[0]["scope"]["status"]
        assert initial_status not in {"completed", "approved", "failed", "cancelled"}, (
            name,
            initial_status,
        )
        final_snapshot = next(event for event in reversed(events) if event["type"] == "snapshot")
        item_kinds = {item["kind"] for item in final_snapshot["timeline"]["items"]}
        assert {"tool_call", "message"} <= item_kinds, (name, item_kinds)
        frame_cursors = [
            (event["cursor_before"], event["cursor_after"])
            for event in events
            if event["type"] == "chat_v2_frame"
        ]
        assert frame_cursors and all(before and after for before, after in frame_cursors)
        assert any(event["type"] == "reconnect" for event in events)
        tool_transitions = {
            (item["tool_name"], item["status"])
            for event in events
            if event["type"] == "snapshot"
            for item in event["timeline"]["items"]
            if item["kind"] == "tool_call"
        }
        assert ("bash", "running") in tool_transitions, (name, tool_transitions)
        assert ("bash", "complete") in tool_transitions, (name, tool_transitions)
        assert any(event["type"] == "sync" and event["reset_required"] for event in events)
        _write_scoped_capture(name, events)

    # The producer captures the terminal lifecycle only after the native
    # streams have been attached; this keeps the live capture bounded while
    # ensuring the task itself owns every captured record.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = e2e_stack.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200, response.text
        if response.json().get("status") in {"completed", "failed", "cancelled", "paused"}:
            break
        time.sleep(1)

    result = subprocess.run(
        ["make", "e2e-events-replay"],
        cwd=Path(__file__).parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

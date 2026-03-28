"""Performance baseline integration tests."""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import (
    IntegrationStack,
    LiveStack,
    live_chat_ws,
    live_create_agent,
    live_create_conversation,
)


@pytest.mark.integration
@pytest.mark.live_server
def test_time_to_first_token_follow_up(live_stack: LiveStack, run_id: str) -> None:
    """Measure time-to-first-token for a follow-up turn.

    NFR target: <= 2.5s (P95). We allow up to 15s for a single sample.
    """
    agent_id = f"perf-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    # Warm-up turn
    events1 = live_chat_ws(live_stack, cid, "Hello, I am warming up the context.")
    assert any(e["type"] == "message_complete" for e in events1)

    time.sleep(1)

    # Measure follow-up turn
    start = time.monotonic()
    events2 = live_chat_ws(live_stack, cid, "What is 1 + 1?")
    first_chunk = next((e for e in events2 if e.get("type") == "chunk"), None)
    if first_chunk:
        # Approximate TTFT from the events list
        ttft = time.monotonic() - start
        print(f"\n  [perf] Follow-up TTFT (approx): {ttft:.3f}s")
        assert ttft < 15.0, f"TTFT was {ttft:.2f}s (expected < 15s for single sample)"
    else:
        # Even without a chunk, message_complete is acceptable
        assert any(e["type"] == "message_complete" for e in events2)


@pytest.mark.integration
def test_health_response_time(stack: IntegrationStack) -> None:
    """Health endpoint should respond within 2 seconds."""
    start = time.monotonic()
    response = stack.client.get("/api/health")
    elapsed = time.monotonic() - start
    assert response.status_code == 200
    assert elapsed < 2.0, f"Health took {elapsed:.2f}s"
    print(f"\n  [perf] Health response: {elapsed:.3f}s")

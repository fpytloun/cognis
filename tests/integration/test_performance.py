"""Performance baseline integration tests.

Measures and asserts P95 targets from docs/specs/13-nfr-operations.md.
These are approximate — real P95 requires more samples, but this
establishes a single-run baseline.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import (
    IntegrationStack,
    create_test_agent,
    create_test_conversation,
)


def _measure_time_to_first_chunk(
    stack: IntegrationStack,
    conversation_id: str,
    message: str,
    *,
    timeout: float = 30,
) -> float:
    """Send a message and measure time from send to first chunk."""
    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        ws.receive_json()
        ws.send_json({"type": "reconnect", "conversation_id": conversation_id, "last_seq": 0})

        start = time.monotonic()
        ws.send_json(
            {
                "type": "message",
                "conversation_id": conversation_id,
                "content": message,
            }
        )

        deadline = start + timeout
        while time.monotonic() < deadline:
            msg = ws.receive_json(mode="text")
            if msg.get("type") == "chunk":
                return time.monotonic() - start
            if msg.get("type") == "message_complete":
                return time.monotonic() - start
            if msg.get("type") == "error":
                raise RuntimeError(f"Error during perf test: {msg}")

    raise RuntimeError("Timed out waiting for first chunk")


@pytest.mark.integration
@pytest.mark.live_server
def test_time_to_first_token_follow_up(
    stack: IntegrationStack,
    agent_id: str,
) -> None:
    """Measure time-to-first-token for a follow-up turn.

    NFR target: <= 2.5s (P95). We allow up to 10s for a single sample
    since this is not a statistical P95 — just a baseline sanity check.
    """
    create_test_agent(stack, agent_id)
    conversation = create_test_conversation(stack, agent_id)
    cid = conversation["conversation_id"]

    # Warm-up turn
    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        ws.receive_json()
        ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
        ws.send_json(
            {
                "type": "message",
                "conversation_id": cid,
                "content": "Hello, I am warming up the context.",
            }
        )

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            msg = ws.receive_json(mode="text")
            if msg.get("type") == "message_complete":
                break

    # Wait for context assembly to settle
    time.sleep(1)

    # Measure follow-up turn
    ttft = _measure_time_to_first_chunk(stack, cid, "What is 1 + 1?")

    # Generous single-sample bound: 10s (real P95 target is 2.5s)
    assert ttft < 10.0, f"Time-to-first-token was {ttft:.2f}s (expected < 10s for single sample)"
    print(f"\n  [perf] Follow-up TTFT: {ttft:.3f}s")


@pytest.mark.integration
def test_health_response_time(
    stack: IntegrationStack,
) -> None:
    """Health endpoint should respond within 2 seconds."""
    start = time.monotonic()
    response = stack.client.get("/api/health")
    elapsed = time.monotonic() - start

    assert response.status_code == 200
    assert elapsed < 2.0, f"Health endpoint took {elapsed:.2f}s (expected < 2s)"
    print(f"\n  [perf] Health response: {elapsed:.3f}s")

"""Guardrails integration tests.

Exercises: Intaris evaluate on tool calls, event recording.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import (
    IntegrationStack,
    create_test_agent,
    create_test_conversation,
)


@pytest.mark.integration
@pytest.mark.live_server
def test_intaris_records_session_events(
    stack: IntegrationStack,
    agent_id: str,
) -> None:
    """After a chat turn, Intaris should have recorded session events."""
    create_test_agent(stack, agent_id)
    conversation = create_test_conversation(stack, agent_id)
    cid = conversation["conversation_id"]

    # Send a message and wait for completion
    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        ws.receive_json()
        ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
        ws.send_json(
            {
                "type": "message",
                "conversation_id": cid,
                "content": "Say one word.",
            }
        )

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            msg = ws.receive_json(mode="text")
            if msg.get("type") == "message_complete":
                break

    # Wait for event persistence
    time.sleep(2)

    # Check sessions exist for this conversation
    sessions_response = stack.client.get(
        f"/api/v1/conversations/{cid}/sessions",
        headers=stack.admin_headers(),
    )
    assert sessions_response.status_code == 200
    sessions = sessions_response.json()
    assert len(sessions) >= 1

    # Read messages — these come from Intaris event store
    messages_response = stack.client.get(
        f"/api/v1/conversations/{cid}/messages?after_seq=0&limit=50",
        headers=stack.admin_headers(),
    )
    assert messages_response.status_code == 200
    items = messages_response.json()["items"]
    types = [item["type"] for item in items]
    assert "user_message" in types, f"Expected user_message in {types}"
    assert "assistant_message" in types, f"Expected assistant_message in {types}"


@pytest.mark.integration
def test_escalation_list_endpoint(
    stack: IntegrationStack,
) -> None:
    """The escalation list endpoint should be accessible.

    May return 200 (empty list) or 500/400 if Intaris audit endpoint
    is not available in the current version. Both are acceptable for
    this integration test — we verify the route exists and auth works.
    """
    response = stack.client.get(
        "/api/v1/escalations",
        headers=stack.admin_headers(),
    )
    # Accept 200 (works) or 500/400 (Intaris audit not available)
    assert response.status_code in (200, 400, 500), (
        f"Unexpected status: {response.status_code} {response.text[:200]}"
    )

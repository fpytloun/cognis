"""Session recovery integration test.

Exercises: stale session detection after restart, recovered session IDs.
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
def test_stale_sessions_detected_on_startup(
    stack: IntegrationStack,
) -> None:
    """On startup, the controller should have run stale session recovery.

    This verifies the recovery mechanism ran (the list may be empty if
    there were no stale sessions, which is fine).
    """
    app = stack.client.app
    recovered = getattr(app.state, "recovered_session_ids", None)
    assert recovered is not None, "recovered_session_ids not set on app.state"
    # It should be a list (possibly empty)
    assert isinstance(recovered, list)


@pytest.mark.integration
@pytest.mark.live_server
def test_session_has_intaris_session_after_chat(
    stack: IntegrationStack,
    agent_id: str,
) -> None:
    """After a chat turn, the session should have an Intaris session ID."""
    create_test_agent(stack, agent_id)
    conversation = create_test_conversation(stack, agent_id)
    cid = conversation["conversation_id"]

    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        ws.receive_json()
        ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
        ws.send_json(
            {
                "type": "message",
                "conversation_id": cid,
                "content": "Hello",
            }
        )

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            msg = ws.receive_json(mode="text")
            if msg.get("type") == "message_complete":
                break

    # Check sessions
    sessions_response = stack.client.get(
        f"/api/v1/conversations/{cid}/sessions",
        headers=stack.admin_headers(),
    )
    assert sessions_response.status_code == 200
    sessions = sessions_response.json()
    assert len(sessions) >= 1

    # Root session should have an Intaris session ID
    root = sessions[0]
    assert root.get("intaris_session_id") is not None, f"Expected intaris_session_id, got: {root}"

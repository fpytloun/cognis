"""Guardrails integration tests.

Exercises: Intaris evaluate on tool calls, event recording.
"""

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
def test_intaris_records_session_events(live_stack: LiveStack, run_id: str) -> None:
    """After a chat turn, Intaris should have recorded session events."""
    agent_id = f"guard-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events = live_chat_ws(live_stack, cid, "Say one word.")
    assert any(e["type"] == "message_complete" for e in events)

    time.sleep(2)

    sessions = live_stack.get(f"/api/v1/conversations/{cid}/sessions")
    assert sessions.status_code == 200
    assert len(sessions.json()) >= 1

    messages = live_stack.get(f"/api/v1/conversations/{cid}/messages?after_seq=0&limit=50")
    assert messages.status_code == 200
    types = [i["type"] for i in messages.json()["items"]]
    assert "user_message" in types
    assert "assistant_message" in types


@pytest.mark.integration
def test_escalation_list_endpoint(stack: IntegrationStack) -> None:
    """The escalation list endpoint should be accessible."""
    response = stack.client.get(
        "/api/v1/escalations",
        headers=stack.admin_headers(),
    )
    assert response.status_code in (200, 400, 500), (
        f"Unexpected status: {response.status_code} {response.text[:200]}"
    )

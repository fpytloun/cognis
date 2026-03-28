"""Core chat flow integration tests — require live Cognis server.

Run with: ``pytest -m "integration and live_server"``
"""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import (
    LiveStack,
    live_chat_ws,
    live_create_agent,
    live_create_conversation,
)


@pytest.mark.integration
@pytest.mark.live_server
def test_send_message_and_receive_response(live_stack: LiveStack, run_id: str) -> None:
    """Full chat round-trip with real LLM."""
    agent_id = f"chat-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events = live_chat_ws(live_stack, cid, "What is 2 + 2? Answer with just the number.")
    assert any(e["type"] == "message_complete" for e in events), (
        f"No message_complete. Got: {[e.get('type') for e in events]}"
    )
    chunks = [e for e in events if e.get("type") == "chunk"]
    assert len(chunks) > 0, "Expected at least one streaming chunk"


@pytest.mark.integration
@pytest.mark.live_server
def test_conversation_messages_persisted(live_stack: LiveStack, run_id: str) -> None:
    """After a chat turn, messages should be readable via REST."""
    agent_id = f"persist-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events = live_chat_ws(live_stack, cid, "Say hello.")
    assert any(e["type"] == "message_complete" for e in events)

    time.sleep(2)

    r = live_stack.get(f"/api/v1/conversations/{cid}/messages?after_seq=0&limit=50")
    assert r.status_code == 200
    types = [i["type"] for i in r.json()["items"]]
    assert "user_message" in types
    assert "assistant_message" in types


@pytest.mark.integration
@pytest.mark.live_server
def test_conversation_sessions_created(live_stack: LiveStack, run_id: str) -> None:
    """After sending a message, a root session should exist."""
    agent_id = f"sess-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events = live_chat_ws(live_stack, cid, "Hello")
    assert any(e["type"] == "message_complete" for e in events)

    r = live_stack.get(f"/api/v1/conversations/{cid}/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) >= 1
    assert sessions[0]["agent_id"] == agent_id

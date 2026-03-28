"""Core chat flow integration tests.

These tests require a live Cognis server for WebSocket + LLM flows.
Run with: ``pytest -m "integration and live_server"``
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
def test_send_message_and_receive_response(stack: IntegrationStack, agent_id: str) -> None:
    create_test_agent(stack, agent_id)
    conversation = create_test_conversation(stack, agent_id)
    cid = conversation["conversation_id"]
    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        assert ws.receive_json()["type"] == "authenticated"
        ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
        ws.send_json({"type": "message", "conversation_id": cid, "content": "Say hello."})
        events: list[dict] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            msg = ws.receive_json(mode="text")
            events.append(msg)
            if msg.get("type") == "message_complete":
                break
        assert any(e["type"] == "message_complete" for e in events)


@pytest.mark.integration
@pytest.mark.live_server
def test_conversation_messages_persisted(stack: IntegrationStack, agent_id: str) -> None:
    create_test_agent(stack, agent_id)
    conv = create_test_conversation(stack, agent_id)
    cid = conv["conversation_id"]
    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        ws.receive_json()
        ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
        ws.send_json({"type": "message", "conversation_id": cid, "content": "Hi"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if ws.receive_json(mode="text").get("type") == "message_complete":
                break
    time.sleep(1)
    r = stack.client.get(
        f"/api/v1/conversations/{cid}/messages?after_seq=0&limit=50", headers=stack.admin_headers()
    )
    assert r.status_code == 200
    types = [i["type"] for i in r.json()["items"]]
    assert "user_message" in types and "assistant_message" in types

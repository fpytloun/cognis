"""Memory integration tests. Require a live Cognis server for WS flows."""

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
def test_multi_turn_recall(stack: IntegrationStack, agent_id: str) -> None:
    create_test_agent(stack, agent_id)
    conv = create_test_conversation(stack, agent_id)
    cid = conv["conversation_id"]

    def _send(content: str) -> list[dict]:
        events: list[dict] = []
        with stack.client.websocket_connect("/api/ws") as ws:
            ws.send_json({"type": "auth", "token": stack.admin_token})
            ws.receive_json()
            ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
            ws.send_json({"type": "message", "conversation_id": cid, "content": content})
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                msg = ws.receive_json(mode="text")
                events.append(msg)
                if msg.get("type") == "message_complete":
                    break
        return events

    _send("My favorite color is cerulean blue. Remember this.")
    time.sleep(3)
    events2 = _send("What is my favorite color?")
    chunks = "".join(e["content"] for e in events2 if e.get("type") == "chunk").lower()
    assert "cerulean" in chunks or "blue" in chunks


@pytest.mark.integration
@pytest.mark.live_server
def test_agent_personality_bootstrap(stack: IntegrationStack, agent_id: str) -> None:
    create_test_agent(stack, agent_id, system_prompt="You are a pirate captain. Always say Ahoy!")
    conv = create_test_conversation(stack, agent_id)
    cid = conv["conversation_id"]
    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        ws.receive_json()
        ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
        ws.send_json({"type": "message", "conversation_id": cid, "content": "Greet me."})
        events: list[dict] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            msg = ws.receive_json(mode="text")
            events.append(msg)
            if msg.get("type") == "message_complete":
                break
    chunks = "".join(e["content"] for e in events if e.get("type") == "chunk").lower()
    assert "ahoy" in chunks or "pirate" in chunks or "captain" in chunks

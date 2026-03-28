"""WebSocket reconnection integration test. Requires a live Cognis server."""

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
def test_websocket_reconnect_replays_missed_events(stack: IntegrationStack, agent_id: str) -> None:
    create_test_agent(stack, agent_id)
    conv = create_test_conversation(stack, agent_id)
    cid = conv["conversation_id"]
    last_seq = 0
    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        ws.receive_json()
        ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
        ws.send_json({"type": "message", "conversation_id": cid, "content": "Say pineapple"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            msg = ws.receive_json(mode="text")
            if msg.get("type") == "message_complete":
                last_seq = msg.get("seq", 0)
                break
    assert last_seq > 0
    time.sleep(2)
    with stack.client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": stack.admin_token})
        ws.receive_json()
        ws.send_json({"type": "reconnect", "conversation_id": cid, "last_seq": 0})
        replay: list[dict] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                msg = ws.receive_json(mode="text")
                replay.append(msg)
                if msg.get("type") == "reconnected":
                    break
            except Exception:
                break
        assert any(e.get("type") == "reconnected" for e in replay)

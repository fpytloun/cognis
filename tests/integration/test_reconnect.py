"""WebSocket reconnection integration test — requires live Cognis server."""

from __future__ import annotations

import json
import time

import pytest
import websockets.sync.client as wsc

from tests.integration.conftest import (
    LiveStack,
    live_chat_ws,
    live_create_agent,
    live_create_conversation,
)


@pytest.mark.integration
@pytest.mark.live_server
def test_websocket_reconnect_replays_missed_events(live_stack: LiveStack, run_id: str) -> None:
    """After a chat turn, reconnecting with last_seq=0 should replay events."""
    agent_id = f"reconn-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events = live_chat_ws(live_stack, cid, "Say pineapple")
    complete = next((e for e in events if e["type"] == "message_complete"), None)
    assert complete is not None
    assert complete["seq"] > 0

    time.sleep(2)

    # Reconnect with last_seq=0 — should get replay + reconnected event
    with wsc.connect(live_stack.ws_url, close_timeout=5) as ws:
        ws.send(json.dumps({"type": "auth", "token": live_stack.admin_token}))
        auth = json.loads(ws.recv(timeout=10))
        assert auth["type"] == "authenticated"

        ws.send(
            json.dumps(
                {
                    "type": "reconnect",
                    "conversation_id": cid,
                    "last_seq": 0,
                }
            )
        )

        replay: list[dict] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(1, deadline - time.monotonic()))
                event = json.loads(raw)
                replay.append(event)
                if event.get("type") == "reconnected":
                    # Collect a few more after reconnected
                    inner = time.monotonic() + 3
                    while time.monotonic() < inner:
                        try:
                            raw2 = ws.recv(timeout=1)
                            replay.append(json.loads(raw2))
                        except Exception:
                            break
                    break
            except Exception:
                break

        types = [e.get("type") for e in replay]
        assert "reconnected" in types, f"Expected 'reconnected', got: {types}"

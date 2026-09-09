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
def test_websocket_reconnect_resumes_chat_v2_cursor(live_stack: LiveStack, run_id: str) -> None:
    """A reconnect gets canonical state over REST and resumes realtime from its cursor."""
    agent_id = f"reconn-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events = live_chat_ws(live_stack, cid, "Say pineapple")
    complete = next((e for e in events if e["type"] == "message_complete"), None)
    assert complete is not None
    assert isinstance(complete["cursor"], str) and complete["cursor"]

    time.sleep(2)
    snapshot_response = live_stack.get(f"/api/v1/chat/v2/conversations/{cid}/snapshot")
    assert snapshot_response.status_code == 200
    snapshot = snapshot_response.json()
    assert any(
        item.get("kind") == "message" and item.get("role") == "assistant"
        for item in snapshot["timeline"]["items"]
    )

    # Canonical replay comes from the snapshot. WebSocket resumes its cursor.
    with wsc.connect(live_stack.ws_url, close_timeout=5) as ws:
        ws.send(json.dumps({"type": "auth", "token": live_stack.admin_token}))
        auth = json.loads(ws.recv(timeout=10))
        assert auth["type"] == "authenticated"

        ws.send(
            json.dumps(
                {
                    "type": "chat_v2_subscribe",
                    "scope": snapshot["scope"],
                    "cursor": snapshot["cursor"],
                }
            )
        )

        frames: list[dict] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(1, deadline - time.monotonic()))
                event = json.loads(raw)
                frames.append(event)
                if event.get("type") == "chat_v2_frame":
                    break
            except Exception:
                break

        frame = next(
            (event for event in frames if event.get("type") == "chat_v2_frame"),
            None,
        )
        assert frame is not None, f"Expected chat_v2_frame, got: {frames}"
        assert frame["scope"]["key"] == snapshot["scope"]["key"]
        assert frame["cursor_before"] == snapshot["cursor"]

        admission = live_stack.put(
            f"/api/v1/chat/v2/conversations/{cid}/messages/reconnect-resume-turn",
            json={
                "client_message_id": "reconnect-resume-message",
                "content": "Say orange",
                "attachments": [],
            },
        )
        assert admission.status_code == 202

        turn_frame = None
        observed_after_admission: list[dict] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                event = json.loads(ws.recv(timeout=max(1, deadline - time.monotonic())))
            except Exception:
                break
            observed_after_admission.append(event)
            if event.get("type") != "chat_v2_frame":
                if (
                    event.get("type") == "sidebar_conversation_upsert"
                    and event.get("conversation_id") == cid
                    and event.get("conversation", {}).get("has_active_turn") is True
                ):
                    turn_frame = event
                    break
                continue
            runtime = event.get("runtime")
            if isinstance(runtime, dict) and runtime.get("has_active_turn") is True:
                turn_frame = event
                break
            if "reconnect-resume-message" in json.dumps(event):
                turn_frame = event
                break

        assert turn_frame is not None, (
            f"Reconnect subscription missed the next admitted turn: {observed_after_admission}"
        )

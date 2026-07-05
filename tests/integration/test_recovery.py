"""Session recovery integration tests."""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    IntegrationStack,
    LiveStack,
    live_chat_ws,
    live_create_agent,
    live_create_conversation,
)


@pytest.mark.integration
def test_stale_sessions_detected_on_startup(stack: IntegrationStack) -> None:
    """On startup, the controller should have run stale session recovery."""
    app = stack.client.app
    recovered = getattr(app.state, "recovered_session_ids", None)
    assert recovered is not None
    assert isinstance(recovered, (list, frozenset, set, tuple))


@pytest.mark.integration
@pytest.mark.live_server
def test_session_has_intaris_session_after_chat(live_stack: LiveStack, run_id: str) -> None:
    """After a chat turn, the session should have an Intaris session ID."""
    agent_id = f"recov-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events = live_chat_ws(live_stack, cid, "Hello")
    assert any(e["type"] == "message_complete" for e in events)

    r = live_stack.get(f"/api/v1/conversations/{cid}/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) >= 1
    assert sessions[0].get("intaris_session_id") is not None

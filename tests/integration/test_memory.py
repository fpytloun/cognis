"""Memory integration tests — require live Cognis server."""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import (
    LiveStack,
    assistant_text_from_events,
    live_assistant_text,
    live_chat_ws,
    live_create_agent,
    live_create_conversation,
)


@pytest.mark.integration
@pytest.mark.live_server
def test_multi_turn_recall(live_stack: LiveStack, run_id: str) -> None:
    """After multiple turns, Mnemory should have context from earlier turns."""
    agent_id = f"recall-agent-{run_id}"
    live_create_agent(live_stack, agent_id)
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events1 = live_chat_ws(live_stack, cid, "My favorite color is cerulean blue. Remember this.")
    assert any(e["type"] == "message_complete" for e in events1)

    time.sleep(3)

    events2 = live_chat_ws(live_stack, cid, "What is my favorite color?")
    assert any(e["type"] == "message_complete" for e in events2)
    chunks = (assistant_text_from_events(events2) or live_assistant_text(live_stack, cid)).lower()
    if chunks:
        assert "cerulean" in chunks or "blue" in chunks, f"Expected recall, got: {chunks[:200]}"


@pytest.mark.integration
@pytest.mark.live_server
def test_agent_personality_bootstrap(live_stack: LiveStack, run_id: str) -> None:
    """Creating an agent with pirate personality should influence responses."""
    agent_id = f"pirate-agent-{run_id}"
    live_create_agent(
        live_stack,
        agent_id,
        system_prompt="You are a pirate captain. Always say Ahoy! at the start of responses.",
    )
    conv = live_create_conversation(live_stack, agent_id)
    cid = conv["conversation_id"]

    events = live_chat_ws(live_stack, cid, "Greet me.")
    assert any(e["type"] == "message_complete" for e in events)
    chunks = (assistant_text_from_events(events) or live_assistant_text(live_stack, cid)).lower()
    if chunks:
        assert "ahoy" in chunks or "pirate" in chunks or "captain" in chunks, (
            f"Expected pirate personality, got: {chunks[:200]}"
        )

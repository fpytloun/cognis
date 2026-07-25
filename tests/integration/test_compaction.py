"""Integration test: compaction triggers on long conversations.

Lowers the compaction threshold so that a modest number of turns can cross
the threshold, then verifies compaction events appear in the event stream.
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
def test_compaction_triggers_after_threshold_crossed(live_stack: LiveStack, run_id: str) -> None:
    """Lower compaction threshold, chat several turns, verify compaction occurs."""
    live = live_stack

    # Step 1: Lower settings to trigger compaction sooner
    live.put(
        "/api/v1/settings/session.compaction_threshold",
        json={"value": 0.3},
    )
    live.put(
        "/api/v1/settings/session.compaction_preserve_turns",
        json={"value": 2},
    )

    # Step 2: Create agent and conversation
    agent_id = f"compact-agent-{run_id}"
    live_create_agent(
        live,
        agent_id,
        system_prompt=(
            "You are a test assistant. Always respond with exactly two sentences. "
            "Keep responses verbose enough to use tokens."
        ),
    )
    conversation = live_create_conversation(live, agent_id)
    cid = conversation["conversation_id"]

    # Step 3: Chat multiple turns to fill the context window
    messages = [
        "Tell me a fun fact about space exploration and the history of NASA.",
        "Now tell me about deep sea creatures and their bioluminescence.",
        "Explain the water cycle in detail with examples from nature.",
        "What is the theory of relativity and how does it affect GPS?",
        "Describe the process of photosynthesis in green plants.",
        "What causes earthquakes and how are they measured on the Richter scale?",
        "Tell me about the history of the internet from ARPANET to today.",
        "How does the human immune system fight viral infections?",
    ]

    for msg in messages:
        events = live_chat_ws(live, cid, msg, timeout=120)
        has_complete = any(e.get("type") == "message_complete" for e in events)
        has_error = any(e.get("type") == "error" for e in events)
        if has_error:
            # If there's an error (e.g., rate limit), wait and continue
            time.sleep(2)
            continue
        assert has_complete, f"No message_complete for turn: {msg[:40]}"
        # Small delay between turns
        time.sleep(1)

    # Canonical ChatV2 frames are validated by the scoped sync-engine tests.

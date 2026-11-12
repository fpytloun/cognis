from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.context import events_to_messages


@pytest.mark.parametrize("cached_objects", [False, True], ids=["cold-cache", "cached-events"])
def test_program_child_exchanges_are_not_replayed_to_model(cached_objects: bool) -> None:
    parent_call_id = uuid.uuid4().hex
    child_call_id = uuid.uuid4().hex
    turn_id = uuid.uuid4().hex
    events: list[dict[str, Any]] = [
        {"type": "assistant_message", "data": {"content": "Run the program."}, "seq": 1},
        {
            "type": "tool_call",
            "data": {
                "call_id": parent_call_id,
                "name": "python_execute",
                "arguments": {"code": "await cognis.emit('done')"},
                "turn_id": turn_id,
            },
            "seq": 2,
        },
        {
            "type": "tool_call",
            "data": {
                "call_id": child_call_id,
                "name": "web_search",
                "arguments": {"query": "private query"},
                "turn_id": turn_id,
                "parent_call_id": parent_call_id,
                "program_id": parent_call_id,
                "kernel_generation": 1,
                "caller": {
                    "type": "program",
                    "program_id": parent_call_id,
                    "parent_call_id": parent_call_id,
                },
            },
            "seq": 3,
        },
        {
            "type": "tool_result",
            "data": {
                "call_id": child_call_id,
                "name": "web_search",
                "result": "nested read result should not enter model history",
                "turn_id": turn_id,
                "parent_call_id": parent_call_id,
                "program_id": parent_call_id,
                "kernel_generation": 1,
                "caller": {
                    "type": "program",
                    "program_id": parent_call_id,
                    "parent_call_id": parent_call_id,
                },
            },
            "seq": 4,
        },
        {
            "type": "tool_result",
            "data": {
                "call_id": parent_call_id,
                "name": "python_execute",
                "result": "program completed",
                "turn_id": turn_id,
            },
            "seq": 5,
        },
    ]
    replay_events: list[Any] = (
        [SimpleNamespace(**event) for event in events] if cached_objects else events
    )

    messages = events_to_messages(replay_events)
    serialized = str(messages)
    assert child_call_id not in serialized
    assert "private query" not in serialized
    assert "nested read result should not enter model history" not in serialized
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == parent_call_id
        for message in messages
    )

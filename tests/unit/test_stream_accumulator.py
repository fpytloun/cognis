"""Regression tests for the LiteLLM streaming tool-call accumulator.

These cover the observed real-world failure modes where mid-stream
restarts and split tool calls could silently corrupt arguments.
"""

from __future__ import annotations

from cognis.core.agent_loop import StreamAccumulator


def _feed_tool_delta(acc: StreamAccumulator, args_fragment: str, *, name: str = "my_tool") -> None:
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": name, "arguments": args_fragment},
                            }
                        ]
                    }
                }
            ]
        }
    )


def test_basic_concatenation_assembles_full_json() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"todos":[{"content"')
    _feed_tool_delta(acc, ':"a","status":"pending"}]}')
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].arguments == {"todos": [{"content": "a", "status": "pending"}]}


def test_duplicate_prefix_is_dropped() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"todos":[]}')
    # Provider retry re-sends the same prefix.
    _feed_tool_delta(acc, '{"todos":[]}')
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].arguments == {"todos": []}


def test_longer_restart_replaces_shorter_prefix() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"todos":')
    # Provider restarts the delta with a superset of what was accumulated.
    _feed_tool_delta(acc, '{"todos":[]}')
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].arguments == {"todos": []}


def test_two_concatenated_objects_split_into_separate_calls() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"query":"a"}')
    _feed_tool_delta(acc, '{"query":"b"}')
    calls = acc.get_tool_calls()
    assert len(calls) == 2
    assert calls[0].arguments == {"query": "a"}
    assert calls[1].arguments == {"query": "b"}


def test_malformed_arguments_fall_through_to_raw() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"todos":[')  # truncated JSON
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    # _raw is the signal to validators / tool handlers; it must be
    # the only key so validate_tool_arguments can detect it.
    assert set(calls[0].arguments.keys()) == {"_raw"}

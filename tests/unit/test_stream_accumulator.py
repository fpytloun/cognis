"""Regression tests for the LiteLLM streaming tool-call accumulator.

These cover the observed real-world failure modes where mid-stream
restarts and split tool calls could silently corrupt arguments.
"""

from __future__ import annotations

from cognis.core.agent_loop import (
    _MAX_TOOL_CALL_ARGUMENT_CHARS,
    StreamAccumulator,
    _anthropic_native_envelope_for_persistence,
)
from cognis.providers.llm.errors import ToolArgumentParseFailure


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


def test_overlap_restart_appends_only_unseen_suffix() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"todos":[{"content":"Load `daily-brief`')
    _feed_tool_delta(
        acc,
        'Load `daily-brief` skill","status":"completed"}]}',
    )
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].arguments == {
        "todos": [{"content": "Load `daily-brief` skill", "status": "completed"}]
    }


def test_corrected_full_object_replaces_invalid_partial_payload() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(
        acc,
        '{"todos":[content":"Find the Cognis Todoist project and appropriate section",'
        '"status":"in_progress"}]}',
        name="step_todo_write",
    )
    _feed_tool_delta(
        acc,
        '{"todos":[{"content":"Find the Cognis Todoist project and appropriate section",'
        '"status":"in_progress"},{"content":"Create the Todoist task for Monday in '
        'the Cognis project","status":"pending"}]}',
        name="step_todo_write",
    )
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "step_todo_write"
    assert calls[0].arguments == {
        "todos": [
            {
                "content": "Find the Cognis Todoist project and appropriate section",
                "status": "in_progress",
            },
            {
                "content": "Create the Todoist task for Monday in the Cognis project",
                "status": "pending",
            },
        ]
    }


def test_large_deliverable_content_is_not_truncated_by_a_coincidental_small_object() -> None:
    """Regression test for the write_deliverable large-args truncation bug.

    While streaming a large tool call, a mid-stream delta chunk can
    coincidentally be a self-contained, parseable JSON object (e.g. a chunk
    that closes a nested value early) even though the overall accumulation is
    nowhere near complete. The accumulator must keep appending rather than
    discarding everything accumulated so far in favor of that tiny fragment.
    """

    acc = StreamAccumulator()
    _feed_tool_delta(
        acc,
        '{"action":"write_deliverable","content":"## Report\\n\\n',
        name="write_deliverable",
    )
    # A long body streamed across many small deltas. Each chunk is distinct so
    # legitimate repeated-suffix dedup logic elsewhere in the merge doesn't
    # interfere with this test's own scenario.
    for i in range(20):
        _feed_tool_delta(acc, f"Lorem ipsum dolor sit amet number {i}. ", name="write_deliverable")
    _feed_tool_delta(acc, '","format":"markdown","outputs":', name="write_deliverable")
    # A coincidental small complete object mid-stream (not a real replay) --
    # e.g. a nested "outputs" value that happens to close out as valid JSON
    # entirely on its own before the outer object is finished.
    _feed_tool_delta(acc, '{"x": 1}', name="write_deliverable")
    _feed_tool_delta(acc, ',"title":"done"}', name="write_deliverable")

    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "write_deliverable"
    assert calls[0].arguments["action"] == "write_deliverable"
    assert calls[0].arguments["format"] == "markdown"
    assert calls[0].arguments["outputs"] == {"x": 1}
    assert calls[0].arguments["title"] == "done"
    assert calls[0].arguments["content"].startswith("## Report")
    assert "Lorem ipsum" in calls[0].arguments["content"]
    assert calls[0].arguments["content"].count("Lorem ipsum") == 20
    assert "number 19" in calls[0].arguments["content"]


def test_divergent_full_object_on_one_index_collapses_to_single_call() -> None:
    """Two complete, divergent JSON objects on ONE stream index is a provider
    double-feed of a single logical call, not two parallel calls. It must
    collapse to one call (the corrected/last object) with the ORIGINAL call
    id -- never fabricate a second call with a minted id, which caused the
    same bash/MCP/create call to execute twice.
    """

    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"query":"a"}')
    _feed_tool_delta(acc, '{"query":"b"}')
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].call_id == "call_1"
    assert calls[0].arguments == {"query": "b"}


def test_shorter_divergent_full_object_on_one_index_is_one_corrected_call() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"command":"echo a long initial command"}', name="bash")
    _feed_tool_delta(acc, '{"command":"pwd"}', name="bash")

    calls = acc.get_tool_calls()

    assert len(calls) == 1
    assert calls[0].call_id == "call_1"
    assert calls[0].arguments == {"command": "pwd"}


def test_parallel_tool_calls_on_separate_indexes_stay_separate() -> None:
    """Genuine parallel tool calls arrive on distinct stream indexes and must
    remain two independent calls with their own ids.
    """

    acc = StreamAccumulator()
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "web_search", "arguments": '{"query":"a"}'},
                            },
                            {
                                "index": 1,
                                "id": "call_b",
                                "function": {"name": "web_search", "arguments": '{"query":"b"}'},
                            },
                        ]
                    }
                }
            ]
        }
    )
    calls = acc.get_tool_calls()
    assert len(calls) == 2
    assert calls[0].call_id == "call_a"
    assert calls[0].arguments == {"query": "a"}
    assert calls[1].call_id == "call_b"
    assert calls[1].arguments == {"query": "b"}


def test_concatenated_json_that_bypasses_merge_collapses_to_single_call() -> None:
    """A single accumulated buffer that already contains concatenated objects
    (e.g. seeded directly, as a restored partial can) must still collapse to a
    single call rather than fabricating multiple minted-id calls.
    """

    acc = StreamAccumulator()
    acc.tool_calls[0] = {
        "id": "call_seed",
        "name": "bash",
        "arguments": '{"command":"ls"}{"command":"pwd"}',
    }
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].call_id == "call_seed"
    assert calls[0].arguments == {"command": "pwd"}


def test_malformed_arguments_fall_through_to_raw() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(acc, '{"todos":[')  # truncated JSON
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert isinstance(calls[0], ToolArgumentParseFailure)
    assert calls[0].raw == '{"todos":['


def test_oversized_tool_arguments_become_recoverable_failure() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(
        acc,
        '{"patchText":"' + ("x" * (_MAX_TOOL_CALL_ARGUMENT_CHARS + 1)),
        name="apply_patch",
    )
    _feed_tool_delta(acc, "ignored-after-limit", name="apply_patch")

    calls = acc.get_tool_calls()

    assert len(calls) == 1
    assert isinstance(calls[0], ToolArgumentParseFailure)
    assert calls[0].name == "apply_patch"
    assert calls[0].reason == "tool_call_arguments_too_large"
    assert calls[0].recovery_attempts == ("tool_call_arguments_too_large",)
    assert calls[0].argument_length is not None
    assert calls[0].argument_length > _MAX_TOOL_CALL_ARGUMENT_CHARS
    assert len(calls[0].raw) == 4096


def test_recover_trailing_valid_object_for_mcp_tool_arguments() -> None:
    acc = StreamAccumulator()
    _feed_tool_delta(
        acc,
        '{"tasks":[content":"Fixnout sizing RDS instance v Terraform",'
        '"description":"","priority":"p4","dueString":"Monday",'
        '"deadlineDate":"","duration":"","labels":[],"'
        'projectId":"6fMRX3vr2McFxCr7","sectionId":"6fr2pjvJV2M5PFF7",'
        '"parentId":"","order":0,"responsibleUser":"",'
        '"isUncompletable":false}]}{"tasks":[{"content":"Fixnout sizing RDS '
        'instance v Terraform","description":"","priority":"p4",'
        '"dueString":"Monday","deadlineDate":"","duration":"",'
        '"labels":[],"projectId":"6fMRX3vr2McFxCr7",'
        '"sectionId":"6fr2pjvJV2M5PFF7","parentId":"","order":0,'
        '"responsibleUser":"","isUncompletable":false}]}',
        name="mcp__todoist_add_tasks",
    )
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "mcp__todoist_add_tasks"
    assert calls[0].arguments == {
        "tasks": [
            {
                "content": "Fixnout sizing RDS instance v Terraform",
                "description": "",
                "priority": "p4",
                "dueString": "Monday",
                "deadlineDate": "",
                "duration": "",
                "labels": [],
                "projectId": "6fMRX3vr2McFxCr7",
                "sectionId": "6fr2pjvJV2M5PFF7",
                "parentId": "",
                "order": 0,
                "responsibleUser": "",
                "isUncompletable": False,
            }
        ]
    }


def test_thinking_blocks_include_stable_request_scoped_id_and_timing() -> None:
    acc = StreamAccumulator(block_id_prefix="llmr_test")

    acc.feed({"choices": [{"delta": {"reasoning": "Inspecting logs"}}]})
    chunk_events = acc.pop_thinking_events()
    assert len(chunk_events) == 1
    assert chunk_events[0].block_id == "thk_llmr_test_1"
    assert chunk_events[0].started_at is not None
    assert chunk_events[0].complete is False

    completed = acc.finalize_thinking()
    close_events = acc.pop_thinking_events()

    assert len(completed) == 1
    assert completed[0].block_id == "thk_llmr_test_1"
    assert completed[0].started_at is not None
    assert completed[0].completed_at is not None
    assert isinstance(completed[0].duration_ms, int)
    assert completed[0].duration_ms >= 0
    assert len(close_events) == 1
    assert close_events[0].complete is True
    assert close_events[0].content == "Inspecting logs"
    assert close_events[0].duration_ms == completed[0].duration_ms


def test_structured_anthropic_thinking_blocks_are_collected_in_order() -> None:
    acc = StreamAccumulator(block_id_prefix="llmr_test")

    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "provider_thinking_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "Inspect first.",
                                "signature": "sig-1",
                            }
                        ]
                    }
                }
            ]
        }
    )
    acc.feed(
        {
            "choices": [
                {"delta": {"thinking_blocks": [{"type": "redacted_thinking", "data": "opaque"}]}}
            ]
        }
    )

    assert acc.get_anthropic_thinking_blocks() == [
        {"type": "thinking", "thinking": "Inspect first.", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]


def test_native_envelope_is_captured_and_invalid_payload_is_non_continuable() -> None:
    envelope = {
        "contract_version": 1,
        "native_blocks": [
            {"type": "thinking", "thinking": "reason", "signature": "sig"},
            {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x"}},
        ],
        "stop_reason": "tool_use",
        "stop_details": {},
        "usage": {"input_tokens": 1},
        "pending_client_message_id": None,
        "pending_server_message_id": "msg_1",
        "bundle_fingerprint": "bundle",
        "provider_fingerprint": "provider",
        "model_fingerprint": "model",
        "thinking_fingerprint": "thinking",
        "continuation_status": "continuable",
    }
    acc = StreamAccumulator()
    acc.feed({"anthropic_native_envelope": envelope})

    assert acc.get_anthropic_native_envelope() == envelope
    assert _anthropic_native_envelope_for_persistence(envelope) == envelope

    envelope["native_blocks"].append({"type": "unknown"})
    assert _anthropic_native_envelope_for_persistence(envelope) == {
        "contract_version": 1,
        "continuation_status": "non_continuable",
    }

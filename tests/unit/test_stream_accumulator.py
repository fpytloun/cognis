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

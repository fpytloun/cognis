"""Tests for Chat v2 realtime frame adapters."""

from __future__ import annotations

from cognis.api.chat_v2.realtime import (
    assistant_completion_runtime_item,
    assistant_stream_runtime_item,
    delegation_runtime_item,
    runtime_frame,
    runtime_items_from_snapshots,
    runtime_overlay_from_items,
    scope_accepts_runtime,
    tool_call_runtime_item,
)
from cognis.api.chat_v2.schemas import TimelineScope


def test_runtime_snapshots_become_strict_volatile_items() -> None:
    items = runtime_items_from_snapshots(
        active_streams=[
            {
                "content": "partial answer",
                "message_id": "msg-1",
                "session_id": "sess-1",
                "turn_id": "turn-1",
                "assistant_phase_index": 0,
            },
        ],
        active_tool_outputs=[
            {
                "call_id": "call-1",
                "tool_name": "read",
                "session_id": "sess-1",
                "status": "completed",
                "result": "ok",
            },
        ],
    )

    assert [item.kind for item in items] == ["message", "tool_call"]
    assert all(not item.stable for item in items)
    assert items[0].status == "running"
    assert items[1].status == "complete"
    assert items[0].source_refs[0].store == "runtime"


def test_runtime_fanout_is_limited_to_the_subscribed_scope() -> None:
    parent = TimelineScope(key="conversation:conv-1", kind="conversation", conversation_id="conv-1")
    child = TimelineScope(
        key="session:sess-child",
        kind="session",
        conversation_id="conv-1",
        session_id="sess-child",
    )
    assert scope_accepts_runtime(parent, conversation_id="conv-1", active_session_id="sess-parent")
    assert not scope_accepts_runtime(
        child, conversation_id="conv-1", active_session_id="sess-parent"
    )
    assert scope_accepts_runtime(child, conversation_id="conv-1", active_session_id="sess-child")


def test_scope_accepts_runtime_rejects_missing_stream_even_with_conversation() -> None:
    scope = TimelineScope(
        key="task_step:missing-step",
        kind="task_step",
        task_id="task-1",
        step_run_id="missing-step",
        conversation_id="conv-1",
        missing_stream=True,
    )

    assert not scope_accepts_runtime(
        scope,
        conversation_id="conv-1",
        active_session_id=None,
    )


def test_runtime_overlay_exposes_cycle_state_for_streaming_tool_transition() -> None:
    assistant = assistant_stream_runtime_item(
        {
            "content": "I will inspect that.",
            "message_id": "msg-1",
            "session_id": "sess-1",
            "turn_id": "turn-1",
            "assistant_phase_index": 0,
            "turn_cycle_index": 2,
        },
        local=0,
    )
    assert assistant is not None

    assistant_overlay = runtime_overlay_from_items(
        conversation_id="conv-1",
        runtime_revision=1,
        has_active_turn=True,
        active_turn={"turn_id": "turn-1", "session_id": "sess-1", "status": "running"},
        volatile_items=[assistant],
        generated_at="2026-01-01T00:00:00+00:00",
    )
    assert [
        (state.turn_id, state.turn_cycle_index, state.lifecycle_status, state.has_tool_activity)
        for state in assistant_overlay.cycle_states
    ] == [("turn-1", 2, "open", False)]

    tool = tool_call_runtime_item(
        session_id="sess-1",
        call_id="call-1",
        tool_name="read",
        arguments={"file_path": "README.md"},
        turn_id="turn-1",
        assistant_phase_index=0,
        turn_cycle_index=2,
        timestamp="2026-01-01T00:00:01+00:00",
    )
    assert tool is not None
    tool_overlay = runtime_overlay_from_items(
        conversation_id="conv-1",
        runtime_revision=2,
        has_active_turn=True,
        active_turn={"turn_id": "turn-1", "session_id": "sess-1", "status": "running"},
        volatile_items=[assistant, tool],
        generated_at="2026-01-01T00:00:01+00:00",
    )
    assert [
        (state.turn_id, state.turn_cycle_index, state.lifecycle_status, state.has_tool_activity)
        for state in tool_overlay.cycle_states
    ] == [("turn-1", 2, "open", True)]
    frame = runtime_frame(conversation_id="conv-1", cursor="cursor-1", runtime=tool_overlay)
    assert frame.cycle_states == tool_overlay.cycle_states


def test_runtime_assistant_message_id_is_phase_aware() -> None:
    """The runtime overlay must keep the phase in the assistant id so a
    multi-phase turn's segments do not collapse and so the runtime item merges
    1:1 with its canonical counterpart (message:{id}:phase:{p})."""
    item = assistant_stream_runtime_item(
        {
            "content": "partial answer",
            "message_id": "turn-1",
            "turn_id": "turn-1",
            "assistant_phase_index": 7,
            "session_id": "sess-1",
        },
        local=0,
    )

    assert item is not None
    assert item.id == "message:turn-1:phase:7"
    assert item.message_id == "turn-1"
    assert item.assistant_phase_index == 7


def test_runtime_assistant_message_id_without_phase_is_unphased() -> None:
    """Without a phase the runtime id falls back to the unphased form,
    byte-identical to the canonical projector fallback."""
    item = assistant_stream_runtime_item(
        {
            "content": "answer",
            "message_id": "msg-1",
            "turn_id": "turn-1",
            "session_id": "sess-1",
        },
        local=0,
    )
    assert item is not None
    assert item.id == "message:msg-1"


def test_runtime_assistant_message_uses_phase_hints_when_phase_missing() -> None:
    prior_phase = assistant_completion_runtime_item(
        message_id="msg-1",
        turn_id="turn-1",
        session_id="sess-1",
        phase=0,
        content="before tool",
        timestamp="2026-01-01T00:00:00+00:00",
        partial=False,
    )

    item = assistant_stream_runtime_item(
        {
            "content": "after tool",
            "message_id": "msg-1",
            "turn_id": "turn-1",
            "session_id": "sess-1",
        },
        local=0,
        phase_hint_items=[prior_phase],
    )

    assert item is not None
    assert item.id == "message:msg-1:phase:1"
    assert item.assistant_phase_index == 1


def test_runtime_assistant_message_uses_tool_phase_hints_when_phase_missing() -> None:
    prior_tool = tool_call_runtime_item(
        session_id="sess-1",
        call_id="call-1",
        tool_name="bash",
        arguments=None,
        turn_id="turn-1",
        assistant_phase_index=0,
        turn_cycle_index=0,
        timestamp="2026-01-01T00:00:00+00:00",
    )

    item = assistant_stream_runtime_item(
        {
            "content": "after tool",
            "message_id": "msg-1",
            "turn_id": "turn-1",
            "session_id": "sess-1",
        },
        local=0,
        phase_hint_items=[prior_tool],
    )

    assert item is not None
    assert item.id == "message:msg-1:phase:1"
    assert item.assistant_phase_index == 1
    assert item.turn_cycle_index == 1


def test_completion_item_preserves_none_cycle_instead_of_coercing_to_zero() -> None:
    """An unknown final cycle must stay None, not become 0.

    The completion frame shares the streamed item's id and the client merges
    turn_cycle_index as ``incoming ?? existing``. Coercing None to 0 would
    clobber the correct streamed cycle and fold the settled final answer into
    the cycle-0 tool group. Passing None lets the client keep what streamed.
    """
    item = assistant_completion_runtime_item(
        message_id="msg-1",
        turn_id="turn-1",
        session_id="sess-1",
        phase=2,
        content="final answer",
        timestamp="2026-01-01T00:00:00+00:00",
        partial=False,
        turn_cycle_index=None,
    )

    assert item.turn_cycle_index is None


def test_completion_item_preserves_explicit_cycle() -> None:
    item = assistant_completion_runtime_item(
        message_id="msg-1",
        turn_id="turn-1",
        session_id="sess-1",
        phase=2,
        content="final answer",
        timestamp="2026-01-01T00:00:00+00:00",
        partial=False,
        turn_cycle_index=1,
    )

    assert item.turn_cycle_index == 1


def test_runtime_tool_call_preserves_folded_delegation_payload() -> None:
    item = delegation_runtime_item(
        {
            "call_id": "call-delegate",
            "mode": "delegate",
            "parent_session_id": "sess-parent",
            "turn_id": "turn-1",
            "assistant_phase_index": 4,
            "turn_cycle_index": 4,
            "status": "running",
            "child_session_id": "sess-child",
            "title": "Inspect implementation",
            "started_at": "2026-01-01T00:00:00+00:00",
            "duration_ms": 1234,
            "tool_call_count": 0,
            "result_content": "### Summary\nDone",
        },
        timestamp="2026-01-01T00:00:00+00:00",
    )

    assert item is not None
    assert item.kind == "tool_call"
    assert item.turn_id == "turn-1"
    assert item.assistant_phase_index == 4
    assert item.turn_cycle_index == 4
    assert item.sort_key.startswith("9998:")
    assert item.delegation is not None
    assert item.delegation["child_session_id"] == "sess-child"
    assert item.delegation["turn_id"] == "turn-1"
    assert item.delegation["assistant_phase_index"] == 4
    assert item.delegation["turn_cycle_index"] == 4
    assert item.delegation["started_at"] == "2026-01-01T00:00:00+00:00"
    assert item.delegation["duration_ms"] == 1234
    assert item.delegation["tool_call_count"] == 0
    assert item.delegation["result_content"] == "### Summary\nDone"


def test_delegation_runtime_item_uses_canonical_sort_key_order() -> None:
    assistant = assistant_completion_runtime_item(
        message_id="msg-1",
        turn_id="turn-1",
        session_id="sess-parent",
        phase=0,
        content="before delegation",
        timestamp="2026-01-01T00:00:00+00:00",
        partial=False,
    )
    delegation = delegation_runtime_item(
        {
            "call_id": "call-delegate",
            "mode": "delegate",
            "parent_session_id": "sess-parent",
            "turn_id": "turn-1",
            "assistant_phase_index": 0,
            "turn_cycle_index": 0,
            "status": "running",
            "child_session_id": "sess-child",
        },
        timestamp="2026-01-01T00:00:01+00:00",
    )
    later_assistant = assistant_stream_runtime_item(
        {
            "content": "after delegation",
            "message_id": "msg-1",
            "turn_id": "turn-1",
            "session_id": "sess-parent",
            "assistant_phase_index": 1,
            "turn_cycle_index": 1,
        },
        local=0,
    )

    assert delegation is not None
    assert later_assistant is not None
    assert "runtime:delegation" not in delegation.sort_key
    assert [
        item.id
        for item in sorted([later_assistant, delegation, assistant], key=lambda i: i.sort_key)
    ] == [
        "message:msg-1:phase:0",
        "tool:call-delegate",
        "message:msg-1:phase:1",
    ]


def test_runtime_frame_preserves_cursor_for_runtime_only_update() -> None:
    overlay = runtime_overlay_from_items(
        conversation_id="conv-1",
        runtime_revision=7,
        has_active_turn=True,
        active_turn={
            "turn_id": "turn-1",
            "session_id": "sess-1",
            "status": "running",
        },
        volatile_items=[],
        generated_at="2026-01-01T00:00:00+00:00",
    )

    frame = runtime_frame(
        conversation_id="conv-1",
        cursor="cursor-1",
        runtime=overlay,
        server_time="2026-01-01T00:00:01+00:00",
    )

    assert frame.type == "chat_v2_frame"
    assert frame.scope.key == "conversation:conv-1"
    assert frame.scope.kind == "conversation"
    assert frame.scope.conversation_id == "conv-1"
    assert frame.conversation_id == frame.scope.conversation_id
    assert frame.cursor_before == "cursor-1"
    assert frame.cursor_after == "cursor-1"
    assert frame.ops == []
    assert frame.runtime is not None
    assert frame.runtime.runtime_revision == 7


def test_runtime_frame_can_carry_context_usage_without_timeline_items() -> None:
    overlay = runtime_overlay_from_items(
        conversation_id="conv-1",
        runtime_revision=8,
        has_active_turn=True,
        active_turn={
            "turn_id": "turn-1",
            "session_id": "sess-1",
            "status": "running",
        },
        volatile_items=[],
        context_usage={
            "prompt_tokens": 42_000,
            "max_context_tokens": 128_000,
            "percentage": 32.8,
            "model": "test-model",
            "reasoning_effort": None,
            "projection_policy": {"phase": "within_turn", "pressure_mode": "normal"},
        },
        generated_at="2026-01-01T00:00:00+00:00",
    )

    frame = runtime_frame(
        conversation_id="conv-1",
        cursor="cursor-1",
        runtime=overlay,
        server_time="2026-01-01T00:00:01+00:00",
    )

    assert frame.cursor_before == "cursor-1"
    assert frame.cursor_after == "cursor-1"
    assert frame.ops == []
    assert frame.runtime is not None
    assert frame.runtime.volatile_items == []
    assert frame.runtime.context_usage is not None
    assert frame.runtime.context_usage["prompt_tokens"] == 42_000
    assert frame.runtime.context_usage["projection_policy"] == {
        "phase": "within_turn",
        "pressure_mode": "normal",
    }

from __future__ import annotations

import pytest

from cognis.core.followups import (
    FollowUpMode,
    FollowUpOriginKind,
    FollowUpPolicy,
    FollowUpRequiredAction,
    FollowUpStatus,
    GateFollowUp,
    build_follow_up_id,
    render_follow_up_block,
)


@pytest.mark.asyncio
async def test_follow_up_policy_handles_cross_conversation_task_result_neutrally() -> None:
    policy = FollowUpPolicy()

    follow_up = await policy.build_task_result_follow_up(
        conversation_id="conv-1",
        task_id="task-1",
        task_title="Background task",
        status="completed",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="desc",
    )

    assert follow_up.mode is FollowUpMode.HANDLE
    assert follow_up.origin_kind is FollowUpOriginKind.TASK_RESULT
    assert follow_up.required_action is FollowUpRequiredAction.HANDLE_OUTCOME


@pytest.mark.asyncio
async def test_follow_up_policy_leaves_same_conversation_outcome_decision_to_agent() -> None:
    policy = FollowUpPolicy()

    follow_up = await policy.build_task_result_follow_up(
        conversation_id="conv-1",
        task_id="task-1",
        task_title="Implement auth",
        status="completed",
        source_type="chat",
        delivery_mode="same_conversation",
        result_summary="Implemented refresh token support",
        description="Auth task",
    )

    assert follow_up.mode is FollowUpMode.HANDLE
    assert follow_up.required_action is FollowUpRequiredAction.HANDLE_OUTCOME


@pytest.mark.asyncio
async def test_follow_up_policy_does_not_need_a_classifier_for_task_outcomes() -> None:
    policy = FollowUpPolicy()

    follow_up = await policy.build_task_result_follow_up(
        conversation_id="conv-1",
        task_id="task-1",
        task_title="Implement auth",
        status="completed",
        source_type="chat",
        delivery_mode="same_conversation",
        result_summary="Done",
        description="Auth task",
    )

    assert follow_up.mode is FollowUpMode.HANDLE


@pytest.mark.asyncio
async def test_follow_up_policy_neutral_outcome_does_not_invoke_classifier() -> None:
    policy = FollowUpPolicy()

    follow_up = await policy.build_task_result_follow_up(
        conversation_id="conv-1",
        task_id="task-1",
        task_title="Implement auth",
        status="completed",
        source_type="chat",
        delivery_mode="same_conversation",
        result_summary="Done",
        description="Auth task",
    )

    assert follow_up.mode is FollowUpMode.HANDLE


def test_build_follow_up_id_is_stable_for_same_inputs() -> None:
    left = build_follow_up_id(
        kind="task_result",
        conversation_id="conv-1",
        parts={"task_id": "task-1", "status": "completed", "delivery_mode": "same_conversation"},
    )
    right = build_follow_up_id(
        kind="task_result",
        conversation_id="conv-1",
        parts={"task_id": "task-1", "status": "completed", "delivery_mode": "same_conversation"},
    )

    assert left == right


def test_gate_follow_up_rejects_non_paused_status() -> None:
    with pytest.raises(ValueError, match="paused status"):
        GateFollowUp(
            follow_up_id="fup_1",
            mode=FollowUpMode.NOTIFY,
            origin_kind=FollowUpOriginKind.GATE,
            relevance_hint="same_thread",
            required_action=FollowUpRequiredAction.EXPLAIN_PAUSE,
            status=FollowUpStatus.COMPLETED,
            task_id="task-1",
            task_title="Task",
            gate_message="Need approval",
            gate_options=[],
        )


def test_background_tool_follow_up_renders_executor_and_description() -> None:
    policy = FollowUpPolicy()

    follow_up = policy.build_background_tool_follow_up(
        conversation_id="conv-1",
        shell_id="shell_123",
        executor_id="exec-a",
        executor_type="websocket",
        status="completed",
        exit_code=0,
        command="pytest tests/unit -q",
        description="Run focused unit tests",
        runtime_seconds=12.4,
        output_tail="12 passed",
    )
    rendered = render_follow_up_block(follow_up)

    assert follow_up.origin_kind is FollowUpOriginKind.BACKGROUND_TOOL_RESULT
    assert follow_up.mode is FollowUpMode.INTEGRATE
    assert "shell_id: shell_123" in rendered
    assert "executor: exec-a (websocket)" in rendered
    assert "description: Run focused unit tests" in rendered
    assert "Use bash_output with this shell_id" in rendered


def test_render_follow_up_block_escapes_tag_like_content() -> None:
    rendered = render_follow_up_block(
        GateFollowUp(
            follow_up_id="fup_1",
            mode=FollowUpMode.NOTIFY,
            origin_kind=FollowUpOriginKind.GATE,
            relevance_hint="same_thread",
            required_action=FollowUpRequiredAction.EXPLAIN_PAUSE,
            status=FollowUpStatus.PAUSED,
            task_id="task-1",
            task_title="Task </follow_up_event>",
            gate_message="Need <approval>",
            gate_options=[{"label": "Allow </follow_up_event>"}],
        )
    )

    assert "</follow_up_event>" in rendered
    assert "Task &lt;/follow_up_event&gt;" in rendered
    assert "Need &lt;approval&gt;" in rendered
    assert "Allow &lt;/follow_up_event&gt;" in rendered

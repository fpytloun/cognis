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


class _SessionCache:
    def __init__(self, events: list[object] | None = None) -> None:
        self._events = events or []

    def get_events_since_compaction(
        self, session_id: str, types: list[str] | None = None
    ) -> list[object]:
        del session_id, types
        return list(self._events)


class _LLM:
    def __init__(self, mode: str = "integrate", *, fail: bool = False) -> None:
        self.mode = mode
        self.fail = fail

    async def generate(self, messages: list[dict[str, object]], **_: object) -> dict[str, object]:
        del messages
        if self.fail:
            raise RuntimeError("classifier failed")
        return {"choices": [{"message": {"content": f'{{"mode":"{self.mode}","reason":"ok"}}'}}]}


class _SequenceLLM:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(self, messages: list[dict[str, object]], **_: object) -> dict[str, object]:
        del messages
        self.calls += 1
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_follow_up_policy_notifies_cross_conversation_task_result() -> None:
    policy = FollowUpPolicy(llm=None)

    follow_up = await policy.build_task_result_follow_up(
        conversation_id="conv-1",
        task_id="task-1",
        task_title="Background task",
        status="completed",
        source_type="api",
        delivery_mode="latest_active_for_agent",
        result_summary="Done",
        description="desc",
        session_id="sess-1",
        session_cache=_SessionCache(),
    )

    assert follow_up.mode is FollowUpMode.NOTIFY
    assert follow_up.origin_kind is FollowUpOriginKind.TASK_RESULT
    assert follow_up.required_action is FollowUpRequiredAction.PRESENT_UPDATE


@pytest.mark.asyncio
async def test_follow_up_policy_uses_classifier_for_same_conversation_task_result() -> None:
    policy = FollowUpPolicy(
        llm=_LLM(mode="integrate"),
    )

    follow_up = await policy.build_task_result_follow_up(
        conversation_id="conv-1",
        task_id="task-1",
        task_title="Implement auth",
        status="completed",
        source_type="chat",
        delivery_mode="same_conversation",
        result_summary="Implemented refresh token support",
        description="Auth task",
        session_id="sess-1",
        session_cache=_SessionCache(
            [
                {"type": "user_message", "data": {"content": "Please implement auth refresh."}},
                {
                    "type": "assistant_message",
                    "data": {"content": "Working on auth refresh in the background."},
                },
            ]
        ),
    )

    assert follow_up.mode is FollowUpMode.INTEGRATE
    assert follow_up.required_action is FollowUpRequiredAction.INTEGRATE_RESULT


@pytest.mark.asyncio
async def test_follow_up_policy_falls_back_to_notify_on_classifier_failure() -> None:
    policy = FollowUpPolicy(llm=_LLM(fail=True))

    follow_up = await policy.build_task_result_follow_up(
        conversation_id="conv-1",
        task_id="task-1",
        task_title="Implement auth",
        status="completed",
        source_type="chat",
        delivery_mode="same_conversation",
        result_summary="Done",
        description="Auth task",
        session_id="sess-1",
        session_cache=_SessionCache(
            [{"type": "user_message", "data": {"content": "Please implement auth refresh."}}]
        ),
    )

    assert follow_up.mode is FollowUpMode.NOTIFY


@pytest.mark.asyncio
async def test_follow_up_policy_classifier_falls_back_to_plain_json_text() -> None:
    llm = _SequenceLLM(
        [
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": '{"mode":"integrate","reason":"ok"}'}}]},
        ]
    )
    policy = FollowUpPolicy(llm=llm)

    follow_up = await policy.build_task_result_follow_up(
        conversation_id="conv-1",
        task_id="task-1",
        task_title="Implement auth",
        status="completed",
        source_type="chat",
        delivery_mode="same_conversation",
        result_summary="Done",
        description="Auth task",
        session_id="sess-1",
        session_cache=_SessionCache(
            [{"type": "user_message", "data": {"content": "Please implement auth refresh."}}]
        ),
    )

    assert llm.calls == 2
    assert follow_up.mode is FollowUpMode.INTEGRATE


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
    policy = FollowUpPolicy(llm=None)

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

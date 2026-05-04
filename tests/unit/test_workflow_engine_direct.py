from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import PauseWaiter
from cognis.core.events import EventBus
from cognis.core.followups import (
    FollowUpMode,
    FollowUpOriginKind,
    FollowUpRequiredAction,
    FollowUpStatus,
    TaskResultFollowUp,
)
from cognis.core.runtime import ResolvedStepRuntime, build_local_executor_environment
from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.agent import AgentDefinition


@pytest.mark.asyncio
async def test_run_direct_turn_enables_questions() -> None:
    captured: dict[str, object] = {}

    class _AgentLoop:
        async def run_step(self, ctx: object, **_: object) -> str:
            captured["ctx"] = ctx
            return "ok"

    async def _runtime_factory(**_: object) -> ResolvedStepRuntime:
        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="registry",
            executor_connection="executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    engine = WorkflowEngine(
        session_factory=SimpleNamespace(),
        providers=SimpleNamespace(),
        agent_loop=_AgentLoop(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )

    await engine.run_direct_turn(
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        session=SimpleNamespace(user_email="user@example.com"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        user_message="Need help",
    )

    ctx = captured["ctx"]
    assert ctx.interaction_mode == "step_requests"
    assert ctx.step_definition.allow_questions is True
    assert ctx.step_definition.step_profile_id == "system:direct-default"


@pytest.mark.asyncio
async def test_run_direct_turn_threads_follow_up_metadata() -> None:
    captured: dict[str, object] = {}

    class _AgentLoop:
        async def run_step(self, ctx: object, **_: object) -> str:
            captured["ctx"] = ctx
            return "ok"

    async def _runtime_factory(**_: object) -> ResolvedStepRuntime:
        async def _cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry="registry",
            executor_connection="executor",
            cleanup=_cleanup,
            executor_environment=build_local_executor_environment(),
        )

    engine = WorkflowEngine(
        session_factory=SimpleNamespace(),
        providers=SimpleNamespace(),
        agent_loop=_AgentLoop(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=EventBus(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=_runtime_factory,
    )
    follow_up = TaskResultFollowUp(
        follow_up_id="fup_1",
        mode=FollowUpMode.NOTIFY,
        origin_kind=FollowUpOriginKind.TASK_RESULT,
        relevance_hint="unknown",
        required_action=FollowUpRequiredAction.PRESENT_UPDATE,
        topic_ref="task-1",
        status=FollowUpStatus.COMPLETED,
        task_id="task-1",
        task_title="Daily brief",
        source_type="scheduler",
        delivery_mode="same_conversation",
        result_summary="Done",
        description="daily schedule",
    )

    await engine.run_direct_turn(
        conversation=SimpleNamespace(
            conversation_id="conv-1",
            context=SimpleNamespace(type="web", ref=None, platform_data={}),
        ),
        session=SimpleNamespace(user_email="user@example.com"),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        user_message="",
        system_initiated=True,
        follow_up=follow_up,
    )

    ctx = captured["ctx"]
    assert ctx.follow_up == follow_up

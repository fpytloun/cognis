"""Focused execution tests for controller-owned deterministic workflow steps."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.events import EventBus, EventType
from cognis.core.management import validate_workflow_definition
from cognis.core.runtime import ResolvedStepRuntime, TransientExecutorUnavailable
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_registry import SOFTWARE_DEVELOPMENT_WORKFLOW
from cognis.core.workflow_rendering import MAX_DETERMINISTIC_JUMPS, WorkflowRenderer
from cognis.models.agent import AgentDefinition
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.tool import NativeToolDefinition, ToolResult, ToolSource
from cognis.models.workflow import StepDefinition, StepOutput, Workflow, WorkflowState
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_step_run,
    create_task,
    create_user,
    get_latest_step_run_for_task_step,
    get_step_run,
    get_task,
    list_step_runs_for_task,
    update_step_run,
    update_task_workflow_state,
)
from cognis.tools.registry import RegisteredTool, ToolRegistry


class _DeterministicAgentLoop:
    def __init__(self, result: ToolResult | None = None) -> None:
        self.result = result or ToolResult(output='{"messages":[]}')
        self.calls: list[object] = []
        self.run_step = AsyncMock()
        self.artifact_store = None

    async def execute_controller_tool(self, _ctx: object, tool_call: object) -> ToolResult:
        self.calls.append(tool_call)
        return self.result

    async def persist_controller_tool_output(
        self,
        _ctx: object,
        tool_call: object,
        result: ToolResult,
    ) -> ToolResult:
        metadata = dict(result.metadata or {})
        metadata.pop("_raw_output", None)
        metadata["has_full_output"] = True
        metadata["recovery_call_id"] = tool_call.call_id
        return result.model_copy(update={"metadata": metadata})


async def _engine_runtime(
    tmp_path: object,
    *,
    agent_loop: object | None = None,
) -> tuple[object, object, WorkflowEngine, TaskModel, EventBus]:
    db_engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/deterministic.db")
    async with db_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(db_engine)
    state = WorkflowState()
    async with session_factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hash",
            role="user",
        )
        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
        )
        await create_task(
            session,
            task_id="task-deterministic",
            created_by="user@example.com",
            agent_id="agent-1",
            title="Deterministic task",
            status="running",
            source_type="scheduler",
            delivery_mode="preferred_channel",
            workflow_state=state.model_dump(mode="json"),
        )
        await session.commit()

    event_bus = EventBus()
    workflow_engine = WorkflowEngine(
        session_factory=session_factory,
        providers=SimpleNamespace(llm=AsyncMock()),
        agent_loop=agent_loop or _DeterministicAgentLoop(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(mark_completed=AsyncMock()),
        event_bus=event_bus,
        pause_waiter=SimpleNamespace(),
    )
    task = TaskModel(
        task_id="task-deterministic",
        title="Deterministic task",
        status=TaskStatus.RUNNING,
        created_by="user@example.com",
        agent_id="agent-1",
        source_type="scheduler",
        delivery=TaskDelivery(mode="preferred_channel"),
        workflow_state=state,
    )
    return db_engine, session_factory, workflow_engine, task, event_bus


def _mock_tool_runtime(
    engine: WorkflowEngine,
    monkeypatch: pytest.MonkeyPatch,
    registry: ToolRegistry,
) -> AsyncMock:
    cleanup = AsyncMock()
    runtime = ResolvedStepRuntime(
        tool_registry=registry,
        executor_connection=SimpleNamespace(),
        cleanup=cleanup,
        executor_environment=None,
        runtime_info={"runtime_source": "test"},
    )
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
    )
    monkeypatch.setattr(engine, "_resolve_step_agents", AsyncMock(return_value=(agent, agent)))
    monkeypatch.setattr(
        engine,
        "_reuse_or_create_step_session",
        AsyncMock(
            return_value=(
                SimpleNamespace(conversation_id="conversation-1"),
                SimpleNamespace(
                    session_id="session-1",
                    intaris_session_id="intaris-1",
                    parent_session_id=None,
                    delegation_mode=None,
                ),
                False,
            )
        ),
    )
    monkeypatch.setattr(engine, "_resolve_step_runtime", AsyncMock(return_value=runtime))
    return cleanup


@pytest.mark.asyncio
async def test_condition_false_routes_to_silent_complete_without_agent_or_llm(
    tmp_path: object,
) -> None:
    db_engine, session_factory, engine, task, event_bus = await _engine_runtime(tmp_path)
    seen: list[EventType] = []

    async def _capture(event: object) -> None:
        seen.append(event.type)

    event_bus.subscribe_all(_capture)
    workflow = Workflow(
        workflow_id="wf-condition",
        name="Condition",
        steps=[
            StepDefinition(
                name="has_work",
                type="condition",
                condition={"if": "{{ false }}", "then": "respond", "else": "no_op"},
            ),
            StepDefinition(name="respond", type="run", prompt="Use the LLM."),
            StepDefinition(
                name="no_op",
                type="complete",
                complete={
                    "summary": "No actionable work.",
                    "delivery_mode_override": "silent",
                },
            ),
        ],
    )

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.COMPLETED
        assert result.applied_completion_mode == "silent"
        assert result.workflow_state is not None
        assert result.workflow_state.routing_skips == {"respond": "condition:has_work:else"}
        assert engine._agent_loop.run_step.await_count == 0  # noqa: SLF001
        assert engine._providers.llm.await_count == 0  # noqa: SLF001
        assert EventType.FOLLOW_UP_TURN_REQUESTED not in seen
        assert EventType.TASK_COMPLETED in seen

        async with session_factory() as session:
            rows = await list_step_runs_for_task(session, task.task_id)
        assert [(row.step_name, row.status) for row in rows] == [
            ("has_work", "approved"),
            ("no_op", "approved"),
        ]
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_condition_backward_route_uses_explicit_loop_budget(
    tmp_path: object,
) -> None:
    db_engine, _session_factory, engine, task, _event_bus = await _engine_runtime(tmp_path)
    workflow = Workflow(
        workflow_id="wf-condition-budget",
        name="Condition budget",
        steps=[
            StepDefinition(name="implement", type="run", prompt="Implement."),
            StepDefinition(
                name="review_route",
                type="condition",
                condition={
                    "if": "{{ true }}",
                    "then": "implement",
                    "max_loop_iterations": 1,
                    "on_exhausted": "gate",
                },
            ),
        ],
    )
    state = task.workflow_state or WorkflowState()
    state.current_step_index = 1
    state.loop_iterations["condition:review_route->implement"] = 1

    try:
        result = await engine._execute_condition_step(  # noqa: SLF001
            task,
            workflow.steps[1],
            state,
            workflow,
            "step-budget",
            {},
            WorkflowRenderer(),
            {},
        )

        assert result.output.error is not None
        assert result.error_action == "gate"
        assert result.next_step_index == 2
        assert "exhausted after 1 backward iterations" in result.output.error
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_condition_backward_route_preserves_revision_source_context(
    tmp_path: object,
) -> None:
    db_engine, _session_factory, engine, task, _event_bus = await _engine_runtime(tmp_path)
    workflow = Workflow(
        workflow_id="wf-condition-feedback",
        name="Condition feedback",
        steps=[
            StepDefinition(name="implement", type="run", prompt="Implement."),
            StepDefinition(name="review", type="run", prompt="Review."),
            StepDefinition(
                name="review_route",
                type="condition",
                condition={
                    "if": "{{ true }}",
                    "then": "implement",
                    "revision_source": "review",
                    "max_loop_iterations": 2,
                },
            ),
        ],
    )
    state = task.workflow_state or WorkflowState()
    state.current_step_index = 2
    state.step_outputs["review"] = StepOutput(
        summary="Revision required.",
        content="FIX_THE_BOUNDARY_SENTINEL",
        metadata={"decision": "revise"},
    ).model_dump(mode="json")

    try:
        result = await engine._execute_condition_step(  # noqa: SLF001
            task,
            workflow.steps[2],
            state,
            workflow,
            "step-feedback",
            {},
            WorkflowRenderer(),
            {},
        )

        assert result.next_step_index == 0
        assert result.revision_context is not None
        assert "FIX_THE_BOUNDARY_SENTINEL" in result.revision_context
        assert result.route_loop_key == "condition:review_route->implement"
        assert result.route_loop_iterations == 1
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_when_false_persists_skipped_output_and_uses_explicit_next(
    tmp_path: object,
) -> None:
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(tmp_path)
    workflow = Workflow(
        workflow_id="wf-when",
        name="When",
        steps=[
            StepDefinition(
                name="optional_check",
                type="tool_call",
                when="{{ false }}",
                on_skip={
                    "summary": "Check intentionally skipped.",
                    "outputs": {"reason": "disabled"},
                },
                next="done",
                tool_call={"tool": "must_not_execute", "args": {"value": "{{ missing_value }}"}},
            ),
            StepDefinition(name="unused", type="run", prompt="Must not run."),
            StepDefinition(
                name="done",
                type="complete",
                complete={"summary": "Done.", "delivery_mode_override": "silent"},
            ),
        ],
    )

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.COMPLETED
        assert result.workflow_state is not None
        skipped = result.workflow_state.step_outputs["optional_check"]
        assert skipped["summary"] == "Check intentionally skipped."
        assert skipped["metadata"]["skipped"] is True
        assert skipped["outputs"] == {"reason": "disabled"}
        assert result.workflow_state.routing_skips == {"unused": "when:optional_check:false"}
        async with session_factory() as session:
            rows = await list_step_runs_for_task(session, task.task_id)
        assert [(row.step_name, row.status) for row in rows] == [
            ("optional_check", "skipped"),
            ("done", "approved"),
        ]
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_terminal_deterministic_run_is_recovered_without_rendering_again(
    tmp_path: object,
) -> None:
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(tmp_path)
    persisted_output = {
        "summary": "Previously selected done.",
        "content": "",
        "outputs": {},
        "metadata": {
            "deterministic_step": True,
            "step_type": "condition",
            "selected_branch": "then",
            "selected_target": "done",
        },
        "claims": [],
    }
    async with session_factory() as session:
        await create_step_run(
            session,
            task_id=task.task_id,
            step_name="route",
            step_type="condition",
            agent_id=task.agent_id,
            step_run_id="step-persisted-condition",
            status="running",
            runtime_info={
                "deterministic_step": True,
                "deterministic_substate": "persisted",
                "deterministic_generation": 0,
                "terminal_status": "approved",
                "selected_target": "done",
            },
        )
        await update_step_run(
            session,
            "step-persisted-condition",
            output=persisted_output,
        )
        await update_step_run(
            session,
            "step-persisted-condition",
            status="approved",
        )
        await session.commit()
    workflow = Workflow(
        workflow_id="wf-recovery",
        name="Recovery",
        steps=[
            StepDefinition(
                name="route",
                type="condition",
                condition={"if": "{{ undefined_and_must_not_render }}", "then": "done"},
            ),
            StepDefinition(
                name="done",
                type="complete",
                complete={"summary": "Recovered.", "delivery_mode_override": "silent"},
            ),
        ],
    )

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.COMPLETED
        assert result.workflow_state is not None
        assert result.workflow_state.step_outputs["route"]["summary"] == (
            "Previously selected done."
        )
        async with session_factory() as session:
            rows = await list_step_runs_for_task(session, task.task_id)
        assert [row.step_run_id for row in rows if row.step_name == "route"] == [
            "step-persisted-condition"
        ]
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_current_step_run_query_ignores_historical_and_superseded_rows(
    tmp_path: object,
) -> None:
    db_engine, session_factory, _engine, task, _event_bus = await _engine_runtime(tmp_path)
    async with session_factory() as session:
        for step_run_id, attempt_number, attempt, session_id in (
            ("run-historical", 1, 9, "sess-historical"),
            ("run-superseded", 2, 9, "sess-superseded"),
            ("run-current", 2, 2, "sess-current"),
        ):
            await create_step_run(
                session,
                task_id=task.task_id,
                step_name="plan",
                step_type="run",
                agent_id=task.agent_id,
                step_run_id=step_run_id,
                attempt=attempt,
                attempt_number=attempt_number,
                status="approved",
            )
            await update_step_run(session, step_run_id, session_id=session_id)
        superseded = await get_step_run(session, "run-superseded")
        assert superseded is not None
        superseded.superseded_by_step_run_id = "run-current"
        await session.commit()

        selected = await get_latest_step_run_for_task_step(
            session,
            task.task_id,
            "plan",
            attempt_number=2,
            current_revision_only=True,
            eligible_statuses={"approved"},
        )

    try:
        assert selected is not None
        assert selected.step_run_id == "run-current"
        assert selected.session_id == "sess-current"
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_persisted_complete_delivery_override_is_reapplied_during_finalization_recovery(
    tmp_path: object,
) -> None:
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(tmp_path)
    workflow = Workflow(
        workflow_id="wf-delivery-recovery",
        name="Delivery recovery",
        steps=[
            StepDefinition(
                name="done",
                type="complete",
                complete={
                    "summary": "Completed before restart.",
                    "delivery_mode_override": "latest_active_for_agent",
                },
            )
        ],
    )
    state = WorkflowState(
        current_step_index=1,
        step_outputs={
            "done": {
                "summary": "Completed before restart.",
                "content": "",
                "outputs": {},
                "metadata": {
                    "deterministic_step": True,
                    "step_type": "complete",
                    "completion_status": "completed",
                    "delivery_mode_override": "latest_active_for_agent",
                },
                "claims": [],
            }
        },
    )
    task.workflow_state = state
    async with session_factory() as session:
        await update_task_workflow_state(
            session,
            task.task_id,
            state.model_dump(mode="json"),
        )
        await session.commit()

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.COMPLETED
        assert result.delivery.mode == "latest_active_for_agent"
        async with session_factory() as session:
            persisted = await get_task(session, task.task_id)
        assert persisted is not None
        assert persisted.delivery_mode == "latest_active_for_agent"
    finally:
        await db_engine.dispose()


def test_backward_route_reactivates_skips_and_supersedes_outputs() -> None:
    engine = object.__new__(WorkflowEngine)
    workflow = Workflow(
        workflow_id="wf-backward",
        name="Backward",
        steps=[
            StepDefinition(name="start", type="run"),
            StepDefinition(name="fetch", type="tool_call", tool_call={"tool": "read"}),
            StepDefinition(
                name="branch",
                type="condition",
                condition={"if": "{{ true }}", "then": "fetch"},
            ),
        ],
    )
    state = WorkflowState(
        routing_skips={"fetch": "condition:start:false"},
        step_outputs={
            "fetch": {"summary": "stale"},
            "branch": {"summary": "stale"},
        },
    )

    engine._apply_deterministic_route(  # noqa: SLF001
        state,
        workflow,
        source_index=2,
        target_index=1,
        reason="condition:branch:then",
    )

    assert state.routing_skips == {}
    assert state.step_outputs == {}
    assert state.loop_iterations["deterministic_jumps"] == 1
    assert state.loop_iterations["deterministic_generation:fetch"] == 1
    assert state.loop_iterations["deterministic_generation:branch"] == 1


def test_recovered_condition_result_restores_durable_route_state() -> None:
    engine = object.__new__(WorkflowEngine)
    workflow = Workflow(
        workflow_id="wf-recovered-route",
        name="Recovered route",
        steps=[
            StepDefinition(name="implement", type="run"),
            StepDefinition(name="review", type="run"),
            StepDefinition(
                name="route",
                type="condition",
                condition={"if": "{{ true }}", "then": "implement"},
            ),
        ],
    )

    result = engine._deterministic_result_from_persisted(  # noqa: SLF001
        "step-route",
        "approved",
        StepOutput(summary="route").model_dump(mode="json"),
        {
            "selected_target": "implement",
            "route_loop_key": "condition:route->implement",
            "route_loop_iterations": 5,
            "revision_context": "REVIEW_FEEDBACK_SENTINEL",
        },
        workflow,
    )

    assert result.next_step_index == 0
    assert result.route_loop_key == "condition:route->implement"
    assert result.route_loop_iterations == 5
    assert result.revision_context == "REVIEW_FEEDBACK_SENTINEL"


@pytest.mark.parametrize("route_name", ["architect_review_route", "code_review_route"])
@pytest.mark.parametrize("on_exhausted", ["continue", "fail", "gate"])
def test_recovered_exhausted_software_review_route_uses_post_exhaustion_target(
    route_name: str,
    on_exhausted: str,
) -> None:
    engine = object.__new__(WorkflowEngine)
    route_index = next(
        index
        for index, step in enumerate(SOFTWARE_DEVELOPMENT_WORKFLOW.steps)
        if step.name == route_name
    )
    route = SOFTWARE_DEVELOPMENT_WORKFLOW.steps[route_index]
    assert route.condition is not None

    result = engine._deterministic_result_from_persisted(  # noqa: SLF001
        f"step-{route_name}",
        "failed",
        StepOutput(summary="route exhausted", error="loop cap").model_dump(mode="json"),
        {
            "condition_exhausted": True,
            "selected_branch": "exhausted",
            "selected_target": route.condition.then,
            "next_step_index": route_index + 1,
            "error_action": on_exhausted,
        },
        SOFTWARE_DEVELOPMENT_WORKFLOW,
    )

    assert result.step_run_status == "failed"
    assert result.error_action == on_exhausted
    assert result.next_step_index == route_index + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", ["architect_review_route", "code_review_route"])
@pytest.mark.parametrize("on_exhausted", ["continue", "fail", "gate"])
async def test_exhausted_software_review_route_persists_post_exhaustion_target(
    tmp_path: object,
    route_name: str,
    on_exhausted: str,
) -> None:
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(tmp_path)
    workflow = SOFTWARE_DEVELOPMENT_WORKFLOW.model_copy(deep=True)
    route_index = next(
        index for index, step in enumerate(workflow.steps) if step.name == route_name
    )
    route = workflow.steps[route_index]
    assert route.condition is not None
    route.condition.on_exhausted = on_exhausted
    state = WorkflowState(current_step_index=route_index)
    loop_key = f"condition:{route.name}->{route.condition.then}"
    state.loop_iterations[loop_key] = route.condition.max_loop_iterations or 1
    reviewer_name = route.condition.revision_source
    assert reviewer_name is not None
    context = {
        "steps": {
            reviewer_name: {
                "metadata": {
                    "decision": "revise",
                    "must_fix_count": 1,
                    "missing_scope_count": 1,
                    "required_scope_complete": False,
                }
            }
        }
    }
    step_run_id = f"step-{route_name}-{on_exhausted}"
    async with session_factory() as session:
        await create_step_run(
            session,
            task_id=task.task_id,
            step_name=route.name,
            step_type="condition",
            agent_id=task.agent_id,
            step_run_id=step_run_id,
            status="running",
        )
        await session.commit()

    try:
        result = await engine._execute_condition_step(  # noqa: SLF001
            task,
            route,
            state,
            workflow,
            step_run_id,
            {},
            WorkflowRenderer(),
            context,
        )
        async with session_factory() as session:
            persisted = await get_step_run(session, step_run_id)

        assert persisted is not None
        assert persisted.runtime_info["condition_exhausted"] is True
        assert persisted.runtime_info["next_step_index"] == route_index + 1
        assert persisted.runtime_info["error_action"] == on_exhausted
        recovered = engine._deterministic_result_from_persisted(  # noqa: SLF001
            step_run_id,
            persisted.status,
            persisted.output,
            persisted.runtime_info,
            workflow,
        )
        assert result.next_step_index == route_index + 1
        assert recovered.next_step_index == route_index + 1
        assert recovered.error_action == on_exhausted
    finally:
        await db_engine.dispose()


def test_backward_review_route_marks_compact_retry_and_reopens_terminal_todos() -> None:
    engine = object.__new__(WorkflowEngine)
    workflow = SOFTWARE_DEVELOPMENT_WORKFLOW
    route_index = next(
        index for index, step in enumerate(workflow.steps) if step.name == "code_review_route"
    )
    implement_index = next(
        index for index, step in enumerate(workflow.steps) if step.name == "implement"
    )
    state = WorkflowState(
        current_step_index=route_index,
        last_revision_context="Fix the session boundary.",
    )

    engine._apply_deterministic_route(  # noqa: SLF001
        state,
        workflow,
        source_index=route_index,
        target_index=implement_index,
        reason="condition:code_review_route:then",
    )
    reopened = engine._todos_for_evaluation_retry(  # noqa: SLF001
        state,
        [{"content": "Implement the change", "status": "completed"}],
    )

    assert state.last_retry_reason == "routed_revision"
    assert state.loop_iterations["attempts:implement"] == 2
    assert reopened == [
        {
            "content": (
                "Revise this step based on the routed independent review. "
                "Feedback: Fix the session boundary."
            ),
            "status": "pending",
        }
    ]


@pytest.mark.asyncio
async def test_registry_validated_backward_cycle_reactivates_and_stops_at_jump_cap(
    tmp_path: object,
) -> None:
    definition = validate_workflow_definition(
        {
            "workflow_id": "wf-backward-cap",
            "name": "Backward cap",
            "steps": [
                {
                    "name": "first",
                    "type": "condition",
                    "condition": {"if": "true", "then": "second"},
                },
                {
                    "name": "second",
                    "type": "condition",
                    "condition": {"if": "true", "then": "first"},
                },
            ],
        }
    )
    workflow = Workflow.model_validate(definition)
    db_engine, _session_factory, engine, task, _event_bus = await _engine_runtime(tmp_path)
    task.workflow_state.routing_skips = {"first": "stale"}
    task.workflow_state.step_outputs = {"first": {"summary": "stale"}}

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.FAILED
        assert result.workflow_state.routing_skips == {}
        assert result.workflow_state.step_outputs["first"]["summary"].startswith(
            "Condition 'first' selected"
        )
        assert (
            result.workflow_state.loop_iterations["deterministic_jumps"]
            == MAX_DETERMINISTIC_JUMPS + 1
        )
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_read_only_tool_output_drives_condition_and_target_executor(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_output = '{"messages":[{"id":"m1"}]}'
    agent_loop = _DeterministicAgentLoop(
        ToolResult(
            output=raw_output,
            metadata={"_raw_output": raw_output, "executor_id": "executor-b"},
        )
    )
    db_engine, _session_factory, engine, task, _event_bus = await _engine_runtime(
        tmp_path,
        agent_loop=agent_loop,
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="fetch_messages",
                description="Fetch messages.",
                parameters={
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": ["limit"],
                },
                source=ToolSource(type="executor"),
                read_only=True,
            )
        )
    )
    cleanup = AsyncMock()
    runtime = ResolvedStepRuntime(
        tool_registry=registry,
        executor_connection=SimpleNamespace(),
        cleanup=cleanup,
        executor_environment=None,
        runtime_info={"runtime_source": "test"},
        executor_pool=SimpleNamespace(),
        active_executor_id="executor-a",
    )
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
    )
    conversation = SimpleNamespace(conversation_id="conversation-1")
    session = SimpleNamespace(
        session_id="session-1",
        intaris_session_id="intaris-1",
        parent_session_id=None,
        delegation_mode=None,
    )
    monkeypatch.setattr(engine, "_resolve_step_agents", AsyncMock(return_value=(agent, agent)))
    monkeypatch.setattr(
        engine,
        "_reuse_or_create_step_session",
        AsyncMock(return_value=(conversation, session, False)),
    )
    monkeypatch.setattr(engine, "_resolve_step_runtime", AsyncMock(return_value=runtime))
    workflow = Workflow(
        workflow_id="wf-tool",
        name="Tool",
        steps=[
            StepDefinition(
                name="fetch",
                type="tool_call",
                tool_call={
                    "tool": "fetch_messages",
                    "args": {"limit": 10, "target_executor": "executor-b"},
                },
            ),
            StepDefinition(
                name="has_messages",
                type="condition",
                condition={
                    "if": "{{ steps.fetch.outputs.messages | length > 0 }}",
                    "then": "done",
                    "else": "done",
                },
            ),
            StepDefinition(
                name="done",
                type="complete",
                complete={"summary": "Fetched messages.", "delivery_mode_override": "silent"},
            ),
        ],
    )

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.COMPLETED
        assert len(agent_loop.calls) == 1
        assert agent_loop.calls[0].arguments == {
            "limit": 10,
            "target_executor": "executor-b",
        }
        assert result.workflow_state is not None
        assert result.workflow_state.step_outputs["fetch"]["outputs"]["messages"] == [{"id": "m1"}]
        assert result.workflow_state.step_outputs["fetch"]["outputs"]["tool_output_ref"].startswith(
            "det_"
        )
        cleanup.assert_awaited_once()
    finally:
        await db_engine.dispose()


def test_large_tool_output_context_is_bounded_and_keeps_recovery_reference() -> None:
    raw_output = "prefix:" + ("x" * 100_000) + ":unbounded-tail"
    result = ToolResult(
        output="bounded preview",
        metadata={"recovery_call_id": "det_large_output"},
    )

    context = WorkflowEngine._deterministic_tool_result_context(  # noqa: SLF001
        result,
        raw_output=raw_output,
    )
    outputs = WorkflowEngine._default_deterministic_tool_outputs(  # noqa: SLF001
        context,
        result,
    )

    assert len(context["value"].encode()) <= 32_000
    assert "unbounded-tail" not in context["value"]
    assert outputs["tool_output_ref"] == "det_large_output"


@pytest.mark.asyncio
async def test_write_capable_tool_is_default_denied_before_dispatch(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = _DeterministicAgentLoop()
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(
        tmp_path,
        agent_loop=agent_loop,
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="mutate",
                description="Mutate state.",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                read_only=False,
            )
        )
    )
    runtime = ResolvedStepRuntime(
        tool_registry=registry,
        executor_connection=SimpleNamespace(),
        cleanup=AsyncMock(),
        executor_environment=None,
    )
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
    )
    monkeypatch.setattr(engine, "_resolve_step_agents", AsyncMock(return_value=(agent, agent)))
    monkeypatch.setattr(
        engine,
        "_reuse_or_create_step_session",
        AsyncMock(
            return_value=(
                SimpleNamespace(conversation_id="conversation-1"),
                SimpleNamespace(
                    session_id="session-1",
                    intaris_session_id=None,
                    parent_session_id=None,
                    delegation_mode=None,
                ),
                False,
            )
        ),
    )
    monkeypatch.setattr(engine, "_resolve_step_runtime", AsyncMock(return_value=runtime))
    workflow = Workflow(
        workflow_id="wf-deny",
        name="Deny",
        steps=[
            StepDefinition(
                name="mutate",
                type="tool_call",
                tool_call={"tool": "mutate"},
            )
        ],
    )

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.FAILED
        assert agent_loop.calls == []
        async with session_factory() as session:
            rows = await list_step_runs_for_task(session, task.task_id)
            persisted_task = await get_task(session, task.task_id)
        assert rows[0].status == "failed"
        assert "write-capable" in str(rows[0].output["error"])
        assert persisted_task is not None
        assert persisted_task.status == "failed"
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_tool_error_on_error_continue_persists_failure_then_completes(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = _DeterministicAgentLoop(ToolResult(output="upstream unavailable", is_error=True))
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(
        tmp_path,
        agent_loop=agent_loop,
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="fetch",
                description="Fetch state.",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                read_only=True,
            )
        )
    )
    _mock_tool_runtime(engine, monkeypatch, registry)
    workflow = Workflow(
        workflow_id="wf-continue",
        name="Continue",
        steps=[
            StepDefinition(
                name="fetch",
                type="tool_call",
                on_error="continue",
                tool_call={"tool": "fetch"},
            ),
            StepDefinition(
                name="done",
                type="complete",
                complete={"summary": "Handled failure.", "delivery_mode_override": "silent"},
            ),
        ],
    )

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.COMPLETED
        assert len(agent_loop.calls) == 1
        assert result.workflow_state is not None
        assert result.workflow_state.step_outputs["fetch"]["error"] == "upstream unavailable"
        async with session_factory() as session:
            rows = await list_step_runs_for_task(session, task.task_id)
        assert [(row.step_name, row.status) for row in rows] == [
            ("fetch", "failed"),
            ("done", "approved"),
        ]
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_explicit_side_effect_timeout_is_not_replayed_and_marks_ambiguity(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TimeoutAgentLoop(_DeterministicAgentLoop):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = False

        async def execute_controller_tool(self, _ctx: object, tool_call: object) -> ToolResult:
            self.calls.append(tool_call)
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return ToolResult(output="late side effect")

    agent_loop = _TimeoutAgentLoop()
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(
        tmp_path,
        agent_loop=agent_loop,
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="mutate",
                description="Mutate state.",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                read_only=False,
            )
        )
    )
    _mock_tool_runtime(engine, monkeypatch, registry)
    workflow = Workflow(
        workflow_id="wf-timeout",
        name="Timeout",
        steps=[
            StepDefinition(
                name="mutate",
                type="tool_call",
                tool_call={
                    "tool": "mutate",
                    "allow_side_effects": True,
                    "timeout_seconds": 1,
                },
            )
        ],
    )

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.FAILED
        assert len(agent_loop.calls) == 1
        assert agent_loop.cancelled is True
        async with session_factory() as session:
            rows = await list_step_runs_for_task(session, task.task_id)
        assert rows[0].status == "failed"
        assert rows[0].runtime_info["dispatch_timeout"] is True
        assert rows[0].runtime_info["ambiguous_side_effect"] is True
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_transient_executor_unavailability_defers_deterministic_tool_step(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_loop = _DeterministicAgentLoop()
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(
        tmp_path,
        agent_loop=agent_loop,
    )
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
    )
    monkeypatch.setattr(engine, "_resolve_step_agents", AsyncMock(return_value=(agent, agent)))
    monkeypatch.setattr(
        engine,
        "_reuse_or_create_step_session",
        AsyncMock(
            return_value=(
                SimpleNamespace(conversation_id="conversation-1"),
                SimpleNamespace(
                    session_id="session-1",
                    intaris_session_id=None,
                    parent_session_id=None,
                    delegation_mode=None,
                ),
                False,
            )
        ),
    )
    monkeypatch.setattr(
        engine,
        "_resolve_step_runtime",
        AsyncMock(
            side_effect=TransientExecutorUnavailable(
                "executor reconnecting",
                executor_id="executor-a",
                retry_after_seconds=2,
            )
        ),
    )
    workflow = Workflow(
        workflow_id="wf-defer",
        name="Defer",
        steps=[
            StepDefinition(
                name="fetch",
                type="tool_call",
                tool_call={"tool": "fetch"},
            )
        ],
    )

    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == TaskStatus.READY
        assert result.scheduled_for is not None
        assert agent_loop.calls == []
        assert result.workflow_state is not None
        assert result.workflow_state.current_step_index == 0
        async with session_factory() as session:
            persisted = await get_task(session, task.task_id)
            rows = await list_step_runs_for_task(session, task.task_id)
        assert persisted is not None
        assert persisted.status == "ready"
        assert rows[0].runtime_info["deterministic_substate"] == "rendering"
    finally:
        await db_engine.dispose()

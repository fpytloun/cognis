from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.models import PendingPauseResponse
from cognis.api.task_projection import (
    build_task_progress_projection,
    build_task_workflow_projection,
)
from cognis.models.task import TaskModel, TaskStatus
from cognis.models.workflow import (
    CompletionConfig,
    GateConfig,
    StepDefinition,
    Workflow,
    WorkflowPhaseDefinition,
    WorkflowPresentation,
    WorkflowState,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    Base,
    Conversation,
    ConversationTodo,
    ManagedConversationLink,
    Session,
    SessionTodo,
    User,
)


def _workflow(*, presentation: bool = True) -> Workflow:
    return Workflow(
        workflow_id="workflow:test",
        name="Test workflow",
        version=7,
        steps=[
            StepDefinition(
                name="collect",
                type="run",
                completion=CompletionConfig(max_attempts=4),
            ),
            StepDefinition(
                name="review",
                type="gate",
                gate=GateConfig(message="Review"),
            ),
            StepDefinition(name="finish", type="run"),
        ],
        presentation=(
            WorkflowPresentation(
                phases=[
                    WorkflowPhaseDefinition(
                        id="work",
                        title="Work",
                        step_names=["collect"],
                    ),
                    WorkflowPhaseDefinition(
                        id="conclude",
                        title="Conclude",
                        step_names=["review", "finish"],
                    ),
                ]
            )
            if presentation
            else None
        ),
    )


def _task(
    workflow: Workflow,
    *,
    status: TaskStatus = TaskStatus.RUNNING,
    state: WorkflowState | None = None,
) -> TaskModel:
    return TaskModel(
        task_id="task-1",
        title="Task",
        created_by="owner@example.com",
        agent_id="agent-1",
        workflow_id=workflow.workflow_id,
        status=status,
        workflow_state=state or WorkflowState(),
    )


def _step_run(
    step_name: str,
    *,
    status: str,
    attempt: int = 1,
    step_run_id: str | None = None,
    output: dict[str, object] | None = None,
    superseded_by: str | None = None,
    runtime_info: dict[str, object] | None = None,
) -> SimpleNamespace:
    started_at = datetime.now(UTC) - timedelta(seconds=2)
    return SimpleNamespace(
        step_run_id=step_run_id or f"run-{step_name}-{attempt}",
        step_name=step_name,
        status=status,
        attempt=attempt,
        attempt_number=1,
        superseded_by_step_run_id=superseded_by,
        output=output,
        runtime_info=runtime_info
        or {
            "execution_kind": "deterministic",
            "tool_name": "read",
            "secret": "must-not-leak",
        },
        session_id="session-1",
        intaris_session_id="intaris-1",
        deliverable_id="deliverable-1" if output else None,
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=1) if status == "approved" else None,
        updated_at=started_at + timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_projection_uses_pinned_definition_and_latest_attempt() -> None:
    pinned = _workflow()
    state = WorkflowState(
        current_step_index=1,
        effective_workflow_version=7,
        effective_workflow_digest="digest-7",
        effective_workflow_definition=pinned.model_dump(mode="json", exclude_none=True),
    )
    task = _task(pinned, state=state)
    registry = AsyncMock()
    registry.get.return_value = _workflow(presentation=False)

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=registry,
        step_runs=[
            _step_run("collect", status="rejected", attempt=1, superseded_by="run-collect-2"),
            _step_run(
                "collect",
                status="approved",
                attempt=2,
                step_run_id="run-collect-2",
                output={"summary": "Collected", "content": "heavy output"},
            ),
        ],
        pending_pause=None,
    )

    assert projection is not None
    assert registry.get.await_count == 0
    assert projection.workflow_version == 7
    assert projection.workflow_digest == "digest-7"
    assert [phase.id for phase in projection.phases] == ["work", "conclude"]
    collect = projection.phases[0].steps[0]
    assert collect.status == "completed"
    assert collect.attempt_count == 2
    assert collect.max_attempts == 4
    assert collect.summary == "Collected"
    assert collect.has_output is True
    assert collect.has_logs is True
    assert collect.has_deliverable is True
    assert collect.metadata == {"execution_kind": "deterministic", "tool_name": "read"}
    assert "content" not in collect.model_dump()


@pytest.mark.asyncio
async def test_deterministic_projection_exposes_only_safe_bounded_evidence() -> None:
    workflow = _workflow()
    task = _task(workflow)
    runtime_info = {
        "deterministic_substate": "executing",
        "tool_name": "web_fetch",
        "selected_branch": "then",
        "selected_target": "finish",
        "render": {
            "template_digest": "a" * 64,
            "rendered": {
                "url": "https://safe.invalid",
                "api_key": "must-not-leak",
            },
        },
        "condition": {"template_digest": "b" * 64, "rendered": True},
        "call_identity": "must-not-leak",
    }

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=SimpleNamespace(get=AsyncMock(return_value=workflow)),
        step_runs=[_step_run("collect", status="running", runtime_info=runtime_info)],
        pending_pause=None,
    )

    assert projection is not None
    metadata = projection.phases[0].steps[0].metadata
    assert metadata["deterministic_substate"] == "executing"
    assert metadata["selected_branch"] == "then"
    assert metadata["selected_target"] == "finish"
    assert metadata["condition"]["rendered"] is True
    assert metadata["render"] == {
        "template_digest": "a" * 64,
        "rendered_keys": ["api_key", "url"],
        "redacted_keys": [],
    }
    assert "call_identity" not in metadata
    assert "must-not-leak" not in str(metadata)


@pytest.mark.asyncio
async def test_progress_projection_uses_only_explicit_step_session_ownership(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/progress.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    step_run = SimpleNamespace(
        step_run_id="sr-owned",
        step_name="research",
        session_id="session-step",
        superseded_by_step_run_id=None,
        todos=[
            {"content": "Inspect evidence", "status": "in_progress"},
            {"content": "", "status": "pending"},
        ],
        updated_at=now,
    )

    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            await session.flush()
            session.add_all(
                [
                    Agent(
                        agent_id=agent_id,
                        owner_email="owner@example.com",
                        name=agent_id,
                    )
                    for agent_id in ("agent-main", "agent-child", "agent-managed")
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Conversation(
                        conversation_id="conv-step",
                        user_email="owner@example.com",
                        agent_id="agent-main",
                        context_type="task_step",
                    ),
                    Conversation(
                        conversation_id="conv-child",
                        user_email="owner@example.com",
                        agent_id="agent-child",
                        context_type="delegation",
                    ),
                    Conversation(
                        conversation_id="conv-managed",
                        user_email="owner@example.com",
                        agent_id="agent-managed",
                        context_type="agent_direct",
                    ),
                    Conversation(
                        conversation_id="conv-unrelated",
                        user_email="owner@example.com",
                        agent_id="agent-child",
                        context_type="delegation",
                    ),
                    Conversation(
                        conversation_id="conv-managed-idle",
                        user_email="owner@example.com",
                        agent_id="agent-managed",
                        context_type="agent_direct",
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Session(
                        session_id="session-step",
                        conversation_id="conv-step",
                        user_email="owner@example.com",
                        agent_id="agent-main",
                    ),
                    Session(
                        session_id="session-child",
                        conversation_id="conv-child",
                        parent_session_id="session-step",
                        user_email="owner@example.com",
                        agent_id="agent-child",
                        delegation_task="Investigate",
                        status="active",
                    ),
                    Session(
                        session_id="session-unrelated",
                        conversation_id="conv-unrelated",
                        parent_session_id=None,
                        user_email="owner@example.com",
                        agent_id="agent-child",
                        delegation_task="Unrelated source work",
                        status="active",
                    ),
                ]
            )
            await session.flush()
            session.add(
                ManagedConversationLink(
                    link_id="mconv-owned",
                    user_email="owner@example.com",
                    controller_agent_id="agent-main",
                    controller_conversation_id="conv-step",
                    controller_session_id="session-step",
                    target_agent_id="agent-managed",
                    target_conversation_id="conv-managed",
                    target_session_id="session-managed",
                    title="Managed investigation",
                    turn_state="running",
                )
            )
            session.add(
                ManagedConversationLink(
                    link_id="mconv-idle",
                    user_email="owner@example.com",
                    controller_agent_id="agent-main",
                    controller_conversation_id="conv-step",
                    controller_session_id="session-step",
                    target_agent_id="agent-managed",
                    target_conversation_id="conv-managed-idle",
                    target_session_id="session-managed-idle",
                    title="Idle managed investigation",
                    turn_state="idle",
                )
            )
            session.add_all(
                [
                    SessionTodo(
                        session_id="session-child",
                        position=0,
                        content="Check source",
                        status="in_progress",
                    ),
                    *[
                        ConversationTodo(
                            conversation_id="conv-managed",
                            position=position,
                            content=f"Managed todo {position}",
                            status="pending",
                        )
                        for position in range(31)
                    ],
                ]
            )
            await session.commit()

        async with factory() as session:
            projection = await build_task_progress_projection(
                session,
                owner_email="owner@example.com",
                step_runs=[step_run],
            )

        assert projection.todos[0].content == "Inspect evidence"
        assert {item.work_id for item in projection.work_items} == {
            "session-child",
            "mconv-owned",
            "mconv-idle",
        }
        assert all(item.step_run_id == "sr-owned" for item in projection.work_items)
        assert projection.active_count == 2
        assert projection.completed_count == 0
        assert projection.truncated is True
        delegated = next(item for item in projection.work_items if item.kind == "delegated_session")
        assert delegated.todos[0].content == "Check source"
        managed = next(item for item in projection.work_items if item.work_id == "mconv-owned")
        assert len(managed.todos) == 30
        assert "session-unrelated" not in {item.work_id for item in projection.work_items}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_progress_todo_truncation_counts_across_live_step_runs() -> None:
    first = SimpleNamespace(
        step_run_id="sr-first",
        step_name="first",
        session_id=None,
        superseded_by_step_run_id=None,
        todos=[{"content": f"Todo {index}", "status": "pending"} for index in range(100)],
        updated_at=datetime.now(UTC),
    )
    second = SimpleNamespace(
        step_run_id="sr-second",
        step_name="second",
        session_id=None,
        superseded_by_step_run_id=None,
        todos=[{"content": "Overflow", "status": "pending"}],
        updated_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    projection = await build_task_progress_projection(
        AsyncMock(),
        owner_email="owner@example.com",
        step_runs=[first, second],
    )

    assert len(projection.todos) == 100
    assert projection.truncated is True


@pytest.mark.asyncio
async def test_projection_marks_pause_and_phase_waiting() -> None:
    workflow = _workflow()
    task = _task(
        workflow,
        status=TaskStatus.PAUSED,
        state=WorkflowState(
            current_step_index=1,
            status="paused",
            pending_pause_type="gate",
            current_step_status="paused",
        ),
    )
    pause = PendingPauseResponse(
        pause_id="pause-1",
        pause_type="gate",
        task_id=task.task_id,
        step_name="review",
    )

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=SimpleNamespace(get=AsyncMock(return_value=workflow)),
        step_runs=[_step_run("collect", status="approved")],
        pending_pause=pause,
    )

    assert projection is not None
    assert projection.current_phase_id == "conclude"
    assert projection.current_step_name == "review"
    assert projection.phases[1].status == "waiting"
    review = projection.phases[1].steps[0]
    assert review.status == "waiting"
    assert review.action_required is True
    assert review.pause_type == "gate"


@pytest.mark.asyncio
async def test_projection_aggregates_routing_skips_and_terminal_failure() -> None:
    workflow = _workflow()
    task = _task(
        workflow,
        status=TaskStatus.FAILED,
        state=WorkflowState(
            current_step_index=2,
            status="failed",
            routing_skips={"review": "condition:collect:false"},
        ),
    )

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=SimpleNamespace(get=AsyncMock(return_value=workflow)),
        step_runs=[_step_run("collect", status="approved")],
        pending_pause=None,
    )

    assert projection is not None
    assert projection.phases[0].status == "completed"
    assert projection.phases[1].status == "failed"
    review, finish = projection.phases[1].steps
    assert review.status == "skipped"
    assert review.skip_reason == "condition:collect:false"
    assert finish.status == "failed"


@pytest.mark.asyncio
async def test_legacy_projection_uses_current_definition_and_implicit_phase() -> None:
    workflow = _workflow(presentation=False)
    registry = SimpleNamespace(get=AsyncMock(return_value=workflow))
    task = _task(workflow, state=WorkflowState(current_step_index=0))

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=registry,
        step_runs=[],
        pending_pause=None,
    )

    assert projection is not None
    registry.get.assert_awaited_once_with(
        workflow.workflow_id,
        owner_email=task.created_by,
        include_disabled=True,
        project_id=None,
    )
    assert projection.workflow_version == 7
    assert projection.workflow_digest is None
    assert len(projection.phases) == 1
    assert projection.phases[0].id == "workflow"
    assert projection.phases[0].status == "active"
    assert [step.name for step in projection.phases[0].steps] == [
        "collect",
        "review",
        "finish",
    ]


@pytest.mark.asyncio
async def test_workflow_less_task_has_no_projection() -> None:
    task = TaskModel(
        task_id="task-1",
        title="Draft",
        created_by="owner@example.com",
        agent_id="agent-1",
    )
    registry = SimpleNamespace(get=AsyncMock())

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=registry,
        step_runs=[],
        pending_pause=None,
    )

    assert projection is None
    assert registry.get.await_count == 0


@pytest.mark.asyncio
async def test_cancelled_latest_step_marks_owning_phase_cancelled() -> None:
    workflow = _workflow()
    task = _task(
        workflow,
        status=TaskStatus.CANCELLED,
        state=WorkflowState(current_step_index=0, status="cancelled"),
    )

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=SimpleNamespace(get=AsyncMock(return_value=workflow)),
        step_runs=[_step_run("collect", status="paused")],
        pending_pause=None,
    )

    assert projection is not None
    assert projection.phases[0].status == "cancelled"
    assert projection.phases[0].steps[0].status == "cancelled"


@pytest.mark.asyncio
async def test_infrastructure_pause_without_pause_type_is_waiting() -> None:
    workflow = _workflow()
    task = _task(
        workflow,
        status=TaskStatus.PAUSED,
        state=WorkflowState(
            current_step_index=0,
            status="paused",
            current_step_status="paused",
            pending_pause_payload={"reason": "executor_unavailable"},
        ),
    )

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=SimpleNamespace(get=AsyncMock(return_value=workflow)),
        step_runs=[],
        pending_pause=None,
    )

    assert projection is not None
    assert projection.current_step_name == "collect"
    assert projection.phases[0].status == "waiting"
    assert projection.phases[0].steps[0].status == "waiting"
    assert projection.phases[0].steps[0].action_required is True


@pytest.mark.asyncio
async def test_retry_gap_projects_current_rejected_attempt_as_running() -> None:
    workflow = _workflow()
    task = _task(workflow, state=WorkflowState(current_step_index=0, status="running"))

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=SimpleNamespace(get=AsyncMock(return_value=workflow)),
        step_runs=[_step_run("collect", status="rejected")],
        pending_pause=None,
    )

    assert projection is not None
    assert projection.phases[0].status == "active"
    assert projection.phases[0].steps[0].status == "running"


@pytest.mark.asyncio
async def test_exhausted_terminal_step_owns_failed_phase_after_index_advances() -> None:
    workflow = _workflow()
    task = _task(
        workflow,
        status=TaskStatus.FAILED,
        state=WorkflowState(
            current_step_index=len(workflow.steps),
            status="failed",
            skipped_steps=["finish"],
        ),
    )

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=SimpleNamespace(get=AsyncMock(return_value=workflow)),
        step_runs=[
            _step_run("collect", status="approved"),
            _step_run("review", status="skipped"),
        ],
        pending_pause=None,
    )

    assert projection is not None
    assert projection.current_step_name == "finish"
    assert projection.phases[1].status == "failed"
    assert projection.phases[1].steps[0].status == "skipped"
    assert projection.phases[1].steps[1].status == "failed"


@pytest.mark.asyncio
async def test_exhaustion_gate_cancellation_infers_cancelled_phase() -> None:
    workflow = _workflow()
    task = _task(
        workflow,
        status=TaskStatus.CANCELLED,
        state=WorkflowState(
            current_step_index=len(workflow.steps),
            status="cancelled",
        ),
    )

    projection = await build_task_workflow_projection(
        task,
        workflow_registry=SimpleNamespace(get=AsyncMock(return_value=workflow)),
        step_runs=[
            _step_run("collect", status="approved"),
            _step_run("finish", status="rejected"),
        ],
        pending_pause=None,
    )

    assert projection is not None
    assert projection.current_step_name == "finish"
    assert projection.phases[1].status == "cancelled"
    assert projection.phases[1].steps[1].status == "cancelled"

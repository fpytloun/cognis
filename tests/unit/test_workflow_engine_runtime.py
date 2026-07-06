"""Focused workflow engine runtime tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

import cognis.core.workflow_engine as workflow_engine_module
from cognis.core.agent_loop import PauseResolution, PauseWaiter, PendingPause
from cognis.core.runtime import TransientExecutorUnavailable
from cognis.core.workflow_engine import (
    TRANSIENT_EXECUTOR_MAX_DEFERRALS,
    WorkflowEngine,
    _resolve_task_execution_paths,
)
from cognis.core.workflow_registry import SOFTWARE_DEVELOPMENT_WORKFLOW
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext
from cognis.models.task import TaskModel, TaskStatus
from cognis.models.workflow import (
    CompletionConfig,
    GateConfig,
    GateOption,
    OutcomeRoute,
    StepDefinition,
    StepEvaluation,
    StepOutcome,
    StepOutput,
    Workflow,
    WorkflowState,
)
from cognis.runtime_context import (
    current_agent_id,
    current_agent_owner_email,
    current_runtime_access_context,
)


class _SessionFactory:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def __call__(self) -> _SessionFactory:
        return self

    def begin(self) -> _SessionFactory:
        return self

    def add(self, row: object) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        return None

    async def __aenter__(self) -> _SessionFactory:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(scalar_one_or_none=lambda: None)


class _EventBus:
    async def publish(self, _: object) -> None:
        return None


def _build_engine() -> WorkflowEngine:
    async def _refresh_intaris_session_policy(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    return WorkflowEngine(
        session_factory=_SessionFactory(),
        providers=SimpleNamespace(llm=None),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(
            refresh_intaris_session_policy=_refresh_intaris_session_policy
        ),
        event_bus=_EventBus(),
        pause_waiter=PauseWaiter(),
    )


@pytest.mark.asyncio
async def test_create_step_session_passes_task_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    calls: list[dict[str, object]] = []

    async def _get_task(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(active_executor_id="executor-1")

    async def _create_conversation_with_root_session(**kwargs: object) -> tuple[object, object]:
        calls.append(dict(kwargs))
        return SimpleNamespace(conversation_id="conv-1"), SimpleNamespace(session_id="sess-1")

    monkeypatch.setattr("cognis.store.queries.get_task", _get_task)
    monkeypatch.setattr(
        engine._session_manager,
        "create_conversation_with_root_session",
        _create_conversation_with_root_session,
        raising=False,
    )

    await engine._create_step_session(
        TaskModel(
            task_id="task-1",
            title="Task",
            created_by="user@example.com",
            agent_id="agent-1",
            project_id="proj-1",
        ),
        StepDefinition(name="plan", type="run", prompt="Plan"),
        AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
    )

    assert calls
    assert calls[0]["project_id"] == "proj-1"
    assert calls[0]["initial_active_executor_id"] == "executor-1"
    context = cast(ConversationContext, calls[0]["context"])
    assert context.platform_data == {
        "workspace_root": None,
        "working_directory": None,
    }


def test_resolve_task_execution_paths_defaults_unassigned_task_to_executor_home() -> None:
    task = TaskModel(
        task_id="task-1",
        title="Research",
        created_by="user@example.com",
        agent_id="agent-1",
    )

    workspace_root, working_directory = _resolve_task_execution_paths(
        task,
        executor_home="/home/user",
        executor_cwd="/home/user",
    )

    assert workspace_root == "/home/user"
    assert working_directory == "/home/user"


def test_resolve_task_execution_paths_preserves_project_paths() -> None:
    task = TaskModel(
        task_id="task-1",
        title="Project work",
        created_by="user@example.com",
        agent_id="agent-1",
        project_id="proj-1",
        workspace_root="/home/user/src/cognis",
        working_directory="/home/user/src/cognis/ui/src/lib",
    )

    workspace_root, working_directory = _resolve_task_execution_paths(
        task,
        executor_home="/home/user",
        executor_cwd="/home/user",
    )

    assert workspace_root == "/home/user/src/cognis"
    assert working_directory == "/home/user/src/cognis/ui/src/lib"


@pytest.mark.asyncio
async def test_system_step_full_input_does_not_copy_primary_prefix() -> None:
    engine = _build_engine()
    target_session = SimpleNamespace(session_id="architect-session")
    state = WorkflowState(
        step_outputs={
            "plan": {
                "session_id": "plan-session",
                "intaris_session_id": "plan-intaris",
            }
        }
    )
    system_step_agent = AgentDefinition(
        agent_id="system:architect",
        owner_email="system@cognis.local",
        name="Architect",
        agent_type="secondary",
        is_system=True,
    )
    calls: list[dict[str, object]] = []

    async def _fork_session_events(**kwargs: object) -> bool:
        calls.append(dict(kwargs))
        return True

    engine._fork_session_events = _fork_session_events  # type: ignore[method-assign]

    copied = await engine._fork_source_events(
        source_name="plan",
        target_session=target_session,
        state=state,
        copy_prefix=not (
            system_step_agent.is_system or system_step_agent.agent_type == "secondary"
        ),
    )

    assert copied is True
    assert calls == [
        {
            "source_cognis_session_id": "plan-session",
            "source_intaris_session_id": "plan-intaris",
            "target_session": target_session,
            "source_label": "plan",
            "copy_prefix": False,
        }
    ]


class _NotificationService:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.notifications: dict[str, SimpleNamespace] = {}
        self.resolved: list[tuple[str, str, dict[str, object] | None, str | None]] = []

    async def create(self, **kwargs: object) -> object:
        payload = dict(kwargs)
        self.created.append(payload)
        notification_id = str(payload.get("notification_id"))
        self.notifications[notification_id] = SimpleNamespace(
            notification_id=notification_id,
            status="pending",
            resolution=None,
        )
        return SimpleNamespace()

    async def get(self, notification_id: str) -> object | None:
        return self.notifications.get(notification_id)

    async def resolve(
        self,
        notification_id: str,
        decision: str,
        data: dict[str, object] | None = None,
        *,
        user_email: str | None = None,
    ) -> bool:
        self.resolved.append((notification_id, decision, data, user_email))
        notification = self.notifications.get(notification_id)
        if notification is not None:
            notification.status = "resolved"
            notification.resolution = {"decision": decision, **(data or {})}
        return True


def test_build_step_task_context_includes_operator_instruction() -> None:
    engine = _build_engine()

    task_context = engine._build_step_task_context(
        TaskModel(
            task_id="task-1",
            title="Task",
            description="Build feature",
            expected_output="Concrete implementation plan with tests.",
            created_by="user@example.com",
            agent_id="agent-1",
        ),
        WorkflowState(last_operator_instruction="Incorporate the review and continue."),
    )

    assert "Task title: Task" in task_context
    assert "Task description: Build feature" in task_context
    assert "Expected output: Concrete implementation plan with tests." in task_context
    assert "Build feature" in task_context
    assert "Operator instruction for this step" in task_context
    assert "Incorporate the review and continue." in task_context


@pytest.mark.asyncio
async def test_handle_step_outcome_routes_rejected_step_to_prior_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Test Workflow",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(
                name="architect_review",
                type="run",
                outcome_routes=[
                    OutcomeRoute(
                        status="rejected",
                        action="revise(plan)",
                        max_loop_iterations=3,
                        on_exhausted="gate",
                    )
                ],
            ),
        ],
    )
    state = WorkflowState(current_step_index=1, loop_iterations={"attempts:plan": 2})
    step_result = StepOutput(
        summary="review complete",
        content="Full architect review with verdict REQUEST REWORK",
        outputs={"verdict": "REQUEST REWORK"},
        claims=["Completed review"],
        outcome=StepOutcome(status="rejected", reason="Plan needs revision."),
    )
    persisted: list[int] = []

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persisted.append(state.current_step_index)

    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)

    result = await engine._handle_step_outcome(
        task,
        workflow.steps[1],
        step_result,
        state,
        workflow,
    )

    assert result == "routed"
    assert state.current_step_index == 0
    assert "attempts:plan" not in state.loop_iterations
    assert state.last_evaluation_feedback == "Plan needs revision."
    assert state.last_revision_context is not None
    assert "Reviewer Output:" in state.last_revision_context
    assert "REQUEST REWORK" in state.last_revision_context
    assert persisted == [0]


@pytest.mark.asyncio
async def test_handle_step_outcome_revise_route_respects_loop_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Test Workflow",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(
                name="architect_review",
                type="run",
                outcome_routes=[
                    OutcomeRoute(
                        status="rejected",
                        action="revise(plan)",
                        max_loop_iterations=1,
                        on_exhausted="gate",
                    )
                ],
            ),
        ],
    )
    state = WorkflowState(
        current_step_index=1, loop_iterations={"outcome:architect_review:revise(plan)": 1}
    )
    step_result = StepOutput(
        summary="review complete",
        outputs={},
        claims=["Completed review"],
        outcome=StepOutcome(status="rejected", reason="Needs changes."),
    )
    exhausted_calls: list[tuple[str, str | None]] = []

    async def _handle_exhausted(*args: object, **kwargs: object) -> bool:
        exhausted_calls.append((str(args[4]), kwargs.get("last_error")))
        return True

    monkeypatch.setattr(engine, "_handle_exhausted", _handle_exhausted)

    result = await engine._handle_step_outcome(
        task,
        workflow.steps[1],
        step_result,
        state,
        workflow,
    )

    assert result == "routed"
    assert exhausted_calls == [("gate", "Needs changes.")]


@pytest.mark.asyncio
async def test_handle_step_outcome_defaults_failed_without_route() -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Test Workflow",
        steps=[StepDefinition(name="commit", type="run")],
    )
    state = WorkflowState(current_step_index=0)
    step_result = StepOutput(
        summary="commit blocked",
        outputs={},
        claims=["Could not create the commit"],
        outcome=StepOutcome(status="failed", reason="git identity missing"),
    )

    result = await engine._handle_step_outcome(
        task, workflow.steps[0], step_result, state, workflow
    )

    assert result == "failed"


@pytest.mark.asyncio
async def test_build_result_data_uses_final_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        status="completed",
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Deliverable Workflow",
        steps=[
            StepDefinition(name="plan", type="run", require_deliverable=True),
            StepDefinition(name="final_summary", type="run", require_deliverable=True),
        ],
    )
    state = WorkflowState(
        step_outputs={
            "plan": {"summary": "planned", "deliverable_id": "dlv-plan"},
            "final_summary": {"summary": "done", "deliverable_id": "dlv-final"},
        }
    )

    async def _get_deliverable(_session: object, deliverable_id: str) -> SimpleNamespace | None:
        if deliverable_id != "dlv-final":
            return None
        return SimpleNamespace(
            deliverable_id="dlv-final",
            step_run_id="sr-final",
            version=2,
            content="# Final result",
            format="markdown",
            title="Final summary",
            target="channel",
            outputs={"tests": "passed"},
            status="approved",
            evaluator_feedback=None,
            created_at=None,
            updated_at=None,
        )

    monkeypatch.setattr("cognis.core.workflow_engine.get_deliverable", _get_deliverable)

    result = await engine._build_result_data(task, state, workflow)

    assert result is not None
    assert result["final_deliverable_id"] == "dlv-final"
    assert result["final_content"] == "# Final result"


@pytest.mark.asyncio
async def test_software_development_post_review_gate_pauses_for_should_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    notifications = _NotificationService()
    engine._notification_service = notifications

    task = TaskModel(
        task_id="task-1",
        title="Feature",
        created_by="user@example.com",
        agent_id="agent-1",
        status="running",
    )
    state = WorkflowState(
        step_outputs={
            "plan": {"summary": "planned", "metadata": {}},
            "implement": {"summary": "implemented", "metadata": {}},
            "code_review": {
                "summary": "reviewed with non-blocking follow-up",
                "metadata": {
                    "verdict": "approve",
                    "required_scope_complete": True,
                    "missing_scope_count": 0,
                    "must_fix_count": 0,
                    "should_fix_count": 1,
                },
            },
        }
    )
    gate_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "post_review_gate"
    )

    persisted_statuses: list[str] = []

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persisted_statuses.append(state.status)

    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)

    async def _resolve_soon() -> None:
        while not state.pending_pause_payload:
            await asyncio.sleep(0.01)
        pause_id = str(state.pending_pause_payload["pause_id"])
        while engine._pause_waiter.get(pause_id) is None:
            await asyncio.sleep(0.01)
        engine._pause_waiter.resolve(pause_id, PauseResolution(decision="continue"))

    asyncio.create_task(_resolve_soon())
    result = await engine._handle_gate_step(task, gate_step, state, SOFTWARE_DEVELOPMENT_WORKFLOW)

    assert result == "continue"
    assert "paused" in persisted_statuses
    assert notifications.created
    assert notifications.created[0]["step_name"] == "post_review_gate"


@pytest.mark.asyncio
async def test_post_review_gate_revise_sets_revision_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    notifications = _NotificationService()
    engine._notification_service = notifications

    task = TaskModel(
        task_id="task-1",
        title="Feature",
        created_by="user@example.com",
        agent_id="agent-1",
        status="running",
    )
    state = WorkflowState(
        step_outputs={
            "code_review": {
                "summary": "reviewed with missing UI",
                "metadata": {
                    "verdict": "approve",
                    "required_scope_complete": True,
                    "missing_scope_count": 0,
                    "must_fix_count": 0,
                    "should_fix_count": 1,
                },
            },
        }
    )
    gate_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "post_review_gate"
    )

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)

    async def _resolve_soon() -> None:
        while not state.pending_pause_payload:
            await asyncio.sleep(0.01)
        pause_id = str(state.pending_pause_payload["pause_id"])
        while engine._pause_waiter.get(pause_id) is None:
            await asyncio.sleep(0.01)
        engine._pause_waiter.resolve(
            pause_id,
            PauseResolution(
                decision="revise(implement)",
                data={"note": "Fix the missing UI test before commit."},
            ),
        )

    asyncio.create_task(_resolve_soon())
    result = await engine._handle_gate_step(task, gate_step, state, SOFTWARE_DEVELOPMENT_WORKFLOW)

    assert result == "revise(implement)"
    assert state.last_revision_context is not None
    assert "post_review_gate" in state.last_revision_context
    assert "unresolved scope or review findings" in state.last_revision_context
    assert "should_fix_count" in state.last_revision_context
    assert "Fix the missing UI test before commit." in state.last_revision_context


@pytest.mark.asyncio
async def test_handle_gate_step_reuses_existing_pause_id(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _build_engine()
    notifications = _NotificationService()
    engine._notification_service = notifications
    notifications.notifications["gate_existing"] = SimpleNamespace(
        notification_id="gate_existing",
        status="pending",
        resolution=None,
    )

    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        status="paused",
    )
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[])
    state = WorkflowState(
        status="paused",
        current_step_status="paused",
        pending_pause_type="gate",
        pending_pause_payload={
            "pause_id": "gate_existing",
            "task_id": "task-1",
            "step_name": "review",
            "message": "Review it",
            "context": {},
            "options": [{"label": "Continue", "action": "continue"}],
        },
    )
    step_def = StepDefinition(
        name="review",
        type="gate",
        gate=GateConfig(
            message="Review it",
            options=[GateOption(label="Continue", action="continue")],
        ),
    )

    persisted: list[str] = []

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persisted.append(
            str(state.pending_pause_payload.get("pause_id")) if state.pending_pause_payload else ""
        )

    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)
    engine._pause_waiter.register(
        PendingPause(
            pause_id="gate_existing",
            pause_type="gate",
            task_id="task-1",
            step_name="review",
            options=[{"label": "Continue", "action": "continue"}],
        )
    )

    async def _resolve_soon() -> None:
        await asyncio.sleep(0.01)
        engine._pause_waiter.resolve("gate_existing", PauseResolution(decision="continue"))

    asyncio.create_task(_resolve_soon())
    result = await engine._handle_gate_step(task, step_def, state, workflow)

    assert result == "continue"
    assert notifications.created == []
    assert persisted[0] == "gate_existing"


@pytest.mark.asyncio
async def test_handle_gate_step_timeout_defaults_to_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _build_engine()
    notifications = _NotificationService()
    engine._notification_service = notifications

    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
    )
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[])
    state = WorkflowState()
    step_def = StepDefinition(
        name="review",
        type="gate",
        gate=GateConfig(
            message="Review it",
            timeout_seconds=1,
            options=[GateOption(label="Continue", action="continue")],
        ),
    )

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _timeout(*args: object, **kwargs: object) -> PauseResolution:
        del args, kwargs
        raise TimeoutError

    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)
    monkeypatch.setattr(engine._pause_waiter, "wait", _timeout)

    result = await engine._handle_gate_step(task, step_def, state, workflow)

    assert result == "fail"
    assert len(notifications.created) == 1
    assert notifications.resolved == [
        (
            notifications.created[0]["notification_id"],
            "fail",
            {"reason": "timeout"},
            "user@example.com",
        )
    ]


@pytest.mark.asyncio
async def test_handle_gate_step_recreates_missing_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    notifications = _NotificationService()
    engine._notification_service = notifications

    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        status="paused",
        source_type="api",
    )
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[])
    state = WorkflowState(
        status="paused",
        current_step_status="paused",
        pending_pause_type="gate",
        pending_pause_payload={
            "pause_id": "gate_missing",
            "task_id": "task-1",
            "step_name": "review",
        },
    )
    step_def = StepDefinition(
        name="review",
        type="gate",
        gate=GateConfig(
            message="Review it",
            options=[GateOption(label="Continue", action="continue")],
        ),
    )

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _resolve_soon() -> None:
        await asyncio.sleep(0.01)
        engine._pause_waiter.resolve("gate_missing", PauseResolution(decision="continue"))

    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)
    asyncio.create_task(_resolve_soon())

    result = await engine._handle_gate_step(task, step_def, state, workflow)

    assert result == "continue"
    assert len(notifications.created) == 1
    assert notifications.created[0]["notification_id"] == "gate_missing"


@pytest.mark.asyncio
async def test_handle_gate_step_uses_resolved_notification_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    notifications = _NotificationService()
    engine._notification_service = notifications
    notifications.notifications["gate_resolved"] = SimpleNamespace(
        notification_id="gate_resolved",
        status="resolved",
        resolution={"decision": "continue", "note": "Use the approved version."},
    )

    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        status="paused",
    )
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[])
    state = WorkflowState(
        status="paused",
        current_step_status="paused",
        pending_pause_type="gate",
        pending_pause_payload={
            "pause_id": "gate_resolved",
            "task_id": "task-1",
            "step_name": "review",
        },
    )
    step_def = StepDefinition(
        name="review",
        type="gate",
        gate=GateConfig(
            message="Review it",
            options=[GateOption(label="Continue", action="continue")],
        ),
    )
    persisted: list[str | None] = []

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persisted.append(state.pending_pause_type)

    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)

    result = await engine._handle_gate_step(task, step_def, state, workflow)

    assert result == "continue"
    assert notifications.created == []
    assert state.pending_pause_type is None
    assert state.last_operator_instruction == "Use the approved version."
    assert persisted == [None]


@pytest.mark.asyncio
async def test_has_prior_step_session_accepts_completed_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()

    async def _latest_step_run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(session_id="sess-1")

    async def _get_session_row(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr("cognis.store.queries.get_session_row", _get_session_row)

    reusable = await engine._has_prior_step_session(
        TaskModel(
            task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
        ),
        StepDefinition(name="plan", type="run"),
    )

    assert reusable is True


@pytest.mark.asyncio
async def test_has_prior_step_session_accepts_idle_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()

    async def _latest_step_run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(session_id="sess-1")

    async def _get_session_row(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(status="idle")

    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr("cognis.store.queries.get_session_row", _get_session_row)

    reusable = await engine._has_prior_step_session(
        TaskModel(
            task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
        ),
        StepDefinition(name="plan", type="run"),
    )

    assert reusable is True


@pytest.mark.asyncio
async def test_reuse_or_create_step_session_resumes_completed_step_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    resumed_session = SimpleNamespace(session_id="sess-2", intaris_session_id="sess-2")
    fork_calls: list[tuple[str | None, str | None, str]] = []

    async def _latest_step_run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            step_run_id="run-1",
            session_id="sess-1",
            intaris_session_id="sess-1",
        )

    async def _get_session_row(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            session_id="sess-1",
            conversation_id="conv-1",
            status="completed",
            intaris_session_id="sess-1",
        )

    async def _get_conversation(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            title="Task: Task / Step: plan",
            title_source="manual",
            context_type="task",
            context_ref="task-1",
            context_data={},
            memory_labels={},
            active_session_id="sess-1",
            status="active",
            last_message_at=None,
            created_at=None,
            updated_at=None,
        )

    async def _create_root_session(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return resumed_session

    async def _fork_session_events(**kwargs: object) -> bool:
        fork_calls.append(
            (
                str(kwargs.get("source_cognis_session_id") or "") or None,
                str(kwargs.get("source_intaris_session_id") or "") or None,
                str(kwargs.get("source_label") or ""),
            )
        )
        return True

    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr("cognis.store.queries.get_session_row", _get_session_row)
    monkeypatch.setattr("cognis.store.queries.get_conversation", _get_conversation)
    monkeypatch.setattr(
        engine._session_manager, "create_root_session", _create_root_session, raising=False
    )
    monkeypatch.setattr(engine, "_fork_session_events", _fork_session_events)

    conversation, session, seeded = await engine._reuse_or_create_step_session(
        TaskModel(
            task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
        ),
        StepDefinition(name="plan", type="run", prompt="Plan"),
        AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
    )

    assert conversation.conversation_id == "conv-1"
    assert session is resumed_session
    assert seeded is True
    assert fork_calls == [("sess-1", "sess-1", "plan:resume")]


@pytest.mark.asyncio
async def test_reuse_or_create_step_session_reports_unseeded_resume_when_fork_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    resumed_session = SimpleNamespace(session_id="sess-2", intaris_session_id="sess-2")

    async def _latest_step_run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            step_run_id="run-1",
            session_id="sess-1",
            intaris_session_id="sess-1",
        )

    async def _get_session_row(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            session_id="sess-1",
            conversation_id="conv-1",
            status="completed",
            intaris_session_id="sess-1",
        )

    async def _get_conversation(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            conversation_id="conv-1",
            user_email="user@example.com",
            agent_id="agent-1",
            title="Task: Task / Step: plan",
            title_source="manual",
            context_type="task",
            context_ref="task-1",
            context_data={},
            memory_labels={},
            active_session_id="sess-1",
            status="active",
            last_message_at=None,
            created_at=None,
            updated_at=None,
        )

    async def _create_root_session(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return resumed_session

    async def _fork_session_events(**kwargs: object) -> bool:
        del kwargs
        return False

    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr("cognis.store.queries.get_session_row", _get_session_row)
    monkeypatch.setattr("cognis.store.queries.get_conversation", _get_conversation)
    monkeypatch.setattr(
        engine._session_manager, "create_root_session", _create_root_session, raising=False
    )
    monkeypatch.setattr(engine, "_fork_session_events", _fork_session_events)

    _, _, seeded = await engine._reuse_or_create_step_session(
        TaskModel(
            task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
        ),
        StepDefinition(name="plan", type="run", prompt="Plan"),
        AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
    )

    assert seeded is False


@pytest.mark.asyncio
async def test_execute_workflow_preserves_cancelled_status_from_outcome_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        workflow_state=WorkflowState(),
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Test Workflow",
        steps=[
            StepDefinition(
                name="commit",
                type="run",
                outcome_routes=[OutcomeRoute(status="failed", action="gate")],
            )
        ],
    )
    step_result = StepOutput(
        summary="commit blocked",
        outputs={},
        claims=["Could not create the commit"],
        outcome=StepOutcome(status="failed", reason="git identity missing"),
        session_id="sess-1",
        intaris_session_id="sess-1",
    )

    async def _execute_run_step(*args: object, **kwargs: object) -> StepOutput:
        del args, kwargs
        return step_result

    async def _mark_completed(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    async def _persist_state(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _persist_final(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _cleanup(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _deliver(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _evaluate_step(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            decision="approved", model_dump=lambda mode=None: {"decision": "approved"}
        )

    async def _handle_gate_step(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "cancel"

    async def _latest_step_run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(step_run_id="sr-1")

    async def _update_step_run(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    monkeypatch.setattr(engine, "_execute_run_step", _execute_run_step)
    monkeypatch.setattr(engine._session_manager, "mark_completed", _mark_completed, raising=False)
    monkeypatch.setattr(engine, "_persist_workflow_state", _persist_state)
    monkeypatch.setattr(engine, "_persist_task_final", _persist_final)
    monkeypatch.setattr(engine, "_build_result_data", _persist_final)
    monkeypatch.setattr(engine, "_cleanup_step_sessions", _cleanup)
    monkeypatch.setattr(engine, "_deliver_task_result", _deliver)
    monkeypatch.setattr(engine, "_evaluate_step", _evaluate_step)
    monkeypatch.setattr(engine, "_handle_gate_step", _handle_gate_step)
    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr("cognis.core.workflow_engine.update_step_run", _update_step_run)

    result = await engine.execute_workflow(task, workflow)

    assert result.status == "cancelled"
    assert result.workflow_state is not None
    assert result.workflow_state.status == "cancelled"


@pytest.mark.asyncio
async def test_handle_exhausted_gate_cancel_marks_task_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
    )
    workflow = Workflow(
        workflow_id="wf:test", name="Test", steps=[StepDefinition(name="plan", type="run")]
    )
    state = WorkflowState(current_step_index=0)

    async def _handle_gate_step(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "cancel"

    persisted: list[tuple[str, int]] = []

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persisted.append((state.status, state.current_step_index))

    monkeypatch.setattr(engine, "_handle_gate_step", _handle_gate_step)
    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)

    handled = await engine._handle_exhausted(task, workflow.steps[0], state, workflow, "gate")

    assert handled is True
    assert state.status == "cancelled"
    assert state.current_step_index == len(workflow.steps)
    assert persisted == [("cancelled", len(workflow.steps))]


@pytest.mark.asyncio
async def test_handle_exhausted_gate_continue_does_not_mark_step_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
    )
    workflow = Workflow(
        workflow_id="wf:test", name="Test", steps=[StepDefinition(name="plan", type="run")]
    )
    state = WorkflowState(current_step_index=0, skipped_steps=["architect_review"])

    async def _handle_gate_step(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "continue"

    persisted: list[tuple[list[str], int]] = []

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persisted.append((list(state.skipped_steps), state.current_step_index))

    monkeypatch.setattr(engine, "_handle_gate_step", _handle_gate_step)
    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)

    handled = await engine._handle_exhausted(task, workflow.steps[0], state, workflow, "gate")

    assert handled is True
    assert state.current_step_index == len(workflow.steps)
    assert state.skipped_steps == ["architect_review"]
    assert persisted == [(["architect_review"], len(workflow.steps))]


@pytest.mark.asyncio
async def test_handle_exhausted_continue_promotes_rejected_deliverable_into_step_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1", title="Task", created_by="user@example.com", agent_id="agent-1"
    )
    workflow = Workflow(
        workflow_id="wf:test", name="Test", steps=[StepDefinition(name="plan", type="run")]
    )
    state = WorkflowState(current_step_index=0)

    async def _latest_step_run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(step_run_id="sr-1", output={"summary": "Old summary"})

    async def _latest_rejected(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            deliverable_id="dlv-1",
            version=3,
            format="markdown",
            title="Recovered summary",
            content="Recovered deliverable body",
            evaluator_feedback="Needs work",
        )

    async def _update_deliverable_status(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    async def _update_step_run(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    async def _persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_rejected_deliverable_for_step_run",
        _latest_rejected,
    )
    monkeypatch.setattr(
        "cognis.core.workflow_engine.update_deliverable_status",
        _update_deliverable_status,
    )
    monkeypatch.setattr("cognis.core.workflow_engine.update_step_run", _update_step_run)
    monkeypatch.setattr(engine, "_persist_workflow_state", _persist)

    handled = await engine._handle_exhausted(task, workflow.steps[0], state, workflow, "continue")

    assert handled is True
    assert state.step_outputs["plan"]["deliverable_id"] == "dlv-1"
    assert state.step_outputs["plan"]["content"] == "Recovered deliverable body"


@pytest.mark.asyncio
async def test_execute_workflow_updates_current_step_run_id_after_retry_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        workflow_id="wf:test",
        workflow_state=WorkflowState(current_step_index=0),
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Test",
        steps=[StepDefinition(name="implement", type="run", completion={"evaluate": True})],
    )
    updated_ids: list[str] = []

    async def _execute_run_step(*args: object, **kwargs: object):
        del args, kwargs
        return StepOutput(summary="done", content="done", execution_evidence={}), "sr-current"

    async def _evaluate_step(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            decision="approved", model_dump=lambda mode=None: {"decision": "approved"}
        )

    async def _update_step_run(_session: object, step_run_id: str, **kwargs: object) -> bool:
        if kwargs.get("evaluation") is not None:
            updated_ids.append(step_run_id)
        return True

    async def _noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(engine, "_execute_run_step", _execute_run_step)
    monkeypatch.setattr(engine, "_evaluate_step", _evaluate_step)
    monkeypatch.setattr(engine, "_persist_workflow_state", _noop)
    monkeypatch.setattr(engine, "_persist_task_final", _noop)
    monkeypatch.setattr(engine, "_build_result_data", _noop)
    monkeypatch.setattr(engine, "_cleanup_step_sessions", _noop)
    monkeypatch.setattr(engine, "_deliver_task_result", _noop)
    monkeypatch.setattr("cognis.core.workflow_engine.update_step_run", _update_step_run)
    monkeypatch.setattr(engine._session_manager, "mark_completed", _noop, raising=False)

    result = await engine.execute_workflow(task, workflow)

    assert result.status == "completed"
    assert updated_ids == ["sr-current"]


@pytest.mark.asyncio
async def test_execute_workflow_fails_when_active_runtime_exceeds_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-timeout",
        title="Timeout",
        created_by="user@example.com",
        agent_id="agent-1",
        workflow_id="wf:test",
        workflow_state=WorkflowState(current_step_index=0),
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Timeout Workflow",
        steps=[StepDefinition(name="execute", type="run")],
    )

    async def _execute_run_step(*args: object, **kwargs: object):
        del args, kwargs
        await asyncio.sleep(0.05)
        return StepOutput(summary="done", content="done"), "sr-timeout"

    async def _noop(*args: object, **kwargs: object) -> None:
        return None

    async def _fail_running_step_runs_for_task(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(engine, "_execute_run_step", _execute_run_step)
    monkeypatch.setattr(engine, "_persist_task_final", _noop)
    monkeypatch.setattr(engine, "_cleanup_step_sessions", _noop)
    monkeypatch.setattr(engine, "_deliver_task_result", _noop)
    monkeypatch.setattr(
        "cognis.core.workflow_engine.fail_running_step_runs_for_task",
        _fail_running_step_runs_for_task,
    )
    monkeypatch.setattr("cognis.core.workflow_engine.DEFAULT_MAX_WORKFLOW_SECONDS", 0.01)

    result = await engine.execute_workflow(task, workflow)

    assert result.status == "failed"
    assert result.workflow_state is not None
    assert result.workflow_state.status == "failed"


@pytest.mark.asyncio
async def test_execute_run_step_marks_step_run_failed_when_agent_loop_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        workflow_state=WorkflowState(),
        workspace_root="/workspace",
        working_directory="/workspace",
    )
    step_def = StepDefinition(name="execute", type="run", prompt="Do work")
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[step_def])
    conversation = SimpleNamespace(conversation_id="conv-1")
    session = SimpleNamespace(
        session_id="sess-1",
        intaris_session_id="sess-1",
        user_email="user@example.com",
        parent_session_id=None,
        delegation_mode="primary",
    )
    updated_statuses: list[tuple[str, str]] = []

    async def _resolve_step_agents(
        *args: object, **kwargs: object
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        del args, kwargs
        agent = SimpleNamespace(
            agent_id="agent-1",
            agent_type="primary",
            owner_email="user@example.com",
            is_system=False,
            agent_profiles={},
            default_agent_profile_id=None,
            llm_config=None,
        )
        return agent, agent

    async def _reuse_or_create(*args: object, **kwargs: object):
        del args, kwargs
        return conversation, session, False

    async def _create_step_session(*args: object, **kwargs: object):
        del args, kwargs
        return conversation, session

    async def _resolve_runtime(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            tool_registry=None,
            executor_connection=None,
            executor_environment=None,
            runtime_info=None,
            cleanup=lambda: asyncio.sleep(0),
        )

    async def _latest_step_run(*args: object, **kwargs: object):
        del args, kwargs
        return None

    async def _create_step_run(*args: object, **kwargs: object):
        del args, kwargs
        return SimpleNamespace()

    async def _update_step_run(_session: object, step_run_id: str, **kwargs: object) -> bool:
        status = kwargs.get("status")
        if isinstance(status, str):
            updated_statuses.append((step_run_id, status))
        return True

    async def _run_step(*args: object, **kwargs: object):
        del args, kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "_resolve_step_agents", _resolve_step_agents)
    monkeypatch.setattr(engine, "_reuse_or_create_step_session", _reuse_or_create)
    monkeypatch.setattr(engine, "_create_step_session", _create_step_session)
    monkeypatch.setattr(engine, "_resolve_step_runtime", _resolve_runtime)
    monkeypatch.setattr(engine._agent_loop, "run_step", _run_step, raising=False)
    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr("cognis.core.workflow_engine.create_step_run", _create_step_run)
    monkeypatch.setattr("cognis.core.workflow_engine.update_step_run", _update_step_run)

    with pytest.raises(RuntimeError, match="boom"):
        await engine._execute_run_step(
            task, step_def, task.workflow_state or WorkflowState(), workflow
        )

    assert ("sr_" in updated_statuses[0][0]) is True
    assert updated_statuses[-1][1] == "failed"


@pytest.mark.asyncio
async def test_execute_run_step_refreshes_intaris_policy_with_executor_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        workflow_state=WorkflowState(),
    )
    step_def = StepDefinition(name="execute", type="run", prompt="Do work")
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[step_def])
    conversation = SimpleNamespace(conversation_id="conv-1")
    session = SimpleNamespace(
        session_id="sess-1",
        intaris_session_id="sess-1",
        user_email="user@example.com",
        parent_session_id=None,
        delegation_mode="primary",
    )
    refreshed: list[tuple[str | None, str | None]] = []

    async def _resolve_step_agents(
        *args: object, **kwargs: object
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        del args, kwargs
        agent = SimpleNamespace(
            agent_id="agent-1",
            agent_type="primary",
            owner_email="user@example.com",
            is_system=False,
            agent_profiles={},
            default_agent_profile_id=None,
            llm_config=None,
        )
        return agent, agent

    async def _create_step_session(*args: object, **kwargs: object):
        del args, kwargs
        return conversation, session

    async def _resolve_runtime(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            tool_registry=None,
            executor_connection=None,
            executor_environment=SimpleNamespace(home="/home/user", cwd="/home/user/src/cognis"),
            runtime_info=None,
            cleanup=lambda: asyncio.sleep(0),
        )

    async def _latest_step_run(*args: object, **kwargs: object):
        del args, kwargs
        return None

    async def _create_step_run(*args: object, **kwargs: object):
        del args, kwargs
        return SimpleNamespace()

    async def _update_step_run(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    async def _run_step(*args: object, **kwargs: object) -> StepOutput:
        del args, kwargs
        return StepOutput(summary="done", content="done")

    async def _refresh(_session: object) -> None:
        del _session
        refreshed.append((task.workspace_root, task.working_directory))

    monkeypatch.setattr(engine, "_resolve_step_agents", _resolve_step_agents)
    monkeypatch.setattr(engine, "_create_step_session", _create_step_session)
    monkeypatch.setattr(engine, "_resolve_step_runtime", _resolve_runtime)
    monkeypatch.setattr(engine._agent_loop, "run_step", _run_step, raising=False)
    monkeypatch.setattr(engine._session_manager, "refresh_intaris_session_policy", _refresh)
    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr("cognis.core.workflow_engine.create_step_run", _create_step_run)
    monkeypatch.setattr("cognis.core.workflow_engine.update_step_run", _update_step_run)

    output, _step_run_id = await engine._execute_run_step(
        task, step_def, task.workflow_state or WorkflowState(), workflow
    )

    assert output is not None
    assert refreshed == [("/home/user/src/cognis", "/home/user/src/cognis")]


@pytest.mark.asyncio
async def test_execute_run_step_scopes_runtime_context_to_executor_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-b",
        workflow_state=WorkflowState(),
    )
    step_def = StepDefinition(
        name="implement",
        type="run",
        prompt="Do work",
        agent_override="system:implement",
    )
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[step_def])
    conversation = SimpleNamespace(conversation_id="conv-1")
    session = SimpleNamespace(
        session_id="sess-1",
        intaris_session_id="sess-1",
        user_email="user@example.com",
        parent_session_id=None,
        delegation_mode="primary",
    )
    primary_agent = AgentDefinition(
        agent_id="agent-b",
        owner_email="user@example.com",
        name="Agent B",
        agent_type="primary",
    )
    step_agent = AgentDefinition(
        agent_id="system:implement",
        owner_email="system@example.com",
        name="Implement",
        agent_type="secondary",
        is_system=True,
    )
    observed: dict[str, object] = {}

    async def _resolve_step_agents(
        *args: object, **kwargs: object
    ) -> tuple[AgentDefinition, AgentDefinition]:
        del args, kwargs
        return primary_agent, step_agent

    async def _create_step_session(*args: object, **kwargs: object):
        del args, kwargs
        return conversation, session

    async def _resolve_runtime(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            tool_registry=None,
            executor_connection=None,
            executor_environment=SimpleNamespace(home="/home/user", cwd="/home/user"),
            runtime_info=None,
            cleanup=lambda: asyncio.sleep(0),
        )

    async def _latest_step_run(*args: object, **kwargs: object):
        del args, kwargs
        return None

    async def _create_step_run(*args: object, **kwargs: object):
        del args, kwargs
        return SimpleNamespace()

    async def _update_step_run(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    async def _refresh(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _run_step(ctx: object, **kwargs: object) -> StepOutput:
        del kwargs
        observed["ctx_agent_id"] = getattr(getattr(ctx, "agent", None), "agent_id", None)
        observed["ctx_executor_agent_id"] = getattr(
            getattr(ctx, "executor_agent", None), "agent_id", None
        )
        observed["runtime_agent_id"] = current_agent_id.get()
        observed["runtime_agent_owner_email"] = current_agent_owner_email.get()
        runtime_access = current_runtime_access_context.get()
        observed["runtime_access_agent_id"] = (
            runtime_access.agent_id if runtime_access is not None else None
        )
        observed["runtime_access_agent_type"] = (
            runtime_access.agent_type if runtime_access is not None else None
        )
        return StepOutput(summary="done", content="done")

    monkeypatch.setattr(engine, "_resolve_step_agents", _resolve_step_agents)
    monkeypatch.setattr(engine, "_create_step_session", _create_step_session)
    monkeypatch.setattr(engine, "_resolve_step_runtime", _resolve_runtime)
    monkeypatch.setattr(engine._agent_loop, "run_step", _run_step, raising=False)
    monkeypatch.setattr(engine._session_manager, "refresh_intaris_session_policy", _refresh)
    monkeypatch.setattr(
        "cognis.core.workflow_engine.get_latest_step_run_for_task_step",
        _latest_step_run,
    )
    monkeypatch.setattr("cognis.core.workflow_engine.create_step_run", _create_step_run)
    monkeypatch.setattr("cognis.core.workflow_engine.update_step_run", _update_step_run)

    output, _step_run_id = await engine._execute_run_step(
        task, step_def, task.workflow_state or WorkflowState(), workflow
    )

    assert output is not None
    assert observed == {
        "ctx_agent_id": "system:implement",
        "ctx_executor_agent_id": "agent-b",
        "runtime_agent_id": "agent-b",
        "runtime_agent_owner_email": "user@example.com",
        "runtime_access_agent_id": "agent-b",
        "runtime_access_agent_type": "primary",
    }


@pytest.mark.asyncio
async def test_execute_workflow_exception_cleans_running_step_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    task = TaskModel(
        task_id="task-fail-cleanup",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        workflow_id="wf:test",
        workflow_state=WorkflowState(current_step_index=0),
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Test Workflow",
        steps=[StepDefinition(name="execute", type="run")],
    )
    fail_calls: list[str] = []

    async def _execute_run_step(*args: object, **kwargs: object):
        del args, kwargs
        raise RuntimeError("step exploded")

    async def _persist_task_final(*args: object, **kwargs: object) -> None:
        return None

    async def _noop(*args: object, **kwargs: object) -> None:
        return None

    async def _fail_running_step_runs_for_task(
        _session: object, task_id: str, *args: object, **kwargs: object
    ) -> None:
        del args, kwargs
        fail_calls.append(task_id)

    monkeypatch.setattr(engine, "_execute_run_step", _execute_run_step)
    monkeypatch.setattr(engine, "_persist_task_final", _persist_task_final)
    monkeypatch.setattr(engine, "_cleanup_step_sessions", _noop)
    monkeypatch.setattr(engine, "_deliver_task_result", _noop)
    monkeypatch.setattr(
        "cognis.core.workflow_engine.fail_running_step_runs_for_task",
        _fail_running_step_runs_for_task,
    )

    result = await engine.execute_workflow(task, workflow)

    assert result.status == "failed"
    assert fail_calls == ["task-fail-cleanup"]


@pytest.mark.asyncio
async def test_execute_workflow_defers_transient_executor_unavailable_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    state = WorkflowState()
    task = TaskModel(
        task_id="task-executor-race",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        status=TaskStatus.RUNNING,
        workflow_state=state,
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Test",
        steps=[
            StepDefinition(
                name="run",
                type="run",
                prompt="Do work",
                completion=CompletionConfig(evaluate=False),
            )
        ],
    )
    deferred: list[dict[str, object]] = []
    transient = TransientExecutorUnavailable(
        "Selected executor 'olorin' is not connected or not ready",
        executor_id="olorin",
        retry_after_seconds=5,
    )

    async def _defer_running_task(_session: object, task_id: str, **kwargs: object) -> bool:
        deferred.append({"task_id": task_id, **kwargs})
        return True

    async def _transient_step(*args: object, **kwargs: object):
        del args, kwargs
        raise transient

    async def _successful_step(*args: object, **kwargs: object):
        del args, kwargs
        return StepOutput(summary="done", content="done"), "sr-1"

    async def _persist_workflow_state(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(workflow_engine_module, "defer_running_task", _defer_running_task)
    monkeypatch.setattr(engine, "_execute_run_step", _transient_step)
    monkeypatch.setattr(engine, "_persist_workflow_state", _persist_workflow_state)

    first_result = await engine.execute_workflow(task, workflow)

    assert first_result.status == TaskStatus.READY
    assert first_result.scheduled_for is not None
    assert deferred
    assert state.loop_iterations["transient_executor_unavailable:run"] == 1
    assert "attempts:run" not in state.loop_iterations

    task.status = TaskStatus.RUNNING
    monkeypatch.setattr(engine, "_execute_run_step", _successful_step)

    second_result = await engine.execute_workflow(task, workflow)

    assert second_result.status == TaskStatus.COMPLETED
    assert state.step_outputs["run"]["summary"] == "done"
    assert "attempts:run" not in state.loop_iterations


@pytest.mark.asyncio
async def test_evaluate_step_includes_actual_session_tool_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    captured: dict[str, object] = {}
    task = TaskModel(
        task_id="task-evidence",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        workflow_state=WorkflowState(),
    )
    step_def = StepDefinition(
        name="maintain",
        type="run",
        prompt="Inspect and update weekly notes.",
        completion=CompletionConfig(evaluate=True),
    )
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[step_def])
    output = StepOutput(
        summary="Updated weekly notes",
        content="Read current notes and wrote the weekly file.",
        execution_evidence={"files_written": ["Lumilens/Weekly/2026-W24.md"]},
        intaris_session_id="intaris-session-1",
    )

    async def _read_events(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            events=[
                {
                    "seq": 10,
                    "type": "tool_call",
                    "data": {
                        "call_id": "call-read",
                        "name": "read",
                        "arguments": {"file_path": "Lumilens/Weekly/2026-W24.md"},
                    },
                },
                {
                    "seq": 11,
                    "type": "tool_result",
                    "data": {
                        "call_id": "call-read",
                        "name": "read",
                        "content": "Existing weekly note content",
                    },
                },
                {
                    "seq": 12,
                    "type": "tool_call",
                    "data": {
                        "call_id": "call-patch",
                        "name": "apply_patch",
                        "arguments": {"patch": "*** Begin Patch"},
                    },
                },
            ]
        )

    async def _evaluate(**kwargs: object) -> StepEvaluation:
        captured.update(kwargs)
        return StepEvaluation(decision="approved", reasoning="Evidence present")

    engine._providers.guardrails = SimpleNamespace(read_events=_read_events)
    monkeypatch.setattr(engine._step_evaluator, "evaluate", _evaluate, raising=False)

    result = await engine._evaluate_step(step_def, output, WorkflowState(), task, workflow)

    assert result.decision == "approved"
    evidence = captured["execution_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["files_written"] == ["Lumilens/Weekly/2026-W24.md"]
    session_events = evidence["session_events"]
    assert session_events["session_id"] == "intaris-session-1"
    assert session_events["tool_call_count"] == 2
    assert session_events["tool_result_count"] == 1
    assert [item["name"] for item in session_events["tool_calls"]] == ["read", "apply_patch"]


@pytest.mark.asyncio
async def test_evaluate_step_uses_persisted_deliverable_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    captured: dict[str, object] = {}
    task = TaskModel(
        task_id="task-deliverable",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        workflow_state=WorkflowState(),
    )
    step_def = StepDefinition(
        name="execute",
        type="run",
        prompt="Write the final plain text summary.",
        completion=CompletionConfig(evaluate=True),
    )
    workflow = Workflow(workflow_id="wf:test", name="Test", steps=[step_def])
    persisted_text = "🏠 Osobní\nHotovo.\n\nPozn.: závěrečná věta."
    output = StepOutput(
        summary="Summary written",
        content=f"{persisted_text!r}, False",
        deliverable_id="dlv_plain",
        deliverable_format="plain",
    )

    async def _get_deliverable(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            deliverable_id="dlv_plain",
            content=persisted_text,
            version=2,
            format="plain",
            title="Evening summary",
        )

    async def _evaluate(**kwargs: object) -> StepEvaluation:
        captured.update(kwargs)
        return StepEvaluation(decision="approved", reasoning="Canonical content used")

    monkeypatch.setattr(workflow_engine_module, "get_deliverable", _get_deliverable)
    monkeypatch.setattr(engine._step_evaluator, "evaluate", _evaluate, raising=False)

    result = await engine._evaluate_step(step_def, output, WorkflowState(), task, workflow)

    assert result.decision == "approved"
    evaluated_output = captured["step_output"]
    assert isinstance(evaluated_output, StepOutput)
    assert evaluated_output.content == persisted_text
    assert evaluated_output.deliverable_version == 2
    assert evaluated_output.deliverable_format == "plain"
    assert evaluated_output.metadata["evaluator_deliverable_source"] == {
        "source": "persisted_deliverable",
        "deliverable_id": "dlv_plain",
        "content_mirror_changed": True,
    }


@pytest.mark.asyncio
async def test_execute_workflow_pauses_after_transient_executor_deferral_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _build_engine()
    state = WorkflowState(
        loop_iterations={"transient_executor_unavailable:run": TRANSIENT_EXECUTOR_MAX_DEFERRALS}
    )
    task = TaskModel(
        task_id="task-executor-stuck",
        title="Task",
        created_by="user@example.com",
        agent_id="agent-1",
        status=TaskStatus.RUNNING,
        workflow_state=state,
    )
    workflow = Workflow(
        workflow_id="wf:test",
        name="Test",
        steps=[
            StepDefinition(
                name="run",
                type="run",
                prompt="Do work",
                completion=CompletionConfig(evaluate=False),
            )
        ],
    )
    transient = TransientExecutorUnavailable(
        "Selected executor 'olorin' is not connected or not ready",
        executor_id="olorin",
        retry_after_seconds=5,
    )

    async def _transient_step(*args: object, **kwargs: object):
        del args, kwargs
        raise transient

    async def _persist_workflow_state(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(engine, "_execute_run_step", _transient_step)
    monkeypatch.setattr(engine, "_persist_workflow_state", _persist_workflow_state)

    result = await engine.execute_workflow(task, workflow)

    assert result.status == TaskStatus.PAUSED
    assert state.status == "paused"
    assert state.pending_pause_payload is not None
    assert state.pending_pause_payload["kind"] == "infrastructure_blocked"
    assert state.loop_iterations["transient_executor_unavailable:run"] == (
        TRANSIENT_EXECUTOR_MAX_DEFERRALS + 1
    )
    assert "attempts:run" not in state.loop_iterations

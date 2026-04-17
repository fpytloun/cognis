"""Focused workflow engine runtime tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import PauseResolution, PauseWaiter, PendingPause
from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.task import TaskModel
from cognis.models.workflow import (
    GateConfig,
    GateOption,
    OutcomeRoute,
    StepDefinition,
    StepOutcome,
    StepOutput,
    Workflow,
    WorkflowState,
)


class _SessionFactory:
    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> _SessionFactory:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _EventBus:
    async def publish(self, _: object) -> None:
        return None


def _build_engine() -> WorkflowEngine:
    return WorkflowEngine(
        session_factory=_SessionFactory(),
        providers=SimpleNamespace(llm=None),
        agent_loop=SimpleNamespace(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=_EventBus(),
        pause_waiter=PauseWaiter(),
    )


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
            created_by="user@example.com",
            agent_id="agent-1",
        ),
        WorkflowState(last_operator_instruction="Incorporate the review and continue."),
    )

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
        return SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1")

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
        SimpleNamespace(agent_id="agent-1"),
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
        return SimpleNamespace(session_id="sess-1", intaris_session_id="sess-1")

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
        SimpleNamespace(agent_id="agent-1"),
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

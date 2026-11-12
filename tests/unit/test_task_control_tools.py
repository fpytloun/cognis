"""Task-control tools use current authority and the real revision queue."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import copy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cognis.core.agent_loop import AgentLoop, PauseWaiter, StepContext
from cognis.core.events import EventBus
from cognis.core.session import SessionManager
from cognis.core.session_cache import SessionCache
from cognis.core.task_queue import TaskQueue
from cognis.core.turn_scheduler import TurnScheduler
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.models.agent import AgentDefinition
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    EventAppendResult,
    SessionModel,
)
from cognis.models.tool import ToolCall, ToolResult
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    Base,
    Conversation,
    ProjectGrantRow,
    ProjectRow,
    Session,
    Task,
    User,
)
from cognis.store.queries import (
    create_conversation,
    create_session,
    create_step_run,
    create_task,
    get_task,
    list_step_runs_for_task,
    list_task_comments,
    update_conversation_active_session,
)
from cognis.tools.builtin.orchestration import (
    ADD_TASK_CONTEXT_TOOL,
    ADD_TASK_NOTE_TOOL,
    GET_TASK_TOOL,
    PAUSE_TASK_TOOL,
    REQUEST_TASK_REVISION_TOOL,
    RESUME_TASK_TOOL,
)
from cognis.tools.registry import RegisteredTool, ToolRegistry


@pytest.fixture
async def control(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-tools.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    workflow = Workflow(
        workflow_id="wf-control",
        name="Task control",
        steps=[StepDefinition(name=name, type="run") for name in ("plan", "execute", "review")],
    )
    state = WorkflowState(
        status="completed",
        current_step_index=2,
        step_outputs={name: {"summary": name} for name in ("plan", "execute", "review")},
        effective_workflow_definition=workflow.model_dump(mode="json"),
    )
    async with factory() as db:
        db.add_all(
            [
                User(email="owner@example.com", name="Owner", role="admin"),
                User(email="other@example.com", name="Other", role="member"),
            ]
        )
        await db.flush()
        db.add(Agent(agent_id="primary", owner_email="owner@example.com", name="Primary"))
        db.add(
            Agent(
                agent_id="historical",
                owner_email="owner@example.com",
                name="Historical agent",
                agent_type="secondary",
            )
        )
        await db.flush()
        chat = await create_conversation(
            db, user_email="owner@example.com", agent_id="primary", context_type="web"
        )
        session = await create_session(db, chat.conversation_id, "owner@example.com", "primary")
        await update_conversation_active_session(db, chat.conversation_id, session.session_id)
        task = await create_task(
            db,
            created_by="owner@example.com",
            agent_id="primary",
            title="Completed work",
            workflow_id=workflow.workflow_id,
            workflow_state=state.model_dump(mode="json"),
            status="completed",
        )
        for name in ("plan", "execute", "review"):
            await create_step_run(
                db,
                task_id=task.task_id,
                step_name=name,
                step_type="run",
                status="approved",
                agent_id="primary",
                attempt_number=1,
            )
        await db.commit()

    ctx = StepContext(
        step_definition=StepDefinition(name="chat", type="run"),
        session=SessionModel(
            session_id=session.session_id,
            conversation_id=chat.conversation_id,
            user_email="owner@example.com",
            agent_id="primary",
        ),
        conversation=ConversationModel(
            conversation_id=chat.conversation_id,
            user_email="owner@example.com",
            agent_id="primary",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(agent_id="primary", owner_email="owner@example.com", name="Primary"),
        tool_registry=ToolRegistry(),
    )
    for definition in (
        GET_TASK_TOOL,
        REQUEST_TASK_REVISION_TOOL,
        ADD_TASK_CONTEXT_TOOL,
        ADD_TASK_NOTE_TOOL,
        PAUSE_TASK_TOOL,
        RESUME_TASK_TOOL,
    ):
        ctx.tool_registry.register(RegisteredTool(definition=definition))
    waiter = PauseWaiter()
    workflow_engine = SimpleNamespace(_notification_service=None, _pause_waiter=waiter)
    queue = TaskQueue(
        session_factory=factory,
        workflow_engine=workflow_engine,
        workflow_registry=WorkflowRegistry(factory),
        event_bus=EventBus(),
    )
    launched = []
    queue._launch_claimed_task_run = lambda task, claim: launched.append((task, claim))
    loop = object.__new__(AgentLoop)
    guardrails = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(
                analytics_enabled=True, guardrails_enabled=True, intention="Continue owner work"
            )
        ),
        create_session=AsyncMock(),
        update_session_status=AsyncMock(),
        record_events=AsyncMock(
            return_value=EventAppendResult(ok=True, count=1, first_seq=1, last_seq=1)
        ),
    )
    loop.session_manager = SessionManager(
        factory, SimpleNamespace(guardrails=guardrails), SessionCache(guardrails)
    )
    loop._task_queue = queue
    loop.pause_waiter = waiter

    scheduler = object.__new__(TurnScheduler)
    scheduler._session_factory = factory
    scheduler._deferred_creation_locks = {}

    async def refresh_runtime() -> None:
        loaded = await scheduler._load_conversation_runtime(chat.conversation_id)
        assert loaded is not None
        ctx.conversation, ctx.session, ctx.agent, _ = loaded

    async def rotate() -> None:
        await loop.session_manager.rotate_session(
            conversation_id=chat.conversation_id,
            current_session=ctx.session,
            intention="Continue owner work",
            compaction_summary="Completed task is awaiting owner follow-up.",
        )
        await refresh_runtime()

    async def call(name: str, **arguments: object) -> ToolResult:
        return await loop._handle_orchestration_tool(
            ToolCall(
                call_id=uuid4().hex,
                name=name,
                arguments={"task_id": task.task_id, **arguments},
            ),
            ctx=ctx,
            events_to_record=[],
        )

    try:
        yield SimpleNamespace(
            call=call,
            ctx=ctx,
            factory=factory,
            queue=queue,
            loop=loop,
            task_id=task.task_id,
            launched=launched,
            refresh_runtime=refresh_runtime,
            rotate=rotate,
        )
    finally:
        for _, claim in launched:
            await queue._execution_store.release(claim)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["owner_chat", "task_control"])
@pytest.mark.parametrize("historical_agent", [False, True])
async def test_primary_owner_can_inspect_and_revise_completed_task(
    control, surface, historical_agent
):
    if historical_agent:
        async with control.factory() as db:
            row = await db.get(Session, control.ctx.session.session_id)
            row.agent_id = "historical"
            await db.commit()
        await control.refresh_runtime()
    if surface == "task_control":
        data = {"kind": "task_control", "task_id": control.task_id}
        control.ctx.conversation.context.platform_data = data
        async with control.factory() as db:
            chat = await db.get(Conversation, control.ctx.conversation.conversation_id)
            chat.context_data = data
            task = await get_task(db, control.task_id)
            task.control_conversation_id = chat.conversation_id
            await db.commit()

    inspected = await control.call("get_task")
    assert not inspected.is_error
    assert json.loads(inspected.output)["attempt_number"] == 1
    old_session = control.ctx.session
    await control.rotate()
    rotated_session = control.ctx.session
    assert rotated_session.previous_session_id == old_session.session_id
    assert rotated_session.agent_id == ("historical" if historical_agent else "primary")
    assert control.ctx.agent.agent_id == "primary"

    control.ctx.session = old_session
    stale = await control.call("request_task_revision", expected_attempt=1, instruction="Stale")
    assert stale.is_error
    assert json.loads(stale.output)["reason"] == "task_control_session_access_denied"
    assert control.launched == []
    control.ctx.session = rotated_session
    instruction = "\nOprav hierarchii obsahu podle přílohy.\nZachovej původní zadání.  \n"
    result = await control.call(
        "request_task_revision",
        expected_attempt=1,
        target_step="execute",
        instruction=instruction,
    )
    assert not result.is_error, result.output
    payload = json.loads(result.output)
    assert payload["attempt_number"] == 2
    assert payload["target_step"] == "execute"
    assert payload["superseded_count"] == 2
    async with control.factory() as db:
        task = await get_task(db, control.task_id)
        steps = await list_step_runs_for_task(db, control.task_id)
        comments = await list_task_comments(db, control.task_id)
    assert task.attempt_number == 2
    assert task.workflow_state["current_step_index"] == 1
    assert task.workflow_state["last_revision_context"] == instruction
    assert task.workflow_state["last_operator_instruction"] == instruction
    assert task.workflow_state["step_outputs"] == {"plan": {"summary": "plan"}}
    assert {row.step_name: row.status for row in steps} == {
        "plan": "approved",
        "execute": "superseded",
        "review": "superseded",
    }
    assert [(row.body, row.target_step, row.attempt_number, row.applied) for row in comments] == [
        (instruction, "execute", 1, True)
    ]
    assert len(control.launched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "field", "value", "reason"),
    [
        (Task, "created_by", "other@example.com", "task_control_task_access_denied"),
        (Agent, "owner_email", "other@example.com", "task_control_agent_access_denied"),
        (Agent, "is_system", True, "task_control_agent_access_denied"),
        (Agent, "agent_type", "secondary", "task_control_agent_access_denied"),
        (Agent, "status", "disabled", "task_control_agent_access_denied"),
        (Conversation, "user_email", "other@example.com", "task_control_session_access_denied"),
        (Conversation, "status", "deleted", "task_control_session_access_denied"),
        (Conversation, "active_session_id", None, "task_control_session_access_denied"),
        (Conversation, "context_type", "agent_work", "task_control_session_access_denied"),
        (Conversation, "agent_id", "historical", "task_control_session_access_denied"),
        (Session, "user_email", "other@example.com", "task_control_session_access_denied"),
        (Session, "delegation_mode", "sync", "task_control_session_access_denied"),
        (Session, "status", "completed", "task_control_session_access_denied"),
    ],
)
async def test_revision_revalidates_authority_after_inspection(
    control, record, field, value, reason
):
    assert not (await control.call("get_task")).is_error
    await control.rotate()
    identities = {
        Agent: "primary",
        Task: control.task_id,
        Session: control.ctx.session.session_id,
        Conversation: control.ctx.conversation.conversation_id,
    }
    async with control.factory() as db:
        row = await db.get(record, identities[record])
        setattr(row, field, value)
        await db.commit()
    result = await control.call("request_task_revision", expected_attempt=1, instruction="Revise")
    assert result.is_error
    assert json.loads(result.output)["reason"] == reason
    async with control.factory() as db:
        task = await get_task(db, control.task_id)
        assert task.attempt_number == 1
        assert task.status == "completed"
        assert await list_task_comments(db, control.task_id) == []
    assert control.launched == []


@pytest.mark.asyncio
async def test_revoked_task_control_binding_cannot_mutate(control):
    data = {"kind": "task_control", "task_id": control.task_id}
    control.ctx.conversation.context.platform_data = data
    async with control.factory() as db:
        chat = await db.get(Conversation, control.ctx.conversation.conversation_id)
        chat.context_data = data
        task = await get_task(db, control.task_id)
        task.control_conversation_id = chat.conversation_id
        await db.commit()
    assert not (await control.call("get_task")).is_error
    await control.rotate()
    async with control.factory() as db:
        task = await get_task(db, control.task_id)
        task.control_conversation_id = None
        await db.commit()
    result = await control.call("request_task_revision", expected_attempt=1, instruction="Revise")
    assert result.is_error
    assert json.loads(result.output)["reason"] == "task_control_binding_revoked"
    assert control.launched == []


@pytest.mark.asyncio
async def test_duplicate_revisions_admit_only_one_attempt(control):
    competing_queue = TaskQueue(
        session_factory=control.factory,
        workflow_engine=control.queue._workflow_engine,
        workflow_registry=WorkflowRegistry(control.factory),
        event_bus=EventBus(),
    )
    competing_queue._launch_claimed_task_run = control.queue._launch_claimed_task_run
    competing_loop = copy(control.loop)
    competing_loop._task_queue = competing_queue
    results = await asyncio.gather(
        control.call(
            "request_task_revision", expected_attempt=1, instruction="First", target_step="plan"
        ),
        competing_loop._handle_orchestration_tool(
            ToolCall(
                call_id=uuid4().hex,
                name="request_task_revision",
                arguments={
                    "task_id": control.task_id,
                    "expected_attempt": 1,
                    "instruction": "Second",
                    "target_step": "plan",
                },
            ),
            ctx=control.ctx,
            events_to_record=[],
        ),
    )
    assert sum(not result.is_error for result in results) == 1
    async with control.factory() as db:
        task = await get_task(db, control.task_id)
        assert task.attempt_number == 2
        comments = await list_task_comments(db, control.task_id)
    assert len(comments) == 1
    assert comments[0].body in {"First", "Second"}
    assert len(control.launched) == 1


@pytest.mark.asyncio
async def test_revision_rejects_stale_attempt_cancelled_task_and_bad_schema(control):
    result = await control.call("request_task_revision", expected_attempt=2, instruction="Stale")
    assert result.is_error
    assert json.loads(result.output)["reason"] == "stale_task_attempt"
    result = await control.call("request_task_revision", instruction="Missing attempt")
    assert result.is_error
    async with control.factory() as db:
        task = await get_task(db, control.task_id)
        task.status = "cancelled"
        await db.commit()
    result = await control.call("request_task_revision", expected_attempt=1, instruction="Denied")
    assert result.is_error
    assert "Cancelled tasks cannot be revised" in result.output
    assert control.launched == []


@pytest.mark.asyncio
async def test_owner_chat_notes_context_and_pause_resume_use_same_authority(control):
    for name in ("add_task_note", "add_task_context"):
        result = await control.call(name, expected_attempt=1, body=f"  {name}\n")
        assert not result.is_error, result.output
    async with control.factory() as db:
        comments = await list_task_comments(db, control.task_id)
        task = await get_task(db, control.task_id)
        assert [row.intent for row in comments] == ["record_only", "context_only"]
        task.status = "running"
        await db.commit()
    assert not (await control.call("pause_task", expected_attempt=1)).is_error
    assert not (await control.call("resume_task", expected_attempt=1)).is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["member", "viewer", "revoked"])
async def test_owned_task_control_requires_current_project_permission(control, permission):
    async with control.factory() as db:
        db.add(ProjectRow(project_id="shared", owner_email="other@example.com", name="Shared"))
        await db.flush()
        db.add(
            ProjectGrantRow(
                grant_id="membership",
                project_id="shared",
                grantee_user_email="owner@example.com",
                permission="member" if permission == "revoked" else permission,
                granted_by="other@example.com",
                revoked_at=datetime.now(UTC) if permission == "revoked" else None,
            )
        )
        task = await get_task(db, control.task_id)
        task.project_id = "shared"
        await db.commit()
    result = await control.call(
        "request_task_revision", expected_attempt=1, instruction="Revise", target_step="plan"
    )
    assert result.is_error == (permission != "member"), result.output
    if permission != "member":
        assert json.loads(result.output)["reason"] == "task_control_project_access_denied"
        assert control.launched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["delegate", "workflow"])
async def test_task_control_rejects_non_root_execution_even_for_owner(control, kind):
    if kind == "delegate":
        async with control.factory() as db:
            parent = await create_session(
                db, control.ctx.conversation.conversation_id, "owner@example.com", "primary"
            )
            child = await db.get(Session, control.ctx.session.session_id)
            child.parent_session_id = parent.session_id
            await db.commit()
        control.ctx.session.parent_session_id = parent.session_id
    else:
        control.ctx.task_id = control.task_id
    result = await control.call("request_task_revision", expected_attempt=1, instruction="Revise")
    assert result.is_error
    assert json.loads(result.output)["reason"] == "task_control_owner_primary_chat_required"
    assert control.launched == []


@pytest.mark.asyncio
async def test_revision_fences_and_joins_active_attempt_before_relaunch(control):
    async with control.factory() as db:
        task = await get_task(db, control.task_id)
        task.status = "running"
        steps = await list_step_runs_for_task(db, control.task_id)
        next(row for row in steps if row.step_name == "execute").status = "running"
        await db.commit()
    started = asyncio.Event()
    stopped = asyncio.Event()
    old_claim = await control.queue._execution_store.claim_existing(
        control.task_id, statuses={"running"}
    )
    assert old_claim is not None

    async def active_attempt():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
            await control.queue._execution_store.release(old_claim)

    active = asyncio.create_task(active_attempt())
    control.queue._active_runs[control.task_id] = active
    await started.wait()
    try:
        result = await control.call(
            "request_task_revision",
            expected_attempt=1,
            target_step="execute",
            instruction="Correct active work",
        )
        assert not result.is_error, result.output
        assert active.cancelled()
        assert stopped.is_set()
        assert len(control.launched) == 1
        assert control.launched[0][0].attempt_number == 2
        async with control.factory() as db:
            rows = await list_step_runs_for_task(db, control.task_id)
        assert next(row for row in rows if row.step_name == "execute").status == "superseded"
    finally:
        if not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)

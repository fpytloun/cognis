"""Focused execution tests for controller-owned deterministic workflow steps."""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cognis.core.events import EventBus, EventType
from cognis.core.management import validate_workflow_definition
from cognis.core.runtime import ResolvedStepRuntime, TransientExecutorUnavailable
from cognis.core.session import SessionManager, _intaris_session_policy
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_registry import SOFTWARE_DEVELOPMENT_WORKFLOW
from cognis.core.workflow_rendering import (
    MAX_CONTEXT_STRING_BYTES,
    MAX_DETERMINISTIC_JUMPS,
    WorkflowRenderer,
)
from cognis.models.agent import AgentDefinition
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.tool import EvaluationResult, NativeToolDefinition, ToolResult, ToolSource
from cognis.models.workflow import (
    StepDefinition,
    StepOutput,
    Workflow,
    WorkflowState,
    pin_effective_workflow,
)
from cognis.runtime_context import (
    current_effective_working_directory,
    current_executor_environment,
    current_runtime_access_context,
    current_workspace_root,
    scoped_runtime_context,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.models import Task as TaskRow
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
    update_task_status,
    update_task_workflow_state,
)
from cognis.tools.registry import RegisteredTool, ToolRegistry


@pytest.fixture(params=["sqlite", "postgres"])
async def artifact_workflow_database(tmp_path, request):
    if request.param == "sqlite":
        db_engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/artifact-workflow.db")
        try:
            yield db_engine
        finally:
            await db_engine.dispose()
        return
    url = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = f"artifact_workflow_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema))
    db_engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        yield db_engine
    finally:
        await db_engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("final_tool", ["artifact_write", "artifact_get_metadata"])
async def test_artifact_tool_after_run_recovers_executor(
    tmp_path, monkeypatch, final_tool, artifact_workflow_database
) -> None:
    from cognis.api import runtime_support
    from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
    from cognis.core.artifact_revisions import ContentActor, write_artifact_document
    from cognis.core.executor_pin_notice_dispatch import ExecutorPinNoticeDispatcher
    from cognis.store.models import Agent, ExecutorRecoveryNoticeRow
    from cognis.store.project_queries import upsert_project_agent
    from cognis.store.queries import create_deliverable, create_executor, create_project
    from cognis.tools.builtin.artifact_tools import handle_artifact_tool

    db_engine, factory, engine, task, _ = await _engine_runtime(
        tmp_path, database_engine=artifact_workflow_database
    )
    loop = engine._agent_loop
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    loop.artifact_store = store
    manager = SessionManager(factory, engine._providers, SimpleNamespace(evict=AsyncMock()))
    engine._providers.guardrails.create_session = AsyncMock()
    engine._providers.guardrails.update_session_policy = AsyncMock()
    engine._providers.guardrails.update_session_status = AsyncMock()
    engine._session_manager = manager
    engine._providers._session_factory = factory
    connection = SimpleNamespace(
        list_tools=AsyncMock(return_value=[]),
        environment=AsyncMock(return_value=None),
    )
    ready = True

    engine._providers.executor = SimpleNamespace(
        websocket=SimpleNamespace(
            get_ready_connection=lambda executor_id: connection if ready else None,
            get_connection=lambda executor_id: connection,
            get_handle_metadata=lambda executor_id: {},
            notify_browser_session_terminal=AsyncMock(),
            wait_for_connection=AsyncMock(
                side_effect=AssertionError("Durable tasks must release capacity while unavailable")
            ),
        )
    )
    engine._providers.executor_pin_notice_dispatcher = ExecutorPinNoticeDispatcher(
        session_factory=factory,
        guardrails=engine._providers.guardrails,
    )
    async with factory() as db:
        executor = await create_executor(
            db,
            name="Workflow",
            owner_email=task.created_by,
            executor_type="websocket",
        )
        executor.runtime_state = "active"
        executor_id = executor.executor_id

        agent = await db.get(Agent, task.agent_id)
        agent.status = "active"
        agent.execution = {"executor_id": executor_id}
        project = await create_project(db, owner_email=task.created_by, name="Workflow")
        task.project_id = project.project_id
        row = await get_task(db, task.task_id)
        row.project_id = project.project_id
        await upsert_project_agent(
            db,
            project_id=project.project_id,
            agent_id=task.agent_id,
            added_by=task.created_by,
            is_default=True,
        )
        await db.commit()
    seed, _ = await write_artifact_document(
        session_factory=factory,
        artifact_store=store,
        actor=ContentActor(task.created_by, task.agent_id, None, None),
        project_id=task.project_id,
        filename="state.json",
        content=b'{"pending":null}',
        mime_type="application/json",
        kind="file",
        purpose="workflow_checkpoint",
    )

    monkeypatch.setattr(
        runtime_support,
        "_resolve_web_config",
        AsyncMock(return_value={"web_available_backends": [], "web_backend": "direct"}),
    )
    engine._step_runtime_factory = runtime_support.build_step_runtime_factory(
        providers=engine._providers,
        shared_registry=ToolRegistry(),
        shared_connection=None,
        session_factory=factory,
        artifact_store=store,
    )

    async def dispatch(ctx, call):
        return await handle_artifact_tool(
            call.name,
            call.arguments,
            llm=None,
            artifact_store=store,
            session_factory=factory,
            user_email=task.created_by,
            runtime_metadata={
                "agent_id": task.agent_id,
                "session_id": ctx.session.session_id,
                "conversation_id": ctx.conversation.conversation_id,
            },
        )

    async def run(ctx, **kwargs):
        nonlocal ready
        ready = False
        async with factory() as db:
            dlv = await create_deliverable(
                db,
                step_run_id=ctx.step_run_id,
                session_id=ctx.session.session_id,
                content="Report",
                artifact_store=store,
            )
            await db.commit()
            return StepOutput(summary="Report", deliverable_id=dlv.deliverable_id)

    loop.execute_controller_tool = dispatch
    loop.run_step = AsyncMock(side_effect=run)
    final_args = (
        {
            "filename": "state.json",
            "content": '{"pending":false}',
            "supersedes": "{{ steps.save_pending.outputs.artifact_id }}",
        }
        if final_tool == "artifact_write"
        else {"artifact_id": "{{ steps.save_pending.outputs.artifact_id }}"}
    )
    workflow = Workflow(
        workflow_id=f"wf_{uuid.uuid4().hex}",
        name="Artifact checkpoint",
        defaults={"evaluate": False},
        steps=[
            StepDefinition(
                name="save_pending",
                type="tool_call",
                tool_call={
                    "tool": "artifact_write",
                    "allow_side_effects": True,
                    "args": {
                        "filename": "state.json",
                        "content": '{"pending":true}',
                        "project_id": task.project_id,
                        "supersedes": seed.artifact_id,
                    },
                },
            ),
            StepDefinition(name="report", type="run", prompt="Report", input={"type": "null"}),
            StepDefinition(
                name="ack",
                type="tool_call",
                tool_call={
                    "tool": final_tool,
                    "allow_side_effects": True,
                    "args": final_args,
                },
            ),
        ],
    )
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status == TaskStatus.READY
        assert result.workflow_state.current_step_index == 2
        ready = True
        async with factory() as db:
            assert await update_task_status(
                db, task.task_id, status="running", expected_status="ready"
            )
            await db.commit()
        result = await engine.execute_workflow(result, workflow)
        loop.run_step.assert_awaited_once()
        for output in result.workflow_state.step_outputs.values():
            assert not output.get("error"), output
        assert result.status == TaskStatus.COMPLETED, result.workflow_state.pending_pause_payload
        ack = result.workflow_state.step_outputs["ack"]["outputs"]
        assert ack["revision"] == (3 if final_tool == "artifact_write" else 2)
        assert ack["project_id"] == task.project_id
        engine._providers.executor.websocket.wait_for_connection.assert_not_awaited()
        # The recovery window must be durable, not reset by a projection rollback.
        async with factory() as db:
            notices = list((await db.scalars(select(ExecutorRecoveryNoticeRow))).all())
            assert {notice.recovered for notice in notices} == {False, True}
            assert len({notice.unavailable_since for notice in notices}) == 1
            assert all(notice.delivered_at for notice in notices)
    finally:
        await db_engine.dispose()


@pytest.mark.parametrize("permission", ["read", "write"])
async def test_scheduled_artifact_write_uses_live_lineage_grant(tmp_path, permission):
    from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
    from cognis.core.artifact_revisions import ContentActor, write_artifact_document
    from cognis.core.document_sharing import replace_explicit_shares
    from cognis.core.tool_router import ToolRouter
    from cognis.models.session import ConversationContext, ConversationModel, SessionModel
    from cognis.models.workflow import ToolCallStepConfig
    from cognis.store.models import ArtifactRecordRow
    from cognis.store.queries import create_conversation, create_session, update_agent
    from cognis.tools.builtin.artifact_tools import ARTIFACT_WRITE_TOOL

    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "shared-state")))

    class ArtifactLoop(_DeterministicAgentLoop):
        async def execute_controller_tool(self, ctx, tool_call):
            return await router.execute(
                tool_call,
                ctx.session,
                ctx.agent,
                ctx.tool_registry,
                executor=ctx.executor_connection,
                workflow_step_scope=ctx.workflow_step_scope,
            )

    loop = ArtifactLoop()
    db_engine, factory, engine, task, _ = await _engine_runtime(tmp_path, agent_loop=loop)
    guardrails = AsyncMock()
    guardrails.evaluate.return_value = EvaluationResult(decision="approve", call_id="eval_scope")
    router = ToolRouter(guardrails=guardrails, artifact_store=store, session_factory=factory)
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=ARTIFACT_WRITE_TOOL))

    async def runtime(**kwargs):
        return ResolvedStepRuntime(
            tool_registry=registry,
            executor_connection=SimpleNamespace(executor_id="executor-a"),
            cleanup=AsyncMock(),
            executor_environment=None,
        )

    async def persisted_session(task, step_def, agent, **kwargs):
        async with factory() as db:
            conversation = await create_conversation(
                db, task.created_by, agent.agent_id, "agent_task"
            )
            session = await create_session(
                db, conversation.conversation_id, task.created_by, agent.agent_id
            )
            await db.commit()
            return (
                ConversationModel(
                    conversation_id=conversation.conversation_id,
                    user_email=task.created_by,
                    agent_id=agent.agent_id,
                    context=ConversationContext(type=conversation.context_type),
                ),
                SessionModel.model_validate(session, from_attributes=True),
                False,
            )

    engine._resolve_step_runtime = runtime
    engine._reuse_or_create_step_session = persisted_session
    engine._session_manager.refresh_intaris_session_policy = AsyncMock(
        return_value={"evaluation_policy_version": "hybrid_v1"}
    )
    try:
        async with factory() as db:
            await update_agent(db, task.agent_id, updates={"status": "active"})
            await create_agent(
                db,
                agent_id="state-producer",
                owner_email=task.created_by,
                name="Producer",
                status="active",
            )
            await db.commit()
        original, _ = await write_artifact_document(
            session_factory=factory,
            artifact_store=store,
            actor=ContentActor(
                user_email=task.created_by,
                agent_id="state-producer",
                conversation_id=None,
                session_id=None,
            ),
            content=b'{"generation": 1}',
            filename="state.json",
            mime_type="application/json",
            kind="file",
            purpose="artifact_write",
        )
        async with factory() as db:
            await replace_explicit_shares(
                db,
                await db.get(ArtifactRecordRow, original.artifact_id),
                task.created_by,
                [task.agent_id],
                [task.agent_id] if permission == "write" else [],
            )
            await db.commit()
        workflow = Workflow(
            workflow_id=f"wf_{uuid.uuid4().hex}",
            name="shared-state",
            defaults={"evaluate": False},
            steps=[
                StepDefinition(
                    name="save",
                    type="tool_call",
                    tool_call=ToolCallStepConfig(
                        tool="artifact_write",
                        allow_side_effects=True,
                        args={
                            "filename": "state.json",
                            "content": '{"generation": 2}',
                            "supersedes": original.artifact_id,
                        },
                    ),
                )
            ],
        )
        result = await engine.execute_workflow(task, workflow)
        assert result.status == (
            TaskStatus.COMPLETED if permission == "write" else TaskStatus.FAILED
        )
        if permission == "read":
            assert "supersedes_not_found" in result.workflow_state.step_outputs["save"]["error"]
        async with factory() as db:
            rows = list(
                await db.scalars(
                    select(ArtifactRecordRow)
                    .where(ArtifactRecordRow.lineage_id == original.artifact_id)
                    .order_by(ArtifactRecordRow.revision)
                )
            )
            assert len(rows) == (2 if permission == "write" else 1)
            if permission == "write":
                assert (rows[1].revision, rows[1].owner_agent_id, rows[1].author_agent_id) == (
                    2,
                    "state-producer",
                    task.agent_id,
                )
    finally:
        await db_engine.dispose()


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["receipt", "before_dispatch", "unresolved_read_only", "reconciled_not_sent"]
)
async def test_task_dispatch_recovery_after_owner_restart(
    tmp_path, monkeypatch, mode, artifact_workflow_database
):
    from types import MethodType
    from uuid import uuid4

    from cognis.core.agent_loop import AgentLoop
    from cognis.core.runtime import TransientExecutorUnavailable
    from cognis.core.task_execution import (
        TaskExecutionFence,
        TaskExecutionStore,
        bind_task_execution_fence,
        reset_task_execution_fence,
    )
    from cognis.store.models import Task as TaskRow

    db_engine, factory, engine, task, _ = await _engine_runtime(
        tmp_path, database_engine=artifact_workflow_database
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="mutate",
                description="Test tool with explicit effect capabilities",
                source=ToolSource(type="executor"),
                read_only=mode == "unresolved_read_only",
                capabilities=["read"] if mode == "unresolved_read_only" else ["write"],
            ),
        ),
    )
    _mock_tool_runtime(engine, monkeypatch, registry=registry)
    original_session = engine._reuse_or_create_step_session.return_value
    next_session = SimpleNamespace(
        **{
            **vars(original_session[1]),
            "session_id": uuid4().hex,
            "intaris_session_id": uuid4().hex,
        }
    )
    engine._reuse_or_create_step_session.side_effect = [
        original_session,
        original_session,
    ]
    engine._create_step_session = AsyncMock(return_value=(original_session[0], next_session))
    workflow = Workflow(
        workflow_id="wf-durable-receipt",
        name="Durable receipt",
        defaults={"evaluate": False},
        steps=[
            StepDefinition(
                name="mutate",
                type="tool_call",
                tool_call={
                    "tool": "mutate",
                    "args": {},
                    "allow_side_effects": True,
                    "outputs": {"value": "{{ result.value.value }}"},
                },
            ),
            StepDefinition(
                name="verify",
                type="run",
                prompt="Verify the recovered mutation.",
                require_deliverable=False,
                input={"type": "null"},
            ),
        ],
    )
    executor_id, instance_id = uuid4().hex, uuid4().hex
    captured = {}
    effects = []

    async def accepted_then_disconnected(ctx, tc):
        if "call_id" in captured:
            assert tc.call_id == captured["call_id"]
        captured["call_id"] = tc.call_id
        await ctx.execution_fence.record_tool_dispatch(
            session_id=ctx.session.intaris_session_id or ctx.session.session_id,
            turn_id=ctx.turn_id,
            descriptors=[
                {
                    "call_id": tc.call_id,
                    "tool_name": tc.name,
                    "dispatch_state": "pending",
                }
            ],
        )
        first_call = len(loop.execute_controller_tool.await_args_list) == 1
        if mode == "before_dispatch" and first_call:
            ctx.executor_pool = SimpleNamespace(
                by_id=lambda identifier: SimpleNamespace(executor_type="websocket")
            )
            await AgentLoop._handle_tool_after_same_executor_transient_failure(
                loop,
                ctx,
                tc=tc,
                registered=registry.get("mutate"),
                target_executor_id=executor_id,
                failed_connection=None,
                output_chunk_callback=None,
                original_result=ToolResult(
                    output="Executor is not ready.",
                    is_error=True,
                    metadata={"delivery_state": "not_sent"},
                ),
            )
            pytest.fail("Known non-delivery must suspend before effects")
        await ctx.execution_fence.bind_tool_dispatch(
            tc.call_id,
            executor_id,
            instance_id,
            session_id=ctx.session.intaris_session_id or ctx.session.session_id,
            turn_id=ctx.turn_id,
            dispatch_state="sent",
        )
        if not (mode == "reconciled_not_sent" and first_call):
            effects.append(tc.call_id)
        if mode in {"unresolved_read_only", "reconciled_not_sent"} and first_call:
            ctx.executor_pool = SimpleNamespace(
                by_id=lambda identifier: SimpleNamespace(executor_type="websocket")
            )
            await AgentLoop._handle_tool_after_same_executor_transient_failure(
                loop,
                ctx,
                tc=tc,
                registered=registry.get("mutate"),
                target_executor_id=executor_id,
                failed_connection=None,
                output_chunk_callback=None,
                original_result=ToolResult(
                    output="Executor delivery outcome needs reconciliation.",
                    is_error=True,
                    metadata={"delivery_state": "accepted_unknown"},
                ),
            )
            pytest.fail("Reconciliation must suspend before releasing task ownership")
        if mode in {"before_dispatch", "reconciled_not_sent"}:
            return ToolResult(output='{"value":42}')
        raise TransientExecutorUnavailable(
            "Accepted call is still active", executor_id=executor_id, retry_after_seconds=30
        )

    loop = engine._agent_loop
    loop.execute_controller_tool = AsyncMock(side_effect=accepted_then_disconnected)
    loop.run_step = AsyncMock(return_value=StepOutput(summary="Verified", content="Verified"))
    loop.providers = engine._providers
    loop._bind_tool_dispatch_fence = AgentLoop._bind_tool_dispatch_fence
    loop._handle_tool_after_same_executor_transient_failure = MethodType(
        AgentLoop._handle_tool_after_same_executor_transient_failure, loop
    )
    loop._resolve_target_connection = lambda **kwargs: None
    loop._tool_safe_for_same_executor_retry = MethodType(
        AgentLoop._tool_safe_for_same_executor_retry, loop
    )
    loop._reconcile_accepted_tool_call = AsyncMock(
        return_value=(
            "terminal",
            ToolResult(output='{"value":42}', is_error=False),
            "recovered",
        ),
        side_effect=(
            [
                ("unresolved", None, "instance_mismatch"),
                ("terminal", ToolResult(output='{"value":42}'), "recovered"),
            ]
            if mode == "unresolved_read_only"
            else [("not_sent", None, "legacy_not_seen")]
            if mode == "reconciled_not_sent"
            else None
        ),
    )
    token = None
    fence = None
    try:
        owner = TaskExecutionStore(
            factory, owner_id="receipt-owner-first", max_active_global=1, max_active_per_agent=1
        )
        claim = await owner.claim_existing(task.task_id, statuses={"running"})
        assert claim is not None
        fence = TaskExecutionFence(owner, claim, asyncio.Event())
        token = bind_task_execution_fence(fence)
        first = await engine.execute_workflow(task, workflow)
        assert first.status == TaskStatus.READY
        async with factory() as db:
            row = await db.get(TaskRow, task.task_id)
            assert row.result_data["execution_recovery"]
            if mode in {"before_dispatch", "reconciled_not_sent"}:
                descriptor = next(iter(row.result_data["execution_recovery"].values()))[
                    "tool_calls"
                ][0]
                assert descriptor["dispatch_state"] == "not_sent"
                assert descriptor["executor_id"] == executor_id
                assert descriptor["executor_instance_id"] is None
                assert not effects
            elif mode == "unresolved_read_only":
                descriptor = next(iter(row.result_data["execution_recovery"].values()))[
                    "tool_calls"
                ][0]
                assert descriptor["dispatch_state"] == "sent"
                assert descriptor["executor_instance_id"] == instance_id
                assert effects == [captured["call_id"]]
        reset_task_execution_fence(token)
        token = None
        await fence.close()
        fence = None

        canonical_call = next(
            event.model_dump(mode="json")
            for recorded in engine._providers.guardrails.record_events.await_args_list
            for event in recorded.kwargs["events"]
            if event.type == "tool_call"
        )
        monkeypatch.setattr(
            "cognis.core.task_tool_recovery.read_canonical_tool_pairs",
            AsyncMock(return_value={captured["call_id"]: (canonical_call, None)}),
        )
        replacement = TaskExecutionStore(
            factory,
            owner_id="receipt-owner-replacement",
            max_active_global=1,
            max_active_per_agent=1,
        )
        assert await replacement.claim_ready() is None
        async with factory() as db:
            row = await db.get(TaskRow, task.task_id)
            # Advance the durable retry deadline without a wall-clock sleep.
            row.scheduled_for = None
            await db.commit()
        claim = await replacement.claim_ready()
        assert claim is not None
        fence = TaskExecutionFence(replacement, claim, asyncio.Event())
        token = bind_task_execution_fence(fence)
        second = await engine.execute_workflow(task, workflow)
        assert second.status == TaskStatus.COMPLETED
        assert second.workflow_state.step_outputs["mutate"]["outputs"]["value"] == 42
        assert loop.execute_controller_tool.await_count == (
            2 if mode in {"before_dispatch", "reconciled_not_sent"} else 1
        )
        assert (
            loop._reconcile_accepted_tool_call.await_count
            == {
                "receipt": 1,
                "before_dispatch": 0,
                "unresolved_read_only": 2,
                "reconciled_not_sent": 1,
            }[mode]
        )
        assert effects == [captured["call_id"]]
        loop.run_step.assert_awaited_once()
        assert (
            loop.run_step.await_args.args[0].session.intaris_session_id
            == next_session.intaris_session_id
        )
        async with factory() as db:
            row = await db.get(TaskRow, task.task_id)
            assert not row.result_data.get("execution_recovery")
    finally:
        if token is not None:
            reset_task_execution_fence(token)
        if fence is not None:
            await fence.close()
        await db_engine.dispose()


async def _engine_runtime(
    tmp_path: object,
    *,
    agent_loop: object | None = None,
    database_engine: AsyncEngine | None = None,
) -> tuple[object, object, WorkflowEngine, TaskModel, EventBus]:
    db_engine = database_engine or create_engine(f"sqlite+aiosqlite:///{tmp_path}/deterministic.db")
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
            status="active",
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
        providers=SimpleNamespace(
            llm=AsyncMock(),
            guardrails=SimpleNamespace(
                record_events=AsyncMock(return_value=SimpleNamespace(ok=True))
            ),
        ),
        agent_loop=agent_loop or _DeterministicAgentLoop(),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(
            mark_completed=AsyncMock(), refresh_intaris_session_policy=AsyncMock()
        ),
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
    # Production TaskQueue pins the effective definition before engine entry.
    execute = workflow_engine.execute_workflow

    async def execute_pinned(task: TaskModel, workflow: Workflow, **kwargs: object) -> object:
        pin_effective_workflow(task.workflow_state, workflow)
        return await execute(task, workflow, **kwargs)

    workflow_engine.execute_workflow = execute_pinned
    return db_engine, session_factory, workflow_engine, task, event_bus


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["auth_denial", "uncertain_effect", "real_failure"])
async def test_recovery_failure_keeps_source_and_truthful_status(tmp_path, cause):
    import httpx

    from cognis.core.task_recovery_errors import TaskRecoveryBlocked
    from cognis.providers.guardrails.errors import IntarisHTTPError
    from cognis.store.models import Task

    db_engine, factory, engine, task, event_bus = await _engine_runtime(tmp_path)
    source = {
        "execution_recovery": {
            "reserved": {
                "session_id": "interrupted-session",
                "turn_id": "interrupted-turn",
                "attempt_number": 1,
                "tool_calls": [{"call_id": "pending-call", "tool_name": "delegate"}],
            }
        }
    }
    async with factory() as db:
        row = await db.get(Task, task.task_id)
        row.result_summary = "Workflow is recovering interrupted execution."
        row.result_data = source
        await db.commit()
    task.result_summary = "Workflow is recovering interrupted execution."
    task.result_data = source
    request = httpx.Request("GET", "https://guardrails.example.com/events")
    if cause == "auth_denial":
        failure = IntarisHTTPError(
            httpx.HTTPStatusError(
                "private diagnostic", request=request, response=httpx.Response(401, request=request)
            ),
            operation="read_events",
        )
    elif cause == "uncertain_effect":
        failure = TaskRecoveryBlocked("Mutation outcome needs reconciliation.")
    else:
        failure = RuntimeError("private unexpected diagnostic")
    engine._execute_run_step = AsyncMock(side_effect=failure)
    event_bus.publish = AsyncMock()
    workflow = Workflow(
        workflow_id="recovery-boundary",
        name="Recovery boundary",
        steps=[StepDefinition(name="execute", type="run")],
    )
    try:
        result = await engine.execute_workflow(task, workflow)
        async with factory() as db:
            row = await db.get(Task, task.task_id)
            assert row.result_data["execution_recovery"] == source["execution_recovery"]
            assert row.result_summary != "Workflow is recovering interrupted execution."
            assert "private" not in row.result_summary
            if cause == "real_failure":
                assert row.status == "failed"
                assert "RuntimeError" in row.result_summary
            else:
                assert row.status == "paused"
                assert row.result_data["recovery_blocked"]["error_type"] == type(failure).__name__
                assert result.workflow_state.resume_failed_step
                assert any(
                    call.args[0].type == EventType.TASK_PAUSED
                    for call in event_bus.publish.await_args_list
                )
        engine._execute_run_step.assert_awaited_once()
    finally:
        await db_engine.dispose()


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
                SimpleNamespace(conversation_id="conversation-1", user_email="user@example.com"),
                SimpleNamespace(
                    session_id="session-1",
                    intaris_session_id="intaris-1",
                    parent_session_id=None,
                    delegation_mode=None,
                    delegation_metadata={},
                    status="active",
                    agent_id=agent.agent_id,
                    user_email=agent.owner_email,
                    conversation_id="conversation-1",
                ),
                False,
            )
        ),
    )
    monkeypatch.setattr(engine, "_resolve_step_runtime", AsyncMock(return_value=runtime))
    return cleanup


@pytest.mark.asyncio
@pytest.mark.parametrize("step_name", ["operate", "watch"])
@pytest.mark.parametrize(
    "outputs, expected", [({"notify": False}, False), ({"notify": True}, True), ({}, False)]
)
async def test_notify_default_through_step_output_context(
    tmp_path: object, step_name: str, outputs: dict[str, object], expected: bool
) -> None:
    db_engine, _, engine, task, _ = await _engine_runtime(tmp_path)
    try:
        task.workflow_state.step_outputs[step_name] = StepOutput(
            summary="Completed", outputs=outputs
        ).model_dump(mode="json")
        route = StepDefinition(
            name="route",
            type="condition",
            condition={
                "if": f"{{{{ steps.{step_name}.outputs.notify | default(false) == true }}}}",
                "then": "complete_notify",
                "else": "complete_silent",
            },
        )
        workflow = Workflow(
            workflow_id=f"wf_{uuid.uuid4().hex}",
            name="Notify routing",
            steps=[
                route,
                StepDefinition(
                    name="complete_notify", type="complete", complete={"summary": "Notify"}
                ),
                StepDefinition(
                    name="complete_silent", type="complete", complete={"summary": "Silent"}
                ),
            ],
        )
        context = engine._deterministic_render_context(task, task.workflow_state, workflow, route)
        assert WorkflowRenderer().render_expression(route.condition.if_, context) is expected
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approve", "deny", "error", "cancel"])
async def test_tool_step_refreshes_policy_in_resolved_runtime_before_dispatch(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    loop = _DeterministicAgentLoop()
    db_engine, factory, engine, task, _ = await _engine_runtime(tmp_path, agent_loop=loop)
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="read",
                description="Read",
                parameters={"type": "object"},
                source=ToolSource(type="executor"),
                read_only=True,
            ),
        )
    )
    cleanup = _mock_tool_runtime(engine, monkeypatch, registry)
    runtime = engine._resolve_step_runtime.return_value
    environment = SimpleNamespace(home="/home/operator", cwd="/home/operator", tmpdir="/tmp")
    runtime.executor_environment = environment
    runtime.active_executor_id = "executor-a"
    session = engine._reuse_or_create_step_session.return_value[1]
    workflow = Workflow(
        workflow_id="wf-runtime-policy",
        name="Runtime policy",
        defaults={"session_policy": {"allow_policies": ["Read the authorized journal."]}},
        steps=[StepDefinition(name="check", type="tool_call", tool_call={"tool": "read"})],
    )
    task.session_policy = {"deny_policies": ["Do not modify production."]}
    order: list[str] = []
    policy = {}
    with scoped_runtime_context():
        # A scheduled session can be created before an executor has been resolved.
        tokens = [
            (current_workspace_root, current_workspace_root.set(None)),
            (current_effective_working_directory, current_effective_working_directory.set(None)),
            (current_executor_environment, current_executor_environment.set(None)),
        ]
        try:
            policy.update(_intaris_session_policy(None))
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)
    assert policy["allow_paths"] == ["/tmp/*", "/private/tmp/*", "/var/tmp/*"]

    async def update_policy(intaris_id: str, **kwargs: object) -> None:
        order.append("refresh")
        assert intaris_id == session.intaris_session_id
        assert current_executor_environment.get() is environment
        assert current_workspace_root.get() == "/home/operator"
        assert current_effective_working_directory.get() == "/home/operator"
        access = current_runtime_access_context.get()
        assert access is not None
        assert access.session_id == session.session_id
        assert access.task_id == task.task_id
        assert access.step_name == "check"
        policy.clear()
        policy.update(kwargs["policy"])
        assert policy["allow_policies"] == ["Read the authorized journal."]
        assert policy["deny_policies"] == ["Do not modify production."]

    engine._providers.guardrails.update_session_policy = AsyncMock(side_effect=update_policy)
    manager = SessionManager(factory, engine._providers, SimpleNamespace())
    original_refresh = manager.refresh_intaris_session_policy

    async def refresh(actual_session: object) -> dict[str, object] | None:
        assert actual_session is session
        if outcome == "error":
            order.append("refresh")
            raise RuntimeError("Refresh failed")
        if outcome == "cancel":
            order.append("refresh")
            raise asyncio.CancelledError
        acknowledged_policy = await original_refresh(actual_session)
        assert acknowledged_policy == policy
        return acknowledged_policy

    engine._session_manager.refresh_intaris_session_policy = AsyncMock(side_effect=refresh)

    async def dispatch(ctx: object, call: object) -> ToolResult:
        order.append("evaluate")
        assert order == ["refresh", "evaluate"]
        assert "/home/operator/*" in policy["allow_paths"]
        assert not any(
            "controller" in path or "secondary" in path for path in policy["allow_paths"]
        )
        assert ctx.active_executor_id == "executor-a"
        assert current_runtime_access_context.get().session_id == session.session_id
        if outcome == "deny":
            return ToolResult(output="Guard denied", is_error=True)
        order.append("execute")
        loop.calls.append(call)
        return ToolResult(output="Journal read")

    monkeypatch.setattr(loop, "execute_controller_tool", dispatch)
    try:
        with scoped_runtime_context(
            workspace_root="/srv/controller",
            effective_working_directory="/srv/controller",
            executor_environment=SimpleNamespace(home="/home/secondary", cwd="/home/secondary"),
        ):
            if outcome == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await engine.execute_workflow(task, workflow)
            else:
                result = await engine.execute_workflow(task, workflow)
                assert result.status == (
                    TaskStatus.COMPLETED if outcome == "approve" else TaskStatus.FAILED
                )
            assert current_workspace_root.get() == "/srv/controller"
            assert current_effective_working_directory.get() == "/srv/controller"
            assert current_executor_environment.get().home == "/home/secondary"
        assert order == (
            ["refresh", "evaluate", "execute"]
            if outcome == "approve"
            else ["refresh", "evaluate"]
            if outcome == "deny"
            else ["refresh"]
        )
        assert len(loop.calls) == (1 if outcome == "approve" else 0)
        cleanup.assert_awaited_once()
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
async def test_deterministic_scope_uses_acknowledged_session_policy(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    from cognis.core.tool_router import ToolRouter
    from cognis.models.session import SessionModel

    loop = _DeterministicAgentLoop()
    db_engine, _, engine, task, _ = await _engine_runtime(tmp_path, agent_loop=loop)
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="read",
                description="Read",
                parameters={"type": "object"},
                source=ToolSource(type="executor"),
                read_only=True,
            )
        )
    )
    _mock_tool_runtime(engine, monkeypatch, registry)
    engine._session_manager.refresh_intaris_session_policy.return_value = {
        "evaluation_policy_version": version
    }
    guardrails = AsyncMock()
    guardrails.evaluate.return_value = EvaluationResult(decision="approve", call_id="eval_scope")
    router = ToolRouter(guardrails)
    executor = SimpleNamespace(
        executor_id="executor-a", tool_execute=AsyncMock(return_value=ToolResult(output="Read"))
    )

    async def dispatch(ctx: object, call: object) -> ToolResult:
        return await router.execute(
            call,
            session=SessionModel(
                session_id=ctx.session.session_id,
                intaris_session_id=ctx.session.intaris_session_id,
                conversation_id=ctx.session.conversation_id,
                user_email=ctx.session.user_email,
                agent_id=ctx.session.agent_id,
            ),
            agent=ctx.agent,
            registry=registry,
            executor=executor,
            workflow_step_scope=ctx.workflow_step_scope,
            workflow_executor_id=executor.executor_id,
        )

    monkeypatch.setattr(loop, "execute_controller_tool", dispatch)
    workflow = Workflow(
        workflow_id="wf-policy-scope",
        name="Policy scope",
        steps=[StepDefinition(name="read", type="tool_call", tool_call={"tool": "read"})],
    )
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status == TaskStatus.COMPLETED
        guardrails.evaluate.assert_awaited_once()
        executor.tool_execute.assert_awaited_once()
        binding = guardrails.evaluate.call_args.kwargs.get("workflow_binding")
        if version == "legacy":
            guardrails.declare_workflow_step.assert_not_awaited()
            assert binding is None
        else:
            guardrails.declare_workflow_step.assert_awaited_once()
            assert binding.declaration is guardrails.declare_workflow_step.call_args.args[0]
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_status", ["success", "failed"])
async def test_condition_routes_saved_run_outcome_to_matching_terminal(
    tmp_path: object, outcome_status: str
) -> None:
    db_engine, session_factory, engine, task, _ = await _engine_runtime(tmp_path)
    task.workflow_state.step_outputs["implement"] = StepOutput(
        summary="Support updated" if outcome_status == "success" else "Source unavailable",
        outcome={"status": outcome_status, "reason": "Actual run result"},
    ).model_dump(mode="json")
    task.workflow_state.current_step_index = 1
    async with session_factory() as session:
        row = await session.get(TaskRow, task.task_id)
        row.workflow_state = task.workflow_state.model_dump(mode="json")
        await session.commit()
    workflow = Workflow(
        workflow_id="wf-outcome-terminal",
        name="Outcome terminal routing",
        steps=[
            StepDefinition(
                name="implement",
                type="run",
                require_deliverable=False,
                completion={"evaluate": False},
                outcome_routes=[{"status": "failed", "action": "continue"}],
            ),
            StepDefinition(
                name="route",
                type="condition",
                condition={
                    "if": "{{ steps.implement.outcome.status == 'success' }}",
                    "then": "complete_notify",
                    "else": "complete_failed",
                },
            ),
            StepDefinition(
                name="complete_notify",
                type="complete",
                complete={"status": "completed", "summary": "{{ steps.implement.summary }}"},
            ),
            StepDefinition(
                name="complete_failed",
                type="complete",
                complete={"status": "failed", "summary": "{{ steps.implement.summary }}"},
            ),
        ],
    )
    try:
        result = await engine.execute_workflow(task, workflow)

        assert result.status == (
            TaskStatus.COMPLETED if outcome_status == "success" else TaskStatus.FAILED
        )
        async with session_factory() as session:
            rows = await list_step_runs_for_task(session, task.task_id)
        assert [row.step_name for row in rows] == [
            "route",
            "complete_notify" if outcome_status == "success" else "complete_failed",
        ]
    finally:
        await db_engine.dispose()


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


@pytest.mark.asyncio
async def test_shipped_code_review_route_allows_one_correction_then_reaches_named_gate(
    tmp_path: object,
) -> None:
    db_engine, session_factory, engine, task, _event_bus = await _engine_runtime(tmp_path)
    workflow = SOFTWARE_DEVELOPMENT_WORKFLOW
    route_index = next(
        index for index, step in enumerate(workflow.steps) if step.name == "code_review_route"
    )
    route = workflow.steps[route_index]
    exhausted_gate = workflow.steps[route_index + 1]
    assert route.condition is not None
    assert route.condition.max_loop_iterations == 1
    assert route.condition.on_exhausted == "continue"
    assert exhausted_gate.name == "code_review_exhausted_gate"
    assert exhausted_gate.gate is not None
    assert (
        exhausted_gate.gate.message
        == "Implementation still has blocking findings after one correction cycle. "
        "Replan or escalate before continuing."
    )

    state = WorkflowState(current_step_index=route_index)
    loop_key = f"condition:{route.name}->{route.condition.then}"
    state.loop_iterations[loop_key] = 1
    step_run_id = "step-code-review-one-correction-exhausted"
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
            {
                "steps": {
                    "code_review": {
                        "metadata": {
                            "decision": "revise",
                            "required_scope_complete": True,
                            "missing_scope_count": 0,
                            "must_fix_count": 1,
                        }
                    }
                }
            },
        )

        assert result.step_run_status == "failed"
        assert result.error_action == "continue"
        assert result.next_step_index == route_index + 1
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
@pytest.mark.parametrize("audit_case", ["success", "interrupted", "rejected"])
async def test_read_only_tool_output_drives_condition_and_target_executor(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    audit_case: str,
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
    conversation = SimpleNamespace(conversation_id="conversation-1", user_email="user@example.com")
    session = SimpleNamespace(
        session_id="session-1",
        intaris_session_id="intaris-1",
        parent_session_id=None,
        delegation_mode=None,
        status="active",
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
                    "args": {
                        "limit": 10,
                        "target_executor": "executor-b",
                        "private_value": "hidden",
                    },
                    "redact_args": ["private_value"],
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
        recorder = engine._providers.guardrails.record_events

        async def append(**kwargs: object) -> object:
            assert kwargs["user_email"] == "user@example.com"
            assert kwargs["agent_id"] == "agent-1"
            assert kwargs["agent_owner_email"] == "user@example.com"
            return SimpleNamespace(ok=audit_case != "rejected")

        recorder.side_effect = append
        if audit_case == "interrupted":
            original_dispatch = agent_loop.execute_controller_tool
            monkeypatch.setattr(
                agent_loop, "execute_controller_tool", AsyncMock(side_effect=asyncio.CancelledError)
            )
            with pytest.raises(asyncio.CancelledError):
                await engine.execute_workflow(task, workflow)
            assert [call.kwargs["events"][0].type for call in recorder.await_args_list] == [
                "tool_call"
            ]
            interrupted_key = recorder.await_args_list[0].kwargs["idempotency_key"]
            monkeypatch.setattr(agent_loop, "execute_controller_tool", original_dispatch)
            recorder.reset_mock()
            cleanup.reset_mock()

        result = await engine.execute_workflow(task, workflow)

        if audit_case == "rejected":
            assert result.status == TaskStatus.FAILED
            assert agent_loop.calls == []
            return
        assert result.status == TaskStatus.COMPLETED
        assert len(agent_loop.calls) == 1
        recorded = engine._providers.guardrails.record_events.await_args_list
        assert len(recorded) == 2
        call_event = recorded[0].kwargs["events"][0]
        result_event = recorded[1].kwargs["events"][0]
        assert call_event.type == "tool_call"
        assert call_event.data["arguments"]["limit"] == 10
        assert call_event.data["arguments"]["private_value"] == "[redacted]"
        assert result_event.type == "tool_result"
        assert result_event.data["result"] == raw_output
        assert result_event.data["call_id"] == call_event.data["call_id"]
        assert result_event.data["turn_id"] == call_event.data["call_id"]
        assert recorded[0].kwargs["idempotency_key"] == (
            f"intaris-1:{call_event.data['call_id']}:tool_call"
        )
        assert recorded[1].kwargs["idempotency_key"] == (
            f"intaris-1:{call_event.data['call_id']}:tool_result"
        )
        if audit_case == "interrupted":
            assert recorded[0].kwargs["idempotency_key"] == interrupted_key
        assert agent_loop.calls[0].arguments == {
            "limit": 10,
            "target_executor": "executor-b",
            "private_value": "hidden",
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
@pytest.mark.parametrize("large_section", ["instructions", "templates"])
async def test_skill_assets_step_excludes_model_only_metadata(
    tmp_path, monkeypatch, large_section
) -> None:
    from cognis import json_codec as json
    from cognis.models.skill import SkillAssetRef
    from cognis.tools.builtin.skill_management import (
        SKILL_LOAD_TOOL,
        materialize_loaded_skill_context,
    )

    asset = SkillAssetRef(
        asset_id="sa_reference",
        artifact_object_id="reference-object",
        filename="reference.md",
        content_hash="a" * 64,
        size_bytes=1,
        content_type="text/markdown",
    )
    manifest = [
        {
            **asset.model_dump(mode="json"),
            "local_path": str(tmp_path / "executor" / "reference.md"),
            "executor_id": "executor-a",
        }
    ]
    python_environment = {"python_path": str(tmp_path / "executor" / "bin" / "python")}
    instructions = "Read the prepared reference."
    templates = {}
    oversized = "Skill guidance.\n" * (MAX_CONTEXT_STRING_BYTES // 10)
    if large_section == "instructions":
        instructions = oversized
    else:
        templates = {"operate": oversized}
    payload, metadata = materialize_loaded_skill_context(
        skill_id="skill_reference",
        name="Reference",
        description="Prepared reference assets.",
        instructions=instructions,
        templates=templates,
        tools=[],
        steps=[],
        tags=[],
        asset_refs=[asset],
        linked_tool_ids=[],
        prepared_assets=manifest,
        python_environment=python_environment,
    )
    metadata["readiness"] = {"status": "ready"}
    loop = _DeterministicAgentLoop(ToolResult(output=json.dumps(payload), metadata=metadata))
    db_engine, factory, engine, task, _ = await _engine_runtime(tmp_path, agent_loop=loop)
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=SKILL_LOAD_TOOL))
    _mock_tool_runtime(engine, monkeypatch, registry)
    workflow = Workflow(
        workflow_id="wf-skill-assets",
        name="Skill assets",
        steps=[
            StepDefinition(
                name="transport_assets",
                type="tool_call",
                tool_call={
                    "tool": "skill_load",
                    "args": {"skill_id": "skill_reference", "require_assets_ready": True},
                    "allow_side_effects": True,
                    "outputs": {
                        "asset_manifest": "{{ result.value.asset_manifest }}",
                        "python_environment": "{{ result.value.python_environment }}",
                        "metadata": "{{ result.metadata }}",
                    },
                },
            ),
            StepDefinition(
                name="done",
                type="complete",
                complete={"summary": "Assets ready.", "delivery_mode_override": "silent"},
            ),
        ],
    )
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status == TaskStatus.COMPLETED, result.result_summary
        async with factory() as db:
            step = await get_latest_step_run_for_task_step(db, task.task_id, "transport_assets")
            assert step.status == "approved"
            outputs = step.output["outputs"]
            assert outputs["asset_manifest"] == manifest
            assert outputs["python_environment"] == python_environment
            assert outputs["metadata"]["readiness"] == {"status": "ready"}
            assert outputs["metadata"]["recovery_call_id"].startswith("det_")
            assert "protected_context" not in outputs["metadata"]
            assert "skill_activation" not in outputs["metadata"]
        # Projection must not strip the original result used by ordinary model loading.
        assert len(metadata["protected_context"].encode()) > MAX_CONTEXT_STRING_BYTES
        assert metadata["skill_activation"]["instructions"] == instructions
    finally:
        await db_engine.dispose()


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
                    status="active",
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
                    status="active",
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


async def _run_reporting_tool_step(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    *,
    creation_origin: str | None,
    fail_first_dispatch: bool = False,
) -> tuple[list[object], AsyncMock]:
    agent_loop = _DeterministicAgentLoop()
    db_engine, session_factory, engine, task, _ = await _engine_runtime(
        tmp_path, agent_loop=agent_loop
    )
    async with session_factory() as session:
        row = await get_task(session, task.task_id)
        assert row is not None
        row.creation_origin = creation_origin
        await session.commit()
    task.creation_origin = creation_origin
    task.source_type = "manual"
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="read",
                description="Read",
                parameters={"type": "object"},
                source=ToolSource(type="executor"),
                read_only=True,
            ),
        )
    )
    _mock_tool_runtime(engine, monkeypatch, registry)
    report_reasoning = AsyncMock()
    engine._providers.guardrails.report_reasoning = report_reasoning
    dispatches = 0

    async def dispatch(_ctx: object, _call: object) -> ToolResult:
        nonlocal dispatches
        dispatches += 1
        if fail_first_dispatch and dispatches == 1:
            raise TransientExecutorUnavailable(
                "executor reconnecting", executor_id="executor-a", retry_after_seconds=1
            )
        return ToolResult(output="Journal read")

    monkeypatch.setattr(agent_loop, "execute_controller_tool", dispatch)
    workflow = Workflow(
        workflow_id="wf-report",
        name="Report",
        steps=[
            StepDefinition(
                name="journal",
                type="tool_call",
                tool_call={"tool": "read", "args": {"path": "/srv/journal.md"}},
            )
        ],
    )
    try:
        result = await engine.execute_workflow(task, workflow)
        if fail_first_dispatch:
            assert result.status == TaskStatus.READY
            async with session_factory() as session:
                row = await get_task(session, task.task_id)
                assert row is not None
                row.status = "running"
                await session.commit()
            task.status = TaskStatus.RUNNING
            result = await engine.execute_workflow(task, workflow)
        assert result.status == TaskStatus.COMPLETED
    finally:
        await db_engine.dispose()
    events = [
        event
        for call in engine._providers.guardrails.record_events.await_args_list
        for event in call.kwargs["events"]
        if event.type == "user_message"
    ]
    return events, report_reasoning


@pytest.mark.asyncio
async def test_owner_configured_tool_step_reports_rendered_step_as_user_message(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, report_reasoning = await _run_reporting_tool_step(
        tmp_path, monkeypatch, creation_origin="owner_configured"
    )

    assert len(events) == 1
    assert events[0].data["role"] == "user"
    assert events[0].data["content"] == (
        'Workflow step journal: run read with {"path": "/srv/journal.md"}'
    )
    report_reasoning.assert_awaited_once_with(session_id="intaris-1", from_events=True)


@pytest.mark.asyncio
async def test_agent_composed_tool_step_reports_no_user_message(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, report_reasoning = await _run_reporting_tool_step(
        tmp_path, monkeypatch, creation_origin="agent_composed"
    )

    assert events == []
    report_reasoning.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_configured_tool_step_retry_does_not_report_twice(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, report_reasoning = await _run_reporting_tool_step(
        tmp_path, monkeypatch, creation_origin="owner_configured", fail_first_dispatch=True
    )

    assert len(events) == 1
    report_reasoning.assert_awaited_once()

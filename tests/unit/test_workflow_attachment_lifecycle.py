"""Deterministic publication through the engine, agent loop, router and executor boundary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from cognis.api.routes.transfers import router as transfers_router
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.agent_loop import AgentLoop, PauseWaiter, SessionLock
from cognis.core.artifact_revisions import ContentActor, write_artifact_document
from cognis.core.binary_transfers import BinaryTransferService
from cognis.core.runtime import ResolvedStepRuntime
from cognis.core.tool_router import ToolRouter
from cognis.models.session import ConversationContext
from cognis.models.task import TaskStatus
from cognis.models.tool import EvaluationResult, NativeToolDefinition, ToolResult, ToolSource
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState
from cognis.store.models import ArtifactRecordRow
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_user,
    get_latest_step_run_for_task_step,
    get_task,
)
from cognis.tools.executor.document import ARTIFACT_PUBLISH_TOOL, handle_artifact_publish
from cognis.tools.registry import RegisteredTool, ToolExecutionContext, ToolRegistry
from cognis.transfers.client import BinaryTransferClient
from tests.unit.test_workflow_deterministic_execution import (
    _engine_runtime,
    _mock_tool_runtime,
)
from tests.unit.test_workflow_deterministic_execution import (
    artifact_workflow_database as _artifact_workflow_database,
)

artifact_workflow_database = _artifact_workflow_database


async def _publication_runtime(tmp_path, monkeypatch, database, *, staged=False):
    _, factory, engine, task, _ = await _engine_runtime(tmp_path, database_engine=database)
    store = ArtifactStore(
        ArtifactStoreConfig(
            path=str(tmp_path / "artifacts"),
            base_url="http://testserver",
            signing_secret="workflow-test-signing-secret",
        )
    )
    guardrails = engine._providers.guardrails
    guardrails.evaluate = AsyncMock(
        return_value=EvaluationResult(decision="approve", call_id="publication-evaluation")
    )
    cache = SimpleNamespace(
        get_loaded_skill_snapshots=lambda _: {},
        get_events_since_compaction=lambda *a, **kw: [],
    )
    router = ToolRouter(guardrails, session_factory=factory, artifact_store=store)
    loop = AgentLoop(
        providers=engine._providers,
        session_manager=SimpleNamespace(),
        session_cache=cache,
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=router,
        remember_queue=SimpleNamespace(),
        event_bus=engine._event_bus,
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
    )
    engine._agent_loop = loop
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=ARTIFACT_PUBLISH_TOOL))
    _mock_tool_runtime(engine, monkeypatch, registry)
    conversation, session, reused = engine._reuse_or_create_step_session.return_value
    conversation.context = ConversationContext(type="web")
    session.metadata = {}
    async with factory() as db:
        await create_conversation(
            db,
            conversation_id=conversation.conversation_id,
            user_email=task.created_by,
            agent_id=task.agent_id,
            context_type="web",
        )
        await create_session(
            db,
            conversation.conversation_id,
            task.created_by,
            task.agent_id,
            session_id=session.session_id,
        )
        await db.commit()

    if staged:
        app = FastAPI()
        app.state.session_factory = factory
        app.state.artifact_store = store
        app.state.binary_transfers = BinaryTransferService(factory, store)
        app.include_router(transfers_router)
        monkeypatch.setattr(
            BinaryTransferClient,
            "_http",
            lambda _: httpx.AsyncClient(transport=httpx.ASGITransport(app=app)),
        )

    # This provider double represents the executor side of the RPC boundary.
    # Controller dispatch, policy, materialization and step persistence are real.
    async def execute(call, **kwargs):
        result = await handle_artifact_publish(
            call.arguments,
            ToolExecutionContext(executor_handle=None, runtime_metadata=call.runtime_metadata),
        )
        # An executor claim must not become the controller publication receipt.
        if not result.is_error:
            claimed_id = store.generate_id("art")
            result.metadata = {"artifact_id": claimed_id}
            result.attachments[0]["artifact_id"] = claimed_id
        return result

    executor = SimpleNamespace(
        executor_id="publication-executor",
        capabilities=SimpleNamespace(binary_transfers=staged),
        tool_execute=AsyncMock(side_effect=execute),
    )
    engine._resolve_step_runtime.return_value = ResolvedStepRuntime(
        tool_registry=registry,
        executor_connection=executor,
        cleanup=AsyncMock(),
        executor_environment=None,
        runtime_info={"runtime_source": "test"},
    )
    return factory, engine, task, store, executor


def _workflow(path):
    return Workflow(
        workflow_id="wf-publication-lifecycle",
        name="Publication lifecycle",
        steps=[
            StepDefinition(
                name="publish",
                type="tool_call",
                tool_call={
                    "tool": "artifact_publish",
                    "args": {"path": str(path), "mime_type": "application/json"},
                    "allow_side_effects": True,
                    "outputs": {"artifact_id": "{{ result.metadata.artifact_id }}"},
                },
            ),
            StepDefinition(
                name="complete",
                type="complete",
                complete={"summary": "{{ steps.publish.outputs.artifact_id }}"},
            ),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True], ids=["inline", "staged"])
async def test_publish_large_json_reference_receipt(
    tmp_path, monkeypatch, artifact_workflow_database, staged
):
    factory, engine, task, store, executor = await _publication_runtime(
        tmp_path, monkeypatch, artifact_workflow_database, staged=staged
    )
    content = json.dumps({"alerts": [{"id": i, "value": "pending"} for i in range(1500)]}).encode()
    assert len(content) > 32768
    path = tmp_path / "pending.json"
    path.write_bytes(content)
    result = await engine.execute_workflow(task, _workflow(path))
    async with factory() as db:
        artifacts = list((await db.scalars(select(ArtifactRecordRow))).all())
        run = await get_latest_step_run_for_task_step(db, task.task_id, "publish")
        saved = await get_task(db, task.task_id)
    assert len(artifacts) == 1, "\n".join(
        event.data["result"]
        for call in engine._providers.guardrails.record_events.call_args_list
        for event in call.kwargs["events"]
        if event.type == "tool_result"
    )
    artifact = artifacts[0]
    stored, _ = await store.async_load(artifact.namespace, artifact.object_id, artifact.filename)
    assert stored == content
    assert result.status == TaskStatus.COMPLETED, run.output
    assert run.output["outputs"] == {"artifact_id": artifact.artifact_id}
    assert "content_b64" not in json.dumps(run.output)
    assert "transfer_receipt" not in json.dumps(run.output)
    assert "sig=" not in json.dumps(run.output["attachments"])
    assert saved.workflow_state["step_outputs"]["publish"] == run.output
    assert len(json.dumps(run.output["outputs"])) < 256
    executor.tool_execute.assert_awaited_once()
    event_types = [
        event.type
        for call in engine._providers.guardrails.record_events.call_args_list
        for event in call.kwargs["events"]
    ]
    assert event_types.count("tool_call") == event_types.count("tool_result") == 1


async def _reload(factory, task):
    async with factory() as db:
        saved = await get_task(db, task.task_id)
    return task.model_copy(
        update={
            "status": TaskStatus.RUNNING,
            "workflow_state": WorkflowState.model_validate(saved.workflow_state),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["published", "persisted", "terminal"])
async def test_publish_recovery_does_not_replay(
    tmp_path, monkeypatch, artifact_workflow_database, boundary
):
    factory, engine, task, _, executor = await _publication_runtime(
        tmp_path, monkeypatch, artifact_workflow_database
    )
    path = tmp_path / "pending.json"
    path.write_text(json.dumps({"pending": "x" * 40000}))
    workflow = _workflow(path)
    if boundary == "published":
        original = engine._agent_loop.persist_controller_tool_output

        async def interrupt(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError

        monkeypatch.setattr(engine._agent_loop, "persist_controller_tool_output", interrupt)
    else:
        from cognis.core import workflow_engine as module

        original = module.update_step_run

        async def interrupt(db, step_run_id, **kwargs):
            if kwargs.get("status") == "approved":
                if boundary == "terminal":
                    await original(db, step_run_id, **kwargs)
                    await db.commit()
                raise asyncio.CancelledError
            return await original(db, step_run_id, **kwargs)

        monkeypatch.setattr(module, "update_step_run", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await engine.execute_workflow(task, workflow)
    async with factory() as db:
        run = await get_latest_step_run_for_task_step(db, task.task_id, "publish")
        artifacts = list((await db.scalars(select(ArtifactRecordRow))).all())
    assert len(artifacts) == 1
    if boundary != "published":
        assert "content_b64" not in json.dumps(run.output)
        assert "sig=" not in json.dumps(run.output)
    if boundary == "published":
        monkeypatch.setattr(engine._agent_loop, "persist_controller_tool_output", original)
    else:
        monkeypatch.setattr(module, "update_step_run", original)
    recovered = await _reload(factory, task)
    result = await engine.execute_workflow(recovered, workflow)
    executor.tool_execute.assert_awaited_once()
    async with factory() as db:
        artifacts_after = list((await db.scalars(select(ArtifactRecordRow))).all())
    assert len(artifacts_after) == 1
    if boundary == "published":
        assert result.status == TaskStatus.FAILED
        assert "not replayed" in result.workflow_state.step_outputs["publish"]["error"]
    else:
        assert result.status == TaskStatus.COMPLETED
        assert result.workflow_state.step_outputs["publish"]["outputs"] == {
            "artifact_id": artifacts[0].artifact_id
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True], ids=["inline", "staged"])
async def test_publish_stale_cas_has_no_revision_or_replay(
    tmp_path, monkeypatch, artifact_workflow_database, staged
):
    factory, engine, task, store, executor = await _publication_runtime(
        tmp_path, monkeypatch, artifact_workflow_database, staged=staged
    )
    pending = tmp_path / "pending.json"
    ack = tmp_path / "ack.json"
    pending.write_text('{"pending":true}')
    ack.write_text('{"pending":false}')
    workflow = _workflow(pending)
    first = workflow.steps[0]
    second = first.model_copy(deep=True)
    second.name = "ack"
    second.tool_call.args.update(
        {"path": str(ack), "supersedes": "{{ steps.publish.outputs.artifact_id }}"}
    )
    stale = second.model_copy(deep=True)
    stale.name = "stale"
    stale.tool_call.outputs = {"failed": "{{ result.is_error }}"}
    workflow.steps = [first, second, stale]
    result = await engine.execute_workflow(task, workflow)
    assert result.status == TaskStatus.FAILED
    assert result.workflow_state.step_outputs["stale"]["outputs"] == {"failed": True}
    assert "supersedes_not_latest" in result.workflow_state.step_outputs["stale"]["error"]
    async with factory() as db:
        artifacts = list((await db.scalars(select(ArtifactRecordRow))).all())
    assert sorted(row.revision for row in artifacts) == [1, 2]
    latest = next(row for row in artifacts if row.revision == 2)
    stored, _ = await store.async_load(latest.namespace, latest.object_id, latest.filename)
    assert stored == ack.read_bytes()
    assert latest.artifact_id == result.workflow_state.step_outputs["ack"]["outputs"]["artifact_id"]
    async with factory() as db:
        run = await get_latest_step_run_for_task_step(db, task.task_id, "stale")
    recovered = await engine._recover_deterministic_step(run, step_def=stale, workflow=workflow)
    assert recovered.step_run_status == "failed"
    assert executor.tool_execute.await_count == 3
    async with factory() as db:
        assert len(list((await db.scalars(select(ArtifactRecordRow))).all())) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["denied", "missing_source", "foreign_source", "unresolved_lazy"])
async def test_publish_source_and_policy_fail_closed(
    tmp_path, monkeypatch, artifact_workflow_database, case
):
    factory, engine, task, store, executor = await _publication_runtime(
        tmp_path, monkeypatch, artifact_workflow_database
    )
    path = tmp_path / "pending.json"
    path.write_text('{"pending":true}')
    workflow = _workflow(path)
    workflow.steps[0].tool_call.outputs = {"failed": "{{ result.is_error }}"}
    before = 0
    if case == "denied":
        engine._providers.guardrails.evaluate.return_value = EvaluationResult(
            decision="deny", call_id="publication-denied", reason="Test policy denial"
        )
    elif case == "unresolved_lazy":
        workflow.steps[0].tool_call.args["path"] = (
            "$artifact:tool_artifact:call_unavailable:binary.filename"
        )
    else:
        artifact_id = store.generate_id("art")
        if case == "foreign_source":
            async with factory() as db:
                await create_user(db, email="other@example.com", name="Other", password_hash="test")
                await create_agent(
                    db, agent_id="other-agent", owner_email="other@example.com", name="Other"
                )
                await create_conversation(
                    db,
                    conversation_id="other-conversation",
                    user_email="other@example.com",
                    agent_id="other-agent",
                    context_type="web",
                )
                await db.commit()
            row, _ = await write_artifact_document(
                session_factory=factory,
                artifact_store=store,
                actor=ContentActor(
                    user_email="other@example.com",
                    agent_id="other-agent",
                    conversation_id="other-conversation",
                    session_id=None,
                ),
                content=b'{"private":true}',
                filename="private.json",
                mime_type="application/json",
                kind="file",
                purpose="artifact_publish",
            )
            artifact_id = row.artifact_id
            before = 1
        workflow.steps[0].tool_call.args["path"] = f"$artifact:{artifact_id}.filename"
    result = await engine.execute_workflow(task, workflow)
    assert result.status == TaskStatus.FAILED
    executor.tool_execute.assert_not_awaited()
    async with factory() as db:
        assert len(list((await db.scalars(select(ArtifactRecordRow))).all())) == before
    assert "complete" not in result.workflow_state.step_outputs


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["forged_metadata", "error_metadata", "body", "receipt"])
async def test_unmaterialized_publisher_cannot_produce_receipt(
    tmp_path, monkeypatch, artifact_workflow_database, case
):
    factory, engine, task, store, executor = await _publication_runtime(
        tmp_path, monkeypatch, artifact_workflow_database
    )
    path = tmp_path / "pending.json"
    path.write_text('{"pending":true}')
    attachments = None
    if case == "body":
        attachments = [{"content_b64": 42}]
    elif case == "receipt":
        attachments = [{"transfer_receipt": {"transfer_id": "unmaterialized-transfer"}}]
    executor.tool_execute.side_effect = None
    executor.tool_execute.return_value = ToolResult(
        output="No controller publication occurred",
        is_error=case == "error_metadata",
        metadata={"artifact_id": store.generate_id("art")},
        attachments=attachments,
    )
    result = await engine.execute_workflow(task, _workflow(path))
    assert result.status == TaskStatus.FAILED
    assert "complete" not in result.workflow_state.step_outputs
    assert not result.workflow_state.step_outputs["publish"]["outputs"]
    async with factory() as db:
        assert not list((await db.scalars(select(ArtifactRecordRow))).all())


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["artifact", "url", "lazy", "deliverable"])
async def test_ordinary_reference_attachments_survive_step_persistence(
    tmp_path, monkeypatch, artifact_workflow_database, reference_kind
):
    factory, engine, task, store, executor = await _publication_runtime(
        tmp_path, monkeypatch, artifact_workflow_database
    )
    refs = {
        "artifact": {"artifact_id": store.generate_id("att"), "filename": "report.txt"},
        "url": {"url": "https://files.test/report.txt", "filename": "report.txt"},
        "lazy": {"artifact_id": "tool_artifact:call_accepted:binary"},
        "deliverable": {"artifact_id": store.generate_id("dlv")},
    }
    ref = refs[reference_kind]
    registry = engine._resolve_step_runtime.return_value.tool_registry
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="read_report",
                description="Return an ordinary reference attachment.",
                parameters={"type": "object"},
                source=ToolSource(type="executor"),
                read_only=True,
            )
        )
    )
    executor.tool_execute.side_effect = None
    executor.tool_execute.return_value = ToolResult(output="Read report", attachments=[ref])
    workflow = Workflow(
        workflow_id="wf-ordinary-attachments",
        name="Ordinary attachments",
        steps=[
            StepDefinition(
                name="read",
                type="tool_call",
                tool_call={
                    "tool": "read_report",
                    "outputs": {"attachments": "{{ result.attachments }}"},
                },
            ),
            StepDefinition(
                name="complete",
                type="complete",
                complete={
                    "summary": "Read report",
                    "outputs": {"attachments": "{{ steps.read.attachments }}"},
                },
            ),
        ],
    )
    result = await engine.execute_workflow(task, workflow)
    assert result.status == TaskStatus.COMPLETED
    assert result.workflow_state.step_outputs["read"]["attachments"] == [ref]
    assert result.workflow_state.step_outputs["complete"]["outputs"]["attachments"] == [ref]
    async with factory() as db:
        run = await get_latest_step_run_for_task_step(db, task.task_id, "read")
    assert run.output["attachments"] == [ref]
    assert executor.tool_execute.return_value.attachments == [ref]

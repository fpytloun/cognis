"""Tool-loop regressions with real SQLite deliverable persistence."""

import asyncio
from copy import copy, deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis import json_codec as json
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.agent_loop import WORKFLOW_POLICY, DeliverablePersistenceError, StepContext
from cognis.core.artifact_revisions import ContentActor, write_artifact_document
from cognis.core.daily_brief_contract import persisted_daily_brief_is_valid
from cognis.core.deliverable_authoring import ResolvedDeliverableAuthoring
from cognis.core.task_execution import (
    StaleTaskExecutionOwner,
    TaskExecutionFence,
    TaskExecutionStore,
    bind_task_execution_fence,
    reset_task_execution_fence,
)
from cognis.core.workflow_output_contract import step_is_task_output
from cognis.models.deliverable import PULSE_DAILY_SKELETON
from cognis.models.workflow import StepDefinition, StepInputConfig, WorkflowState
from cognis.store.models import (
    Agent,
    ArtifactGrantRow,
    Base,
    Conversation,
    Session,
    StepRun,
    Task,
    User,
)
from cognis.store.queries import get_deliverable, get_step_run
from cognis.tools.builtin.workflow import WRITE_DELIVERABLE_TOOL
from cognis.tools.introspection import validate_available_tool_call_with_context
from cognis.tools.native_validation import (
    NativeValidationContext,
    write_deliverable_validation_state_fingerprint,
)
from cognis.tools.registry import ToolRegistry
from tests.unit.test_agent_loop import (
    _RecordingGuardrails,
    _SingleTextLLM,
    _step_complete_test_context,
    _step_complete_test_loop,
)
from tests.unit.test_deliverable_media import _artifact, _daily_brief_arguments

PACKET = {"content": "Internal evidence packet: partial coverage", "format": "markdown"}


class ScriptedTools(_SingleTextLLM):
    def __init__(self, script):
        self.script = script
        self.calls = []

    async def stream_generate(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        index = len(self.calls) - 1
        if index >= len(self.script):
            raise AssertionError(f"Unexpected retry: {messages[-3:]}")
        name, arguments = self.script[index]
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call_{uuid4().hex}",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ]
                    }
                }
            ]
        }


async def database_context(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    steps = [
        StepDefinition(name="collect", type="run"),
        StepDefinition(name="publish", type="run"),
    ]
    ctx = _step_complete_test_context(steps[0])
    ctx.tool_registry = ToolRegistry()
    ctx.policy = WORKFLOW_POLICY
    ctx.workflow_steps = steps
    ctx.step_index = 0
    ctx.task_id = f"task_{uuid4().hex}"
    ctx.step_run_id = f"sr_{uuid4().hex}"
    ctx.task_title = "Daily Brief"
    ctx.session.conversation_id = ctx.conversation.conversation_id
    ctx.conversation.user_email = ctx.session.user_email
    ctx.conversation.agent_id = ctx.agent.agent_id
    async with factory() as db:
        db.add(
            Task(
                task_id=ctx.task_id,
                title=ctx.task_title,
                created_by=ctx.session.user_email,
                agent_id=ctx.agent.agent_id,
                status="running",
            )
        )
        db.add(
            Session(
                session_id=ctx.session.session_id,
                conversation_id=ctx.session.conversation_id,
                user_email=ctx.session.user_email,
                agent_id=ctx.agent.agent_id,
                activity_scope_id=ctx.session.session_id,
            )
        )
        db.add(
            StepRun(
                step_run_id=ctx.step_run_id,
                task_id=ctx.task_id,
                step_name=ctx.step_definition.name,
                step_type="run",
                agent_id=ctx.agent.agent_id,
                status="running",
                require_deliverable=True,
            )
        )
        await db.commit()
    return factory, ctx


@pytest.mark.asyncio
async def test_published_conversation_json_uses_normal_validation_and_write_pipeline(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory, ctx = await database_context(engine)
        ctx.tool_registry = ToolRegistry()
        store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        ctx.task_title = "Publication regression"
        async with factory() as db:
            db.add(User(email=ctx.session.user_email, name="Owner", password_hash="x"))
            db.add(
                Agent(
                    agent_id=ctx.agent.agent_id,
                    owner_email=ctx.session.user_email,
                    name="Publisher",
                    status="active",
                )
            )
            db.add(
                Conversation(
                    conversation_id=ctx.conversation.conversation_id,
                    user_email=ctx.session.user_email,
                    agent_id=ctx.agent.agent_id,
                    context_type="web",
                )
            )
            await db.commit()
        payload = deepcopy(PULSE_DAILY_SKELETON)
        payload["title"] = "Publication regression"
        payload["metadata"].pop("presentation")
        published, _ = await write_artifact_document(
            session_factory=factory,
            artifact_store=store,
            actor=ContentActor(
                user_email=ctx.session.user_email,
                agent_id=ctx.agent.agent_id,
                conversation_id=ctx.session.conversation_id,
                session_id=ctx.session.session_id,
            ),
            content=json.dumps(payload).encode(),
            filename="pulse.json",
            mime_type="application/json",
            kind="file",
            purpose="artifact_publish",
        )
        assert published.conversation_id == ctx.session.conversation_id
        assert published.status == "attached"
        arguments = {
            "action": "rich:pulse",
            "payload_artifact": {"artifact_id": published.artifact_id},
        }
        validation_context = NativeValidationContext(
            session_factory=factory,
            artifact_store=store,
            actor_email=ctx.session.user_email,
            conversation_id=ctx.session.conversation_id,
            conversation_agent_id=ctx.agent.agent_id,
        )
        for denied_context in (
            replace(
                validation_context,
                actor_email="other@example.com",
                task_description="Use daily_brief_v13.",
            ),
            replace(
                validation_context,
                conversation_agent_id=f"agent_{uuid4().hex}",
                task_description="Use daily_brief_v13.",
            ),
        ):
            denied = await validate_available_tool_call_with_context(
                [WRITE_DELIVERABLE_TOOL], "write_deliverable", arguments, denied_context
            )
            assert [error["code"] for error in denied["errors"]] == ["artifact_unavailable"]
        validated = await validate_available_tool_call_with_context(
            [WRITE_DELIVERABLE_TOOL], "write_deliverable", arguments, validation_context
        )
        assert validated["valid"] is True, validated
        inline = {"action": "rich:pulse", "payload": payload}
        llm = ScriptedTools(
            [
                ("write_deliverable", arguments),
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": arguments}),
                ("write_deliverable", inline),
                ("write_deliverable", arguments),
                ("step_todo_write", {"todos": [{"content": "Publish", "status": "completed"}]}),
                ("step_complete", {"summary": "Published"}),
            ]
        )
        loop = _step_complete_test_loop(llm)
        loop.session_manager.session_factory = factory
        loop._session_factory = factory
        loop.artifact_store = store
        result = await loop.run_step(ctx)
        assert result is not None and result.deliverable_id
        current = await loop._get_current_deliverable(ctx)
        assert current.format == "rich"
        assert current.rich_payload["metadata"]["presentation"] == "pulse"
        results = [
            json.loads(item["content"]) for item in llm.calls[-1] if item.get("role") == "tool"
        ]
        assert sum(item.get("reason") == "missing_validation" for item in results) == 1
        assert sum(item.get("reason") == "stale_validation" for item in results) == 1
        assert any(item.get("status") == "buffered" for item in results)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("secondary_executor", [False, True])
async def test_collector_buffered_write_completes_without_pulse_deadlock(
    tmp_path, secondary_executor
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory, ctx = await database_context(engine)
        if secondary_executor:
            ctx.executor_agent = ctx.agent.model_copy(update={"agent_id": f"agent_{uuid4().hex}"})
            ctx.agent.agent_type = "secondary"
        llm = ScriptedTools(
            [
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": PACKET}),
                ("write_deliverable", PACKET),
                ("step_todo_write", {"todos": [{"content": "Collect", "status": "completed"}]}),
                ("step_complete", {"summary": "Packet delivered"}),
                (
                    "write_deliverable",
                    {
                        "content": PACKET["content"] + "\nAdditional synthetic scope note.",
                        "format": "markdown",
                    },
                ),
                ("step_complete", {"summary": "Packet delivered"}),
            ]
        )
        loop = _step_complete_test_loop(llm)
        loop.session_manager.session_factory = factory
        loop._session_factory = factory
        loop.artifact_store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        result = await loop.run_step(ctx)
        payloads = [
            json.loads(message["content"])
            for message in llm.calls[-1]
            if message.get("role") == "tool"
        ]
        assert any(item.get("status") == "buffered" for item in payloads), json.dumps(payloads)
        assert result is not None
        assert result.content == PACKET["content"], json.dumps(payloads)
        async with factory() as db:
            step = await get_step_run(db, ctx.step_run_id)
            row = await get_deliverable(db, result.deliverable_id)
            assert step.deliverable_id == row.deliverable_id
            assert row.step_run_id == ctx.step_run_id
            assert "daily_brief_contract" not in (row.render_metadata or {})
        # A fresh context must use persistence, not the original warm cache.
        cold = StepContext(
            step_definition=ctx.step_definition,
            session=copy(ctx.session),
            conversation=ctx.conversation,
            agent=ctx.agent,
            executor_agent=ctx.executor_agent,
            policy=WORKFLOW_POLICY,
            task_id=ctx.task_id,
            task_title=ctx.task_title,
            step_run_id=ctx.step_run_id,
            workflow_steps=ctx.workflow_steps,
            step_index=0,
            todos=[{"content": "Collect", "status": "completed"}],
        )
        loaded = await loop._get_current_deliverable(cold)
        assert loaded.deliverable_id == result.deliverable_id
        assert (await loop._finalization_instruction(cold))["required_action"] == "step_complete"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["task", "agent", "attempt", "superseded", "artifact"])
async def test_current_packet_cannot_complete_a_foreign_or_stale_step(tmp_path, foreign):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory, ctx = await database_context(engine)
        loop = _step_complete_test_loop(ScriptedTools([]))
        loop.session_manager.session_factory = factory
        loop.artifact_store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        current = await loop._write_step_deliverable(
            ctx,
            authoring=ResolvedDeliverableAuthoring(
                content=PACKET["content"],
                format="markdown",
                title=None,
                outputs={},
                rich=None,
            ),
        )
        async with factory() as db:
            step = await get_step_run(db, ctx.step_run_id)
            if foreign == "task":
                step.task_id = f"task_{uuid4().hex}"
            elif foreign == "agent":
                step.agent_id = f"agent_{uuid4().hex}"
            elif foreign == "attempt":
                task = await db.get(Task, ctx.task_id)
                task.attempt_number += 1
            elif foreign == "superseded":
                step.status = "superseded"
            else:
                row = await get_deliverable(db, current.deliverable_id)
                row.step_run_id = f"sr_{uuid4().hex}"
            await db.commit()
        assert await loop._get_current_deliverable(ctx) is None
        loop._clear_cached_deliverable(ctx)
        assert await loop._get_current_deliverable(ctx) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_final_producer_rejects_text_and_repairs_invalid_existing_pulse(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory, ctx = await database_context(engine)
        store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        collector_llm = ScriptedTools(
            [
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": PACKET}),
                ("write_deliverable", PACKET),
                ("step_complete", {"summary": "Partial packet delivered"}),
            ]
        )
        collector = _step_complete_test_loop(collector_llm)
        collector.session_manager.session_factory = factory
        collector._session_factory = factory
        collector.artifact_store = store
        packet = await collector.run_step(ctx)
        assert packet is not None and packet.deliverable_id is not None
        source_agent = ctx.agent
        steps = ctx.workflow_steps
        steps[1].input = StepInputConfig(type="last", source="collect")
        target_session = copy(ctx.session)
        target_session.session_id = f"sess_{uuid4().hex}"
        target_session.conversation_id = f"conv_{uuid4().hex}"
        ctx = StepContext(
            tool_registry=ToolRegistry(),
            step_definition=steps[1],
            session=target_session,
            conversation=SimpleNamespace(
                conversation_id=target_session.conversation_id,
                user_email=target_session.user_email,
                agent_id=f"agent_{uuid4().hex}",
            ),
            agent=source_agent.model_copy(update={"agent_id": f"agent_{uuid4().hex}"}),
            policy=WORKFLOW_POLICY,
            task_id=ctx.task_id,
            task_title=ctx.task_title,
            step_run_id=f"sr_{uuid4().hex}",
            workflow_steps=steps,
            step_index=1,
            workflow_state=WorkflowState(step_outputs={"collect": packet.model_dump(mode="json")}),
        )
        ctx.conversation.agent_id = ctx.agent.agent_id
        ctx.session.agent_id = ctx.agent.agent_id
        async with factory() as db:
            db.add(
                Agent(
                    agent_id=ctx.agent.agent_id,
                    owner_email=ctx.session.user_email,
                    name="Final producer",
                    status="active",
                )
            )
            db.add(
                Conversation(
                    conversation_id=ctx.session.conversation_id,
                    user_email=ctx.session.user_email,
                    agent_id=ctx.agent.agent_id,
                    context_type="task",
                    context_ref=ctx.task_id,
                )
            )
            db.add(
                Session(
                    session_id=ctx.session.session_id,
                    conversation_id=ctx.session.conversation_id,
                    user_email=ctx.session.user_email,
                    agent_id=ctx.agent.agent_id,
                    activity_scope_id=ctx.session.session_id,
                )
            )
            db.add(
                StepRun(
                    step_run_id=ctx.step_run_id,
                    task_id=ctx.task_id,
                    step_name=ctx.step_definition.name,
                    step_type="run",
                    agent_id=ctx.agent.agent_id,
                    status="running",
                    require_deliverable=True,
                )
            )
            await db.commit()
        arguments, evidence = _daily_brief_arguments()
        factory.artifact_store = store
        for artifact_id in evidence:
            await _artifact(
                factory, artifact_id=artifact_id, owner=ctx.session.user_email, conversation_id=None
            )
        ctx.materialized_artifact_evidence = evidence
        async with factory() as db:
            db.add_all(
                ArtifactGrantRow(
                    owner_email=ctx.session.user_email,
                    content_ref=artifact_id,
                    agent_id=ctx.agent.agent_id,
                )
                for artifact_id in evidence
            )
            await db.commit()
        ctx.tool_execution_counts = {"artifact_read": len(evidence)}
        llm = ScriptedTools(
            [
                ("step_todo_write", {"todos": [{"content": "Publish", "status": "completed"}]}),
                ("step_complete", {"summary": "Publish"}),
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": PACKET}),
                ("write_deliverable", PACKET),
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": arguments}),
                ("write_deliverable", arguments),
                ("step_complete", {"summary": "Published"}),
            ]
        )
        loop = _step_complete_test_loop(llm)
        loop.session_manager.session_factory = factory
        loop._session_factory = factory
        loop.artifact_store = store
        # Model a previously persisted invalid text revision, not a forged receipt.
        old = await loop._write_step_deliverable(
            ctx,
            authoring=ResolvedDeliverableAuthoring(
                content=PACKET["content"],
                format="markdown",
                title=None,
                outputs={},
                rich=None,
            ),
        )
        result = await loop.run_step(ctx)
        assert result is not None
        received = json.dumps(llm.calls[0])
        assert PACKET["content"] in received
        assert source_agent.agent_id != ctx.agent.agent_id
        assert ctx.workflow_state.step_outputs["collect"]["deliverable_id"] == packet.deliverable_id
        assert ctx.prior_context is None
        assert ctx.agent.permissions == source_agent.permissions
        # Delivery of partial evidence is not authorization for external mutations:
        # the target script only performs validation and final publication.
        assert all(
            name in {"step_complete", "step_todo_write", "validate_tool_call", "write_deliverable"}
            for name, _ in llm.script
        )
        assert result.deliverable_id != old.deliverable_id
        results = [
            json.loads(item["content"]) for item in llm.calls[-1] if item.get("role") == "tool"
        ]
        assert any(item.get("reason") == "deliverable_invalid" for item in results), json.dumps(
            results
        )
        assert not any(item.get("reason") == "finalization_required" for item in results)
        assert any(item.get("valid") is False for item in results)
        current = await loop._get_current_deliverable(ctx)
        assert persisted_daily_brief_is_valid(current)
        assert current.version == old.version + 1
        loop._clear_cached_deliverable(ctx)
        assert persisted_daily_brief_is_valid(await loop._get_current_deliverable(ctx))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("ambiguity", ["conditional", "route", "gate", "unknown", "index"])
def test_ambiguous_task_output_ownership_retains_strict_contract(ambiguity):
    steps = [StepDefinition(name="a", type="run"), StepDefinition(name="b", type="run")]
    assert step_is_task_output(steps, steps[0], 0) is False
    assert step_is_task_output(steps, steps[1], 1) is True
    if ambiguity == "conditional":
        steps[1].when = "metadata.allowed"
    elif ambiguity == "route":
        from cognis.models.workflow import OutcomeRoute

        steps[0].outcome_routes = [OutcomeRoute(status="rejected", action="cancel")]
    elif ambiguity == "gate":
        steps.append(StepDefinition(name="check", type="gate"))
    elif ambiguity == "unknown":
        steps = []
    index = 2 if ambiguity == "index" else 0
    current = steps[0] if steps else StepDefinition(name="a", type="run")
    assert step_is_task_output(steps, current, index) is None


@pytest.mark.asyncio
async def test_missing_and_superseded_packets_follow_canonical_pointer(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory, ctx = await database_context(engine)
        loop = _step_complete_test_loop(ScriptedTools([]))
        loop.session_manager.session_factory = factory
        loop.artifact_store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        ctx.todos = [{"content": "Collect", "status": "completed"}]
        assert await loop._get_current_deliverable(ctx) is None
        assert (await loop._finalization_instruction(ctx))[
            "required_action"
        ] == "write_deliverable_then_step_complete"
        authoring = ResolvedDeliverableAuthoring(
            content=PACKET["content"],
            format="markdown",
            title=None,
            outputs={},
            rich=None,
        )
        old = await loop._write_step_deliverable(ctx, authoring=authoring)
        new = await loop._write_step_deliverable(ctx, authoring=authoring)
        loop._cache_deliverable(ctx, old)
        assert (await loop._get_current_deliverable(ctx)).deliverable_id == new.deliverable_id
        async with factory() as db:
            old_row = await get_deliverable(db, old.deliverable_id)
            assert old_row.status == "superseded"
            step = await get_step_run(db, ctx.step_run_id)
            step.deliverable_id = old.deliverable_id
            await db.commit()
        assert await loop._get_current_deliverable(ctx) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lost_execution_fence_cannot_publish_packet(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory, ctx = await database_context(engine)
        loop = _step_complete_test_loop(ScriptedTools([]))
        loop.session_manager.session_factory = factory
        loop.artifact_store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        store = TaskExecutionStore(
            factory, owner_id=f"owner_{uuid4().hex}", max_active_global=1, max_active_per_agent=1
        )
        claim = await store.claim_existing(ctx.task_id, statuses={"running"})
        assert claim is not None
        fence = TaskExecutionFence(store=store, claim=claim, cancel_event=asyncio.Event())
        ctx.execution_fence = fence
        await fence.assert_current()
        async with factory() as db:
            task = await db.get(Task, ctx.task_id)
            task.attempt_number += 1
            await db.commit()
        token = bind_task_execution_fence(fence)
        try:
            with pytest.raises(
                DeliverablePersistenceError, match="StaleTaskExecutionOwner"
            ) as raised:
                await loop._write_step_deliverable(
                    ctx,
                    authoring=ResolvedDeliverableAuthoring(
                        content=PACKET["content"],
                        format="markdown",
                        title=None,
                        outputs={},
                        rich=None,
                    ),
                )
            assert isinstance(raised.value.__cause__, StaleTaskExecutionOwner)
        finally:
            reset_task_execution_fence(token)
        async with factory() as db:
            assert (await get_step_run(db, ctx.step_run_id)).deliverable_id is None
    finally:
        await engine.dispose()


def test_validation_fingerprint_binds_output_scope_step_and_actor():
    context = NativeValidationContext(
        daily_brief_task_output=False,
        deliverable_step_run_id=f"sr_{uuid4().hex}",
        actor_email="collector@example.test",
        current_agent_id=f"agent_{uuid4().hex}",
    )
    initial = write_deliverable_validation_state_fingerprint(context, schema_hash=None)
    for delta in [
        {"daily_brief_task_output": True},
        {"deliverable_step_run_id": f"sr_{uuid4().hex}"},
        {"actor_email": "consumer@example.test"},
        {"current_agent_id": f"agent_{uuid4().hex}"},
    ]:
        assert (
            write_deliverable_validation_state_fingerprint(
                replace(context, **delta),
                schema_hash=None,
            )
            != initial
        )


@pytest.mark.asyncio
async def test_task_document_is_replayed_before_later_step_progress(tmp_path):
    from cognis.api.chat_v2.event_store import RawSessionEvent
    from cognis.api.chat_v2.normalizer import normalize_session_events
    from cognis.api.chat_v2.projector import project_timeline

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory, ctx = await database_context(engine)
    loop = _step_complete_test_loop(
        ScriptedTools(
            [
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": PACKET}),
                ("write_deliverable", PACKET),
                (
                    "step_todo_write",
                    {"todos": [{"content": "Check published output", "status": "completed"}]},
                ),
                ("step_complete", {"summary": "Published and checked"}),
            ]
        ),
    )
    loop.session_manager.session_factory = factory
    loop._session_factory = factory
    loop.artifact_store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    recorder = _RecordingGuardrails()
    loop.providers.guardrails = recorder
    try:
        result = await loop.run_step(ctx)
        assert result is not None and result.deliverable_id
        raw = [
            RawSessionEvent(
                store_id="intaris",
                session_id=ctx.session.session_id,
                seq=index,
                type=event.type,
                data=event.data,
            )
            for index, event in enumerate(recorder.recorded_events, 1)
        ]
        replay = project_timeline(normalize_session_events(raw).events).timeline.items
        documents = [item for item in replay if item.kind == "assistant_deliverable"]
        assert [item.deliverable_id for item in documents] == [result.deliverable_id]
        later = next(
            item
            for item in replay
            if item.kind == "tool_call" and item.tool_name == "step_complete"
        )
        assert replay.index(documents[0]) < replay.index(later)
        assert project_timeline(normalize_session_events(raw).events).timeline.items == replay
    finally:
        await engine.dispose()

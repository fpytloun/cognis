"""Routed two-primary workflow through the engine, loop and real artifact DB.

External LLM/session services are local doubles. StepContext, agent resolution,
outcome routing, task/step rows and deliverable publication are not mocked.
The graph matches the reported v2 handoff; source prompts contain no work data.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis import json_codec as json
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.agent_loop import AgentLoop
from cognis.core.session import SessionManager
from cognis.core.task_execution import (
    TaskExecutionFence,
    TaskExecutionStore,
    bind_task_execution_fence,
    reset_task_execution_fence,
)
from cognis.core.tool_router import ToolRouter
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_output_contract import step_is_task_output
from cognis.models.task import TaskModel
from cognis.models.tool import EvaluationResult, NativeToolDefinition, ToolCall, ToolSource
from cognis.models.workflow import Workflow
from cognis.store.models import Agent, ArtifactGrantRow, Base, Conversation, Session, StepRun, Task
from cognis.store.queries import get_deliverable
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.unit.test_agent_loop import _NoopGuardrails, _SessionStub, _step_complete_test_loop
from tests.unit.test_deliverable_media import _artifact, _daily_brief_arguments
from tests.unit.test_step_deliverable_contract import PACKET, ScriptedTools
from tests.unit.test_tool_result_settlement import _Guardrails
from tests.unit.test_tool_router import _Executor


class EventGuardrails(_NoopGuardrails):
    """Per-session canonical append/read double with idempotent tool settlement."""

    def __init__(self):
        self.sessions = {}
        self.policies = {}
        self.denied_agent_id = None

    async def update_session_policy(self, session_id, *, agent_id, policy, **kwargs):
        self.policies[session_id] = (agent_id, policy)

    async def evaluate(self, session_id, tool_name, arguments, context):
        if tool_name == "synthetic_identity_probe":
            identity, _ = self.policies[session_id]
            return EvaluationResult(
                decision="deny" if identity == self.denied_agent_id else "approve",
                call_id=f"eval_{uuid4().hex}",
            )
        return await super().evaluate(session_id, tool_name, arguments, context)

    async def read_events(self, *, session_id, **kwargs):
        return await self.sessions.setdefault(session_id, _Guardrails([])).read_events(**kwargs)

    async def record_events(self, *, session_id, events, idempotency_key=None, **kwargs):
        return await self.sessions.setdefault(session_id, _Guardrails([])).record_events(
            events=events,
            idempotency_key=idempotency_key or f"append_{uuid4().hex}",
            **kwargs,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "rejected"])
async def test_routed_two_primary_handoff_keeps_collector_internal_and_final_strict(
    tmp_path, outcome, snapshot=None
):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'workflow.db'}")
    try:
        async with database.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(database, expire_on_commit=False)
        owner = "handoff@example.test"
        collector_id, final_id = (f"agent_{uuid4().hex}" for _ in range(2))
        workflow = Workflow.model_validate(
            {
                "workflow_id": f"wf_{uuid4().hex}",
                "name": "synthetic-catalog-evidence-handoff",
                "version": 2,
                "steps": [
                    {
                        "name": "catalog_evidence",
                        "type": "run",
                        "agent_override": collector_id,
                        "agent_profile_id": "developer",
                        "input": None,
                        "require_deliverable": True,
                        "prompt": "Deliver synthetic partial evidence.",
                        "outcome_routes": [{"status": "rejected", "action": "continue"}],
                        "completion": {
                            "evaluate": False,
                            "max_attempts": 1,
                            "on_exhausted": "fail",
                        },
                    },
                    {
                        "name": "execute",
                        "type": "run",
                        "agent_override": final_id,
                        "agent_profile_id": "default",
                        "require_deliverable": True,
                        "revision": {"allowed": True},
                        "metadata_contract": {
                            "fields": [
                                {
                                    "name": "result_status",
                                    "type": "string",
                                    "required": True,
                                    "enum": ["completed", "blocked"],
                                },
                                {"name": "verification", "type": "object", "required": True},
                            ]
                        },
                        "input": {"type": "last", "source": "catalog_evidence"},
                        "prompt": "Gate partial coverage. Do not modify catalog entries or attachments.",
                        "completion": {
                            "evaluate": False,
                            "max_attempts": 3,
                            "on_exhausted": "fail",
                        },
                    },
                ],
            }
        )
        if snapshot is not None:
            from copy import deepcopy

            spec = deepcopy(snapshot["workflow"])
            spec["name"] = spec["workflow_id"]
            for step in spec["steps"]:
                if step["agent_override"] is not None:
                    step["agent_override"] = {"collector": collector_id, "publisher": final_id}[
                        step["agent_override"]
                    ]
            workflow = Workflow.model_validate(spec)
        attempt = snapshot["attempt_number"] if snapshot else 2
        assert all(step.next is None for step in workflow.steps)
        task = TaskModel(
            task_id=f"task_{uuid4().hex}",
            title="Daily brief",
            description="Synthetic handoff regression",
            created_by=owner,
            agent_id=final_id,
            workflow_id=workflow.workflow_id,
            status="running",
            attempt_number=attempt,
        )
        async with factory() as db:
            for agent_id, name in [(collector_id, "Lumi"), (final_id, "Riker")]:
                db.add(
                    Agent(
                        agent_id=agent_id,
                        owner_email=owner,
                        name=name,
                        agent_type="primary",
                        status="active",
                        agent_profiles={"developer": {}},
                    )
                )
            db.add(
                Task(
                    task_id=task.task_id,
                    title=task.title,
                    description=task.description,
                    created_by=owner,
                    agent_id=final_id,
                    workflow_id=workflow.workflow_id,
                    status="running",
                    attempt_number=attempt,
                )
            )
            await db.commit()
        pulse, evidence = _daily_brief_arguments()
        store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        factory.artifact_store = store
        for artifact_id in evidence:
            await _artifact(factory, artifact_id=artifact_id, owner=owner, conversation_id=None)
        async with factory() as db:
            db.add_all(
                ArtifactGrantRow(owner_email=owner, content_ref=artifact_id, agent_id=final_id)
                for artifact_id in evidence
            )
            await db.commit()
        completion = {"summary": "Partial packet delivered"}
        if outcome == "rejected":
            completion["outcome"] = {"status": "rejected", "reason": "Partial coverage"}
        llm = ScriptedTools(
            [
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": PACKET}),
                ("write_deliverable", PACKET),
                ("step_complete", completion),
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": PACKET}),
                ("write_deliverable", PACKET),
                (
                    "step_complete",
                    {
                        "summary": "Cannot publish text",
                        "metadata": {"result_status": "blocked", "verification": {}},
                    },
                ),
                ("validate_tool_call", {"tool": "write_deliverable", "arguments": pulse}),
                ("write_deliverable", pulse),
                (
                    "step_complete",
                    {
                        "summary": "Coverage gated; no external mutations",
                        "metadata": {
                            "result_status": "blocked",
                            "verification": {"external_mutations": False},
                        },
                    },
                ),
            ]
        )
        loop = _step_complete_test_loop(llm)
        loop.providers.guardrails = EventGuardrails()
        loop._session_factory = factory
        manager = loop.session_manager
        manager.session_factory = factory
        policy_manager = SessionManager(factory, loop.providers, loop.session_cache)
        manager.refresh_intaris_session_policy = policy_manager.refresh_intaris_session_policy
        loop.providers.guardrails.denied_agent_id = final_id
        loop.session_cache.get_entry = lambda session_id: None

        async def create_session(**kwargs):
            conversation_id, session_id = f"conv_{uuid4().hex}", f"sess_{uuid4().hex}"
            async with factory() as db:
                db.add(
                    Conversation(
                        conversation_id=conversation_id,
                        user_email=owner,
                        agent_id=kwargs["agent_id"],
                        context_type="task",
                        context_ref=task.task_id,
                    )
                )
                db.add(
                    Session(
                        session_id=session_id,
                        conversation_id=conversation_id,
                        user_email=owner,
                        agent_id=kwargs["agent_id"],
                        activity_scope_id=session_id,
                    )
                )
                await db.commit()
            session = _SessionStub(
                session_id=session_id,
                intaris_session_id=session_id,
                conversation_id=conversation_id,
                user_email=owner,
                agent_id=kwargs["agent_id"],
                parent_session_id=None,
            )
            session.delegation_mode = "primary"
            return SimpleNamespace(
                conversation_id=conversation_id,
                user_email=owner,
                agent_id=kwargs["agent_id"],
                title=None,
                title_source="unset",
            ), session

        manager.create_conversation_with_root_session = create_session
        loop.artifact_store = store
        contexts, access_contexts = [], []
        real_run_step = loop.run_step

        async def observe_step(ctx, **kwargs):
            contexts.append(ctx)
            start = len(llm.calls)
            guardrails = loop.providers.guardrails
            identity, policy = guardrails.policies[ctx.session.intaris_session_id]
            assert identity == ctx.agent.agent_id
            assert policy["interaction_mode"] == ctx.interaction_mode
            registry = ToolRegistry()
            registry.register(
                RegisteredTool(
                    definition=NativeToolDefinition(
                        name="synthetic_identity_probe",
                        description="Local synthetic identity probe",
                        parameters={"type": "object", "properties": {}},
                        source=ToolSource(type="executor"),
                        read_only=True,
                        non_bypassable=True,
                    )
                )
            )
            executor = _Executor()
            probe = await ToolRouter(guardrails=guardrails).execute(
                ToolCall(
                    call_id=f"call_{uuid4().hex}", name="synthetic_identity_probe", arguments={}
                ),
                ctx.session,
                ctx.agent,
                registry,
                executor,
            )
            assert probe.metadata["evaluation"]["decision"] == (
                "approve" if ctx.agent.agent_id == collector_id else "deny"
            )
            assert executor.calls == (1 if ctx.agent.agent_id == collector_id else 0)
            if ctx.agent.agent_id == final_id:
                # Previously materialized, authorized synthetic continuation evidence.
                ctx.materialized_artifact_evidence = evidence
                ctx.tool_execution_counts = {"artifact_read": len(evidence)}
            output = await real_run_step(ctx, **kwargs)
            if ctx.agent.agent_id == collector_id:
                tool_results = [
                    json.loads(message["content"])
                    for messages in llm.calls[start:]
                    for message in messages
                    if message.get("role") == "tool"
                ]
                assert any(item.get("status") == "buffered" for item in tool_results), json.dumps(
                    [item for item in tool_results if item.get("reason") == "invalid_tool_call"]
                )
            return output

        loop.run_step = observe_step

        async def runtime_factory(**kwargs):
            access_contexts.append(kwargs["access_context"])
            return SimpleNamespace(
                tool_registry=ToolRegistry(),
                rebuild_tool_registry=None,
                refresh_executor_pool=None,
                executor_connection=None,
                executor_environment=None,
                runtime_info={},
                cleanup=lambda: asyncio.sleep(0),
            )

        engine = WorkflowEngine(
            session_factory=factory,
            providers=loop.providers,
            agent_loop=loop,
            step_evaluator=SimpleNamespace(),
            workflow_registry=SimpleNamespace(),
            session_manager=manager,
            event_bus=loop.event_bus,
            pause_waiter=loop.pause_waiter,
            session_cache=loop.session_cache,
            step_runtime_factory=runtime_factory,
        )
        execution_store = TaskExecutionStore(
            factory,
            owner_id=f"owner_{uuid4().hex}",
            max_active_global=1,
            max_active_per_agent=1,
        )
        claim = await execution_store.claim_existing(task.task_id, statuses={"running"})
        assert claim is not None
        fence = TaskExecutionFence(store=execution_store, claim=claim, cancel_event=asyncio.Event())
        token = bind_task_execution_fence(fence)
        try:
            result = await asyncio.wait_for(engine.execute_workflow(task, workflow), timeout=30)
        finally:
            reset_task_execution_fence(token)
        assert result.status == "completed"
        assert [ctx.agent.agent_id for ctx in contexts] == [collector_id, final_id]
        assert [access.agent_id for access in access_contexts] == [collector_id, final_id]
        assert contexts[0].step_index == 0 and contexts[1].step_index == 1
        assert all(ctx.execution_fence is fence for ctx in contexts)
        assert AgentLoop._daily_brief_task_output(contexts[0]) is False
        assert AgentLoop._daily_brief_task_output(contexts[1]) is True
        assert contexts[1].prior_context is None
        assert contexts[0].session.session_id != contexts[1].session.session_id
        assert PACKET["content"] in json.dumps(llm.calls[3])
        assert not any(message.get("role") == "tool" for message in llm.calls[3])
        results = [
            json.loads(message["content"])
            for messages in llm.calls
            for message in messages
            if message.get("role") == "tool"
        ]
        assert any(item.get("status") == "buffered" for item in results)
        assert any(item.get("reason") == "invalid_tool_call" for item in results)
        assert any(item.get("reason") == "deliverable_missing" for item in results)
        async with factory() as db:
            rows = (
                await db.scalars(
                    select(StepRun)
                    .where(StepRun.task_id == task.task_id)
                    .order_by(StepRun.started_at)
                )
            ).all()
            assert len(rows) == len(workflow.steps)
            assert [row.agent_id for row in rows[:2]] == [collector_id, final_id]
            assert all(
                row.attempt_number == attempt and row.require_deliverable for row in rows[:2]
            )
            packet = await get_deliverable(db, rows[0].deliverable_id)
            final = await get_deliverable(db, rows[1].deliverable_id)
            assert packet.format == "markdown"
            assert "daily_brief_contract" not in (packet.render_metadata or {})
            assert final.format == "rich"
            assert final.render_metadata["daily_brief_contract"]["version"] == 13
            selected = await engine._resolve_final_deliverable(task, task.workflow_state, workflow)
            assert selected.deliverable_id == final.deliverable_id
            data = await engine._build_result_data(task, task.workflow_state, workflow)
            assert data["final_deliverable_id"] == final.deliverable_id
            assert data["final_format"] == "rich"
            expected_content = engine._truncate_channel_fallback_field(
                engine._channel_safe_deliverable_content(selected.content, str(selected.format)),
                max_length=2000,
            )
            assert data["final_content"] == expected_content
            assert data["final_content_summary"] == expected_content
            if workflow.steps[-1].type == "complete":
                terminal = task.workflow_state.step_outputs[workflow.steps[-1].name]
                assert terminal["content"] == ""
                assert terminal["outputs"] == workflow.steps[-1].complete.outputs
                assert rows[-1].deliverable_id is None
        for ctx, expected in zip(contexts, [False, True], strict=True):
            rehydrated = Workflow.model_validate_json(workflow.model_dump_json())
            cold = replace(
                ctx,
                workflow_steps=rehydrated.steps,
                step_definition=rehydrated.steps[ctx.step_index],
            )
            loop._clear_cached_deliverable(cold)
            assert loop._native_validation_context(cold, []).daily_brief_task_output is expected
            loaded = await loop._get_current_deliverable(cold)
            assert loaded is not None
            assert (
                await loop._finalization_instruction(
                    replace(cold, todos=[{"content": "Done", "status": "completed"}])
                )
            )["required_action"] == "step_complete"
    finally:
        await database.dispose()


@pytest.mark.parametrize("action", ["cancel", "fail", "gate", "revise:a", "skip"])
def test_redirecting_outcome_routes_remain_unknown(action):
    from cognis.models.workflow import OutcomeRoute, StepDefinition

    steps = [StepDefinition(name="a", type="run"), StepDefinition(name="b", type="run")]
    steps[0].outcome_routes = [OutcomeRoute(status="rejected", action=action)]
    assert step_is_task_output(steps, steps[0], 0) is None
    assert step_is_task_output(steps, steps[1], 1) is None


def test_continue_does_not_exempt_single_final_or_conditional_graph():
    from cognis.models.workflow import OutcomeRoute, StepDefinition

    step = StepDefinition(
        name="publish",
        type="run",
        outcome_routes=[OutcomeRoute(status="rejected", action="continue")],
    )
    assert step_is_task_output([step], step, 0) is True
    optional = StepDefinition(name="optional", type="run")
    optional.when = "metadata.ready"
    assert step_is_task_output([step, optional], step, 0) is None

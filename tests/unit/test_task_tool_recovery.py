from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from cognis.core.agent_loop import (
    AgentLoop,
    _append_interrupted_tool_results,
    _append_tool_call_event,
)
from cognis.core.harness_guards import SameTurnToolCallLedger
from cognis.core.runtime import TransientExecutorUnavailable
from cognis.core.task_execution import TaskExecutionFence
from cognis.core.task_tool_recovery import acknowledge_task_receipts, recover_task_tools
from cognis.models.tool import ToolCall, ToolResult
from cognis.tools.builtin.orchestration import DELEGATE_TOOL
from cognis.tools.executor.definitions import BASH_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _recovery():
    session_id, turn_id, call_id = (uuid4().hex for _ in range(3))
    descriptor = {
        "call_id": call_id,
        "tool_name": "bash",
        "dispatch_state": "bound",
        "executor_id": uuid4().hex,
        "executor_instance_id": uuid4().hex,
    }
    batches = {
        uuid4().hex: {
            "session_id": session_id,
            "turn_id": turn_id,
            "tool_calls": [descriptor],
        }
    }
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=BASH_TOOL))
    registry.register(RegisteredTool(definition=DELEGATE_TOOL))
    ctx = SimpleNamespace(
        turn_id=turn_id,
        session=SimpleNamespace(session_id=session_id, intaris_session_id=session_id),
        tool_registry=registry,
        same_turn_tool_call_ledger=SameTurnToolCallLedger(),
        tool_call_ledger_candidates={},
        execution_fence=SimpleNamespace(
            assert_current=AsyncMock(),
            complete_tool_dispatch=AsyncMock(return_value=[]),
        ),
    )
    loop = SimpleNamespace(
        providers=SimpleNamespace(guardrails=object()),
        session_cache=SimpleNamespace(append_recorded_events=AsyncMock()),
        _reconcile_accepted_tool_call=AsyncMock(
            return_value=(
                "terminal",
                ToolResult(output="real outcome", is_error=False),
                "recovered",
            )
        ),
        _tool_safe_for_same_executor_retry=Mock(return_value=False),
    )
    events = []
    _append_tool_call_event(
        events, ToolCall(call_id=call_id, name="bash", arguments={"command": "printf 'once'"})
    )
    events[0].data["turn_id"] = turn_id
    pairs = {
        call_id: (
            {
                "type": "tool_call",
                "data": events[0].data,
            },
            None,
        )
    }
    return loop, ctx, batches, pairs


@pytest.mark.asyncio
async def test_reserved_delegate_rejoins_without_executor_dispatch_identity(monkeypatch):
    loop, ctx, batches, pairs = _recovery()
    call_id = next(iter(pairs))
    descriptor = next(iter(batches.values()))["tool_calls"][0]
    descriptor.update(
        tool_name="delegate",
        child_session_id=uuid4().hex,
        child_turn_id=uuid4().hex,
    )
    descriptor.pop("executor_instance_id")
    pairs[call_id][0]["data"]["name"] = "delegate"
    loop._handle_orchestration_tool = AsyncMock(return_value=ToolResult(output="child result"))
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    append = AsyncMock(
        return_value=SimpleNamespace(
            event={"type": "tool_result", "data": {}}, append_result=object()
        )
    )
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)
    await recover_task_tools(loop, ctx, batches)
    loop._handle_orchestration_tool.assert_awaited_once()
    loop._reconcile_accepted_tool_call.assert_not_awaited()
    assert append.await_args.kwargs["event"].data["output"] == "child result"


@pytest.mark.asyncio
async def test_legacy_delegate_without_child_identity_pauses_without_spawn(monkeypatch):
    from cognis.core.task_recovery_errors import TaskRecoveryBlocked

    loop, ctx, batches, pairs = _recovery()
    descriptor = next(iter(batches.values()))["tool_calls"][0]
    descriptor["tool_name"] = "delegate"
    loop._handle_orchestration_tool = AsyncMock()
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    with pytest.raises(TaskRecoveryBlocked, match="Legacy delegation"):
        await recover_task_tools(loop, ctx, batches)
    loop._handle_orchestration_tool.assert_not_awaited()
    ctx.execution_fence.complete_tool_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_replay_uses_normal_authorized_execution(monkeypatch):
    loop, ctx, batches, pairs = _recovery()
    loop._reconcile_accepted_tool_call.return_value = ("unresolved", None, "instance_replaced")
    loop._tool_safe_for_same_executor_retry.return_value = True
    loop.execute_controller_tool = AsyncMock(return_value=ToolResult(output="authorized read"))
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    append = AsyncMock(
        return_value=SimpleNamespace(
            event={"type": "tool_result", "data": {}}, append_result=object()
        )
    )
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)
    await recover_task_tools(loop, ctx, batches)
    loop.execute_controller_tool.assert_awaited_once()
    assert append.await_args.kwargs["event"].data["output"] == "authorized read"


@pytest.mark.asyncio
async def test_descendant_batches_do_not_run_under_parent_authority(monkeypatch):
    loop, ctx, batches, pairs = _recovery()
    next(iter(batches.values()))["session_id"] = uuid4().hex
    read = AsyncMock(return_value=pairs)
    monkeypatch.setattr("cognis.core.task_tool_recovery.read_canonical_tool_pairs", read)
    await recover_task_tools(loop, ctx, batches)
    read.assert_not_awaited()
    ctx.execution_fence.complete_tool_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_real_run_steps_settle_controller_calls_before_new_session(tmp_path, monkeypatch):
    import asyncio

    from cognis.core.task_execution import (
        TaskExecutionStore,
        bind_task_execution_fence,
        reset_task_execution_fence,
    )
    from cognis.models.session import ConversationModel, SessionModel
    from cognis.models.task import TaskStatus
    from cognis.models.workflow import StepDefinition, Workflow
    from cognis.store.database import create_engine, create_session_factory
    from cognis.store.models import Task as TaskRow
    from cognis.store.queries import create_conversation, create_session
    from cognis.tools.registry import ToolRegistry
    from tests.unit.test_agent_loop import (
        _step_complete_test_loop,
        _StepCompleteRetryLLM,
    )
    from tests.unit.test_workflow_deterministic_execution import (
        _engine_runtime,
        _mock_tool_runtime,
    )

    llm = _StepCompleteRetryLLM(
        first_arguments={"summary": "Completed first"},
        second_arguments={"summary": "Completed second"},
    )
    db_engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/controller-settlement.db")
    factory = create_session_factory(db_engine)
    loop = _step_complete_test_loop(llm, session_factory=factory)
    db_engine, factory, engine, task, _ = await _engine_runtime(
        tmp_path, agent_loop=loop, database_engine=db_engine
    )
    _mock_tool_runtime(engine, monkeypatch, registry=ToolRegistry())
    steps = [
        StepDefinition(
            name=name,
            type="run",
            prompt="Complete the step.",
            require_deliverable=False,
            input={"type": "null"},
        )
        for name in ("first", "second")
    ]
    sessions = []
    for _ in steps:
        conversation = ConversationModel(
            conversation_id=uuid4().hex,
            user_email="user@example.com",
            agent_id="agent-1",
            context={"type": "web"},
        )
        session = SessionModel(
            session_id=uuid4().hex,
            intaris_session_id=uuid4().hex,
            conversation_id=conversation.conversation_id,
            user_email=conversation.user_email,
            agent_id=conversation.agent_id,
        )
        async with factory() as db:
            await create_conversation(
                db,
                conversation.user_email,
                conversation.agent_id,
                "web",
                conversation_id=conversation.conversation_id,
            )
            await create_session(
                db,
                conversation.conversation_id,
                session.user_email,
                session.agent_id,
                session_id=session.session_id,
                intaris_session_id=session.intaris_session_id,
            )
            await db.commit()
        sessions.append((conversation, session, False))
    engine._create_step_session = AsyncMock(side_effect=[entry[:2] for entry in sessions])
    store = TaskExecutionStore(
        factory, owner_id=uuid4().hex, max_active_global=1, max_active_per_agent=1
    )
    claim = await store.claim_existing(task.task_id, statuses={"running"})
    assert claim is not None
    fence = TaskExecutionFence(store, claim, asyncio.Event())
    token = bind_task_execution_fence(fence)
    try:
        result = await engine.execute_workflow(
            task,
            Workflow(
                workflow_id="wf-controller-settlement",
                name="Controller settlement",
                defaults={"evaluate": False},
                steps=steps,
            ),
        )
        assert result.status == TaskStatus.COMPLETED, result.workflow_state.step_outputs
        assert set(result.workflow_state.step_outputs) == {"first", "second"}
        assert len(llm.calls) == 2
        assert sessions[0][1].intaris_session_id != sessions[1][1].intaris_session_id
        async with factory() as db:
            row = await db.get(TaskRow, task.task_id)
            assert not row.result_data.get("execution_recovery")
    finally:
        reset_task_execution_fence(token)
        await fence.close()
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_task_suspension_does_not_fabricate_an_executor_outcome() -> None:
    loop, ctx, _, pairs = _recovery()
    del loop
    ctx.execution_fence = object.__new__(TaskExecutionFence)
    ctx.pending_tool_calls = {next(iter(pairs)): object()}
    events = []
    assert _append_interrupted_tool_results(ctx, events) == 0
    assert not events
    assert ctx.pending_tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["[", "[]", '{"_truncated":true}'])
async def test_malformed_canonical_arguments_never_become_empty_arguments(monkeypatch, arguments):
    loop, ctx, batches, pairs = _recovery()
    next(iter(pairs.values()))[0]["data"]["arguments"] = arguments
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    with pytest.raises(RuntimeError, match="Canonical tool arguments"):
        await recover_task_tools(loop, ctx, batches)
    loop._reconcile_accepted_tool_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_deterministic_recovery_returns_real_result_without_early_completion(monkeypatch):
    loop, ctx, batches, pairs = _recovery()
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    append = AsyncMock()
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)
    recovered = await recover_task_tools(
        loop, ctx, batches, collect_results=True, complete_descriptors=False, record_results=False
    )
    assert recovered.results[next(iter(pairs))].output == "real outcome"
    assert loop._reconcile_accepted_tool_call.await_args.kwargs["tc"].arguments == {
        "command": "printf 'once'"
    }
    append.assert_not_awaited()
    ctx.execution_fence.complete_tool_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_or_hook_context_tracks_and_acknowledges_its_receipts():
    loop, ctx, _, pairs = _recovery()
    ctx.execution_fence = None
    ctx.executor_receipt_bindings = {}
    call_id, executor_id, instance = next(iter(pairs)), uuid4().hex, uuid4().hex
    ctx.turn_id = uuid4().hex
    await AgentLoop._bind_tool_dispatch_fence(
        ctx,
        call_id=call_id,
        executor_id=executor_id,
        executor_instance_id=instance,
        dispatch_state="sent",
    )
    connection = SimpleNamespace(
        capabilities=SimpleNamespace(durable_tool_receipts=True),
        rpc_call=AsyncMock(return_value={"acknowledged": True}),
    )
    loop._resolve_target_connection = Mock(return_value=connection)
    await acknowledge_task_receipts(loop, list(ctx.executor_receipt_bindings.values()))
    connection.rpc_call.assert_awaited_once_with(
        "tool.result_ack",
        {
            "call_id": call_id,
            "scope": {
                "session_id": ctx.session.intaris_session_id,
                "turn_id": ctx.turn_id,
            },
        },
        timeout=5.0,
    )


@pytest.mark.asyncio
async def test_takeover_settles_real_result_without_executing_tool(monkeypatch) -> None:
    loop, ctx, batches, pairs = _recovery()
    read = AsyncMock(return_value=pairs)
    append = AsyncMock(
        return_value=SimpleNamespace(
            event={"type": "tool_result", "data": {"output": "real outcome"}},
            append_result=object(),
        )
    )
    monkeypatch.setattr("cognis.core.task_tool_recovery.read_canonical_tool_pairs", read)
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)
    await recover_task_tools(loop, ctx, batches)
    loop._reconcile_accepted_tool_call.assert_awaited_once()
    assert loop._reconcile_accepted_tool_call.await_args.kwargs["single_attempt"]
    assert append.await_args.kwargs["event"].data["output"] == "real outcome"
    ctx.execution_fence.complete_tool_dispatch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["active", "fetch_failed", "reconnect_timeout"])
async def test_unavailable_evidence_suspends_without_result_or_replay(monkeypatch, reason) -> None:
    loop, ctx, batches, pairs = _recovery()
    loop._reconcile_accepted_tool_call.return_value = ("unresolved", None, reason)
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    append = AsyncMock()
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)
    with pytest.raises(TransientExecutorUnavailable):
        await recover_task_tools(loop, ctx, batches)
    append.assert_not_awaited()
    ctx.execution_fence.complete_tool_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_intaris_is_retryable_infrastructure_not_terminal_failure(
    monkeypatch,
) -> None:
    loop, ctx, batches, _ = _recovery()
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs",
        AsyncMock(side_effect=ConnectionError("unavailable")),
    )
    with pytest.raises(TransientExecutorUnavailable):
        await recover_task_tools(loop, ctx, batches)
    loop._reconcile_accepted_tool_call.assert_not_awaited()
    ctx.execution_fence.complete_tool_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_deterministic_uncertain_mutation_stays_fenced_after_process_replacement(
    monkeypatch,
) -> None:
    loop, ctx, batches, pairs = _recovery()
    loop._reconcile_accepted_tool_call.return_value = ("unresolved", None, "indeterminate_state")
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    append = AsyncMock()
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)
    with pytest.raises(RuntimeError, match="it was not replayed"):
        await recover_task_tools(loop, ctx, batches, collect_results=True)
    append.assert_not_awaited()
    ctx.execution_fence.complete_tool_dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["report_instance_mismatch", "indeterminate_state", "missing_dispatch_identity"],
)
async def test_agent_gets_unknown_outcome_and_reconciliation_guidance_without_replay(
    monkeypatch, reason
) -> None:
    loop, ctx, batches, pairs = _recovery()
    call_id = next(iter(pairs))
    loop._reconcile_accepted_tool_call.return_value = ("unresolved", None, reason)
    loop.execute_controller_tool = AsyncMock(side_effect=AssertionError("Unsafe replay"))
    if reason == "missing_dispatch_identity":
        next(iter(batches.values()))["tool_calls"][0].pop("executor_instance_id")
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    append = AsyncMock(
        side_effect=lambda *args, event, **kwargs: SimpleNamespace(
            event=event.model_dump(), append_result=object()
        )
    )
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)

    recovered = await recover_task_tools(loop, ctx, batches)

    result = recovered.results[call_id]
    assert result is not None and result.is_error
    output = json.loads(result.output)
    assert output["status"] == "outcome_unknown"
    assert output["safe_to_retry"] is False
    assert output["ambiguity"]["reconcile_outcome"] == reason
    assert "read-only tools" in output["reconciliation"]
    assert "verified remaining change" in output["reconciliation"]
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("bash", {"command": "printf 'once'"})
    assert not ctx.same_turn_tool_call_ledger.already_executed("bash", {"command": "printf 'once'"})
    event = append.await_args.kwargs["event"]
    assert event.data["uncertain"] is True
    assert event.data["ambiguity"]["reconcile_outcome"] == reason
    ctx.execution_fence.complete_tool_dispatch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("collect_results", [False, True])
async def test_proven_not_sent_without_instance_recovers_without_receipt_or_replay(
    monkeypatch, collect_results
) -> None:
    loop, ctx, batches, pairs = _recovery()
    call_id = next(iter(pairs))
    descriptor = next(iter(batches.values()))["tool_calls"][0]
    descriptor["dispatch_state"] = "not_sent"
    descriptor.pop("executor_instance_id")
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs",
        AsyncMock(return_value=pairs),
    )
    append = AsyncMock(
        return_value=SimpleNamespace(
            event={"type": "tool_result", "data": {"is_error": True}},
            append_result=object(),
        )
    )
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)

    recovered = await recover_task_tools(loop, ctx, batches, collect_results=collect_results)

    loop._reconcile_accepted_tool_call.assert_not_awaited()
    loop._tool_safe_for_same_executor_retry.assert_not_called()
    if collect_results:
        assert recovered.results[call_id] is None
        append.assert_not_awaited()
        ctx.execution_fence.complete_tool_dispatch.assert_not_awaited()
    else:
        assert append.await_args.kwargs["event"].data["is_error"] is True
        assert append.await_args.kwargs["event"].data["metadata"]["recovery"] == "not_sent"
        ctx.execution_fence.complete_tool_dispatch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_state", ["pending", "dispatching", "sent"])
async def test_missing_identity_without_non_delivery_evidence_stays_ambiguous(
    monkeypatch, dispatch_state
) -> None:
    loop, ctx, batches, pairs = _recovery()
    descriptor = next(iter(batches.values()))["tool_calls"][0]
    descriptor["dispatch_state"] = dispatch_state
    descriptor.pop("executor_instance_id")
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs",
        AsyncMock(return_value=pairs),
    )
    append = AsyncMock()
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)

    with pytest.raises(RuntimeError, match="missing_dispatch_identity"):
        await recover_task_tools(loop, ctx, batches, collect_results=True)

    append.assert_not_awaited()
    ctx.execution_fence.complete_tool_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_settled_result_is_not_duplicated(monkeypatch) -> None:
    loop, ctx, batches, pairs = _recovery()
    call_id = next(iter(pairs))
    pairs[call_id] = pairs[call_id][0], {"type": "tool_result", "data": {"output": "settled"}}
    monkeypatch.setattr(
        "cognis.core.task_tool_recovery.read_canonical_tool_pairs", AsyncMock(return_value=pairs)
    )
    append = AsyncMock()
    monkeypatch.setattr("cognis.core.task_tool_recovery.append_tool_result_once", append)
    await recover_task_tools(loop, ctx, batches)
    loop._reconcile_accepted_tool_call.assert_not_awaited()
    append.assert_not_awaited()
    ctx.execution_fence.complete_tool_dispatch.assert_awaited_once()

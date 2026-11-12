"""Cold continuation recovery through production delegation admission."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from cognis.core.agent_loop import AgentLoop, SessionLock
from cognis.core.agent_registry import AgentRegistry
from cognis.core.events import EventBus
from cognis.core.followups import (
    LLM_CYCLE_CEILING_CONTINUATION_REASON,
    TOOL_CALL_CEILING_CONTINUATION_REASON,
)
from cognis.core.runtime import ResolvedStepRuntime, TransientExecutorUnavailable
from cognis.core.session import SessionManager, _to_conversation_model, _to_session_model
from cognis.core.task_execution import bind_task_execution_fence, reset_task_execution_fence
from cognis.models.session import SessionEvent
from cognis.models.tool import ToolCall, ToolDefinition, ToolSource
from cognis.models.workflow import StepOutput
from cognis.store.models import Conversation, Session
from cognis.tools.builtin.orchestration import OrchestrationMode
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.integration.test_task_delegation_recovery import (
    _claim,
    _expire,
    _Ledger,
    _seed,
)
from tests.integration.test_task_delegation_recovery import (
    database as _database_fixture,
)
from tests.unit.test_agent_loop import _NoopSessionCache, _parallel_group_ctx
from tests.unit.test_session_manager import _Cache, _Providers

pytestmark = pytest.mark.integration
database = _database_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("rotate_initial", [False, True])
@pytest.mark.parametrize(
    "reason", [TOOL_CALL_CEILING_CONTINUATION_REASON, LLM_CYCLE_CEILING_CONTINUATION_REASON]
)
async def test_production_delegate_restores_interrupted_continuation(
    database, reason, rotate_initial
):
    task_id, parent_id, call_id, turn_id, conversation_id = await _seed(database)
    providers = _Providers()
    ledger = _Ledger(parent_id, parent_id)
    ledger.interrupt.clear()
    providers.guardrails.record_events = ledger.record_events
    providers.guardrails.read_events = ledger.read_events
    providers.guardrails.create_session = AsyncMock(wraps=providers.guardrails.create_session)
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="read",
                description="Read fixture",
                parameters={},
                source=ToolSource(
                    type="intaris_mcp",
                    server_id="read-fixture",
                    server_name="fixture",
                    raw_tool_name="read",
                ),
            )
        )
    )
    arguments = {
        "agent_id": "system:explore",
        "task": "Complete delegated recovery",
        "context": "Keep the original runtime",
        "expected_output": "Complete result",
    }
    await ledger.record_events(
        parent_id,
        [
            SessionEvent(
                type="tool_call",
                data={
                    "call_id": call_id,
                    "name": "delegate",
                    "arguments": arguments,
                    "turn_id": turn_id,
                },
            )
        ],
    )
    frame_turns = []
    follow_up_attempts = []
    read_effects = 0
    read_call_id = uuid4().hex
    child_id = None
    unavailable_once = True

    async def runtime_factory(**kwargs):
        nonlocal unavailable_once
        assert kwargs["task_id"] == task_id
        assert kwargs["execution_fence"].claim.task_id == task_id
        assert kwargs["cancel_event"] is not None
        if unavailable_once:
            unavailable_once = False
            raise TransientExecutorUnavailable("Executor reconnecting", executor_id="test-executor")
        return ResolvedStepRuntime(
            tool_registry=registry,
            executor_connection=None,
            cleanup=AsyncMock(),
            executor_environment=None,
        )

    for owner in ("controller-one", "controller-two"):
        if owner != "controller-one":
            await _expire(database)
        fence = await _claim(database, task_id, owner)
        if owner == "controller-one":
            await fence.record_tool_dispatch(
                session_id=parent_id,
                turn_id=turn_id,
                descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
            )
        manager = SessionManager(database, providers, _Cache())
        loop = AgentLoop(
            providers=providers,
            session_manager=manager,
            session_cache=_NoopSessionCache(),
            context_assembler=SimpleNamespace(),
            compaction_strategy=None,
            tool_router=SimpleNamespace(artifact_store=None),
            remember_queue=None,
            event_bus=EventBus(),
            session_lock=SessionLock(),
            pause_waiter=None,
            session_factory=database,
            step_runtime_factory=runtime_factory,
        )
        loop._load_session_todos = AsyncMock()
        loop._refresh_orchestration_target_snapshot = AsyncMock()
        loop._recover_pending_managed_join_handoffs = AsyncMock()

        async def model(ctx, *, fence=fence, manager=manager, **kwargs):
            nonlocal read_effects, child_id
            if ctx.turn_id is None:
                ctx.turn_id = f"turn_{uuid4().hex[:12]}"
            child_id = ctx.session.session_id
            frame_turns.append(ctx.turn_id)
            follow_up_attempts.append(ctx.follow_up.attempt if ctx.follow_up else None)
            if ctx.follow_up is None:
                assert len(frame_turns) == 1, "The initial child frame must not replay"
                if rotate_initial:
                    ctx.session = await manager.rotate_session(
                        conversation_id=conversation_id,
                        current_session=ctx.session,
                        intention="Continue the same delegated work",
                    )
                    child_id = ctx.session.session_id
                    assert ctx.session.delegation_metadata["task_execution_turn_id"] == ctx.turn_id
                    assert ctx.session.delegation_metadata["task_runtime_fingerprint"]
                return StepOutput(
                    summary="Reached ceiling",
                    metadata={
                        "interrupted": True,
                        "continuation_reason": reason,
                        "pending_todos": [{"content": "Intaris-owned todo", "status": "pending"}],
                    },
                )
            assert ctx.follow_up.reason == reason and ctx.follow_up.attempt in {1, 2}
            if read_effects == 0:
                await fence.record_tool_dispatch(
                    session_id=child_id,
                    turn_id=ctx.turn_id,
                    descriptors=[
                        {"call_id": read_call_id, "tool_name": "read", "dispatch_state": "not_sent"}
                    ],
                )
                await ledger.record_events(
                    child_id,
                    [
                        SessionEvent(
                            type="tool_call",
                            data={
                                "call_id": read_call_id,
                                "name": "read",
                                "arguments": {},
                                "turn_id": ctx.turn_id,
                            },
                        )
                    ],
                )
                read_effects += 1
                ledger.interrupt.add(child_id)
                await ledger.record_events(
                    child_id,
                    [
                        SessionEvent(
                            type="tool_result",
                            data={
                                "call_id": read_call_id,
                                "name": "read",
                                "output": "Accepted read",
                                "turn_id": ctx.turn_id,
                            },
                        )
                    ],
                    idempotency_key=f"{child_id}:turn:{ctx.turn_id}:tool-result:{read_call_id}",
                )
            assert not any(
                batch["session_id"] == child_id
                for batch in (await fence.recovery_batches()).values()
            )
            if len(frame_turns) == 3:
                return StepOutput(
                    summary="Reached ceiling again",
                    metadata={"interrupted": True, "continuation_reason": reason},
                )
            assert ctx.follow_up.attempt == 2
            return StepOutput(summary="Completed child", content="Complete result")

        loop._execute_step = model
        async with database() as db:
            parent = _to_session_model(await db.get(Session, parent_id))
            conversation = _to_conversation_model(await db.get(Conversation, conversation_id))
        ctx = _parallel_group_ctx()
        ctx.session, ctx.conversation = parent, conversation
        ctx.agent = await AgentRegistry(database).get("primary", owner_email=parent.user_email)
        ctx.turn_id, ctx.task_id, ctx.execution_fence = turn_id, task_id, fence
        ctx.orchestration_mode = OrchestrationMode.TASK_PRIMARY
        token = bind_task_execution_fence(fence)
        try:
            call = ToolCall(call_id=call_id, name="delegate", arguments=arguments)
            if owner == "controller-one":
                with pytest.raises(TransientExecutorUnavailable):
                    await loop._handle_delegate(call, ctx=ctx, events_to_record=[])
                async with database() as db:
                    children = (
                        (
                            await db.execute(
                                select(Session).where(Session.parent_session_id == parent_id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert len(children) == 1
                    assert children[0].status == "active"
                with pytest.raises(asyncio.CancelledError):
                    await loop._handle_delegate(call, ctx=ctx, events_to_record=[])
            else:
                result = await loop._handle_delegate(call, ctx=ctx, events_to_record=[])
                assert not result.is_error, result.output
                assert "Complete result" in result.output
                await ledger.record_events(
                    parent_id,
                    [
                        SessionEvent(
                            type="tool_result",
                            data={
                                **result.model_dump(mode="json"),
                                "call_id": call_id,
                                "name": "delegate",
                                "turn_id": turn_id,
                            },
                        )
                    ],
                    idempotency_key=f"{parent_id}:turn:{turn_id}:tool-result:{call_id}",
                )
                await fence.complete_tool_dispatch(
                    session_id=parent_id,
                    turn_id=turn_id,
                    call_ids=[call_id],
                )
                assert await fence.recovery_batches() == {}
        finally:
            reset_task_execution_fence(token)

    assert len(frame_turns) == 4
    assert frame_turns[0] != frame_turns[1] == frame_turns[2]
    assert frame_turns[3] != frame_turns[2]
    assert follow_up_attempts == [None, 1, 1, 2]
    assert read_effects == 1
    assert providers.guardrails.create_session.await_count == (2 if rotate_initial else 1)
    async with database() as db:
        children = list(
            (
                await db.execute(select(Session).where(Session.parent_session_id == parent_id))
            ).scalars()
        )
        assert len(children) == (2 if rotate_initial else 1)
        assert len([child for child in children if child.previous_session_id is None]) == 1
        assert all(child.status == "completed" for child in children)
        assert (
            len({child.delegation_metadata["task_runtime_fingerprint"] for child in children}) == 1
        )
        metadata = next(
            child for child in children if child.session_id == child_id
        ).delegation_metadata
        assert metadata["task_execution_turn_id"] == frame_turns[-1]
        assert metadata["task_continuation"]["attempt"] == 2
        assert "pending_todos" not in metadata["task_continuation"]
        assert "Intaris-owned todo" not in json.dumps(metadata)
    for session_id in (parent_id, child_id):
        assert sum(event["type"] == "tool_result" for event in ledger.events[session_id]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_result_recorded", "cancelled_child", "questions_allowed"),
    [
        (False, True, False),
        (True, True, False),
        (False, False, False),
        (False, True, True),
        (True, True, True),
        (False, False, True),
    ],
)
async def test_interrupted_child_settles_receipts_before_delegation_continuation(
    database,
    parent_result_recorded,
    cancelled_child,
    questions_allowed,
):
    from cognis.core.session import _to_conversation_model, _to_session_model
    from cognis.core.task_execution import delegation_arguments_binding
    from cognis.core.task_tool_recovery import recover_task_tools
    from cognis.models.tool import ToolResult
    from cognis.store.models import Conversation, StepRun
    from cognis.tools.builtin.orchestration import DELEGATE_TOOL

    ids = await _seed(database)
    task_id, parent_id, call_id, turn_id, conversation_id = ids
    async with database() as db:
        db.add(
            StepRun(
                step_run_id=turn_id,
                task_id=task_id,
                step_name="execute",
                step_type="run",
                agent_id="primary",
                conversation_id=conversation_id,
                session_id=parent_id,
                intaris_session_id=parent_id,
                attempt_number=1,
                status="running",
            )
        )
        await db.commit()
    fence = await _claim(database, task_id, "controller-one")
    arguments = {
        "agent_id": "system:explore",
        "task": "Inspect",
        "context": "",
        "expected_output": "Findings",
    }
    await fence.record_tool_dispatch(
        session_id=parent_id,
        turn_id=turn_id,
        descriptors=[{"call_id": call_id, "tool_name": "delegate"}],
    )
    child_id, _ = await fence.reserve_delegation(
        session_id=parent_id,
        turn_id=turn_id,
        call_id=call_id,
        binding=delegation_arguments_binding(arguments),
        step_run_id=turn_id,
    )
    ledger = _Ledger(child_id, parent_id)
    ledger.interrupt.clear()
    providers = _Providers()
    providers.guardrails.record_events = ledger.record_events
    providers.guardrails.read_events = ledger.read_events
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=DELEGATE_TOOL))
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="read",
                description="Read",
                parameters={},
                source=ToolSource(
                    type="intaris_mcp",
                    server_id="fixture",
                    server_name="fixture",
                    raw_tool_name="read",
                ),
            )
        )
    )
    runtime = ResolvedStepRuntime(
        tool_registry=registry,
        executor_connection=None,
        cleanup=AsyncMock(),
        executor_environment=None,
    )
    manager = SessionManager(database, providers, _Cache())
    loop = AgentLoop(
        providers=providers,
        session_manager=manager,
        session_cache=_NoopSessionCache(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=None,
        tool_router=SimpleNamespace(artifact_store=None),
        remember_queue=None,
        event_bus=EventBus(),
        session_lock=SessionLock(),
        pause_waiter=None,
        session_factory=database,
        step_runtime_factory=AsyncMock(return_value=runtime),
    )
    loop._refresh_orchestration_target_snapshot = AsyncMock()
    loop._load_session_todos = AsyncMock()
    loop._recover_pending_managed_join_handoffs = AsyncMock()
    loop._refresh_delegation_target_snapshot = AsyncMock()
    loop._get_session_store = lambda *a: SimpleNamespace(get_event=AsyncMock(return_value=None))
    loop._finalize_tool_result = AsyncMock(side_effect=lambda ctx, tc, registered, result: result)
    loop._retire_task_tool_receipts = AsyncMock()
    read_id = uuid4().hex

    async def interrupted_model(child_ctx, **kwargs):
        await fence.record_tool_dispatch(
            session_id=child_id,
            turn_id=child_ctx.turn_id,
            descriptors=[{"call_id": read_id, "tool_name": "read", "dispatch_state": "sent"}],
        )
        await fence.bind_tool_dispatch(
            read_id,
            "fixture-executor",
            "fixture-instance",
            session_id=child_id,
            turn_id=child_ctx.turn_id,
            dispatch_state="sent",
        )
        ledger.events[child_id] = [
            {
                "seq": 1,
                "type": "tool_call",
                "data": {
                    "call_id": read_id,
                    "name": "read",
                    "turn_id": child_ctx.turn_id,
                    "arguments": {},
                },
            }
        ]
        raise asyncio.CancelledError

    loop._execute_step = interrupted_model
    loop._reconcile_accepted_tool_call = AsyncMock(
        return_value=("terminal", ToolResult(output="Recovered receipt"), "receipt"),
    )
    ctx = _parallel_group_ctx()
    async with database() as db:
        ctx.session = _to_session_model(await db.get(Session, parent_id))
        ctx.conversation = _to_conversation_model(await db.get(Conversation, conversation_id))
    ctx.turn_id, ctx.step_run_id, ctx.task_id = turn_id, turn_id, task_id
    ctx.execution_fence, ctx.cancel_event, ctx.tool_registry = fence, asyncio.Event(), registry
    if questions_allowed:
        ctx.interaction_mode = "step_requests"
        ctx.step_definition.allow_questions = True
    token = bind_task_execution_fence(fence)
    try:
        with pytest.raises(asyncio.CancelledError):
            await loop._handle_delegate(
                ToolCall(call_id=call_id, name="delegate", arguments=arguments),
                ctx=ctx,
                events_to_record=[],
            )
        if cancelled_child:
            await manager.mark_cancelled(child_id, result_summary="Cancelled by parent session")
        if not cancelled_child:
            ledger.events[parent_id] = [
                {
                    "seq": 1,
                    "type": "tool_call",
                    "data": {
                        "call_id": call_id,
                        "name": "delegate",
                        "arguments": arguments,
                        "turn_id": turn_id,
                    },
                }
            ]
            strict = await recover_task_tools(
                loop, ctx, await fence.recovery_batches(), settlement_only=True
            )
            assert strict.requires_delegation_continuation
            pending = await fence.recovery_batches()
            assert any(
                item["call_id"] == call_id
                for batch in pending.values()
                for item in batch["tool_calls"]
            )
            assert all(batch["session_id"] != child_id for batch in pending.values())

            async def continue_model(child_ctx, **kwargs):
                assert child_ctx.session.session_id == child_id
                assert any(
                    event["type"] == "tool_result"
                    and "Recovered receipt" in event["data"]["output"]
                    for event in ledger.events[child_id]
                )
                return StepOutput(
                    summary="Child completed from its recovered receipt.",
                    content="Child completed from its recovered receipt.",
                    metadata={"completed_via_tool": True},
                )

            loop._execute_step = continue_model
            recovered = await recover_task_tools(
                loop, ctx, await fence.recovery_batches(), collect_results=True
            )
            result = recovered.results[call_id]
        elif parent_result_recorded:
            ledger.events[parent_id] = [
                {
                    "seq": 1,
                    "type": "tool_call",
                    "data": {
                        "call_id": call_id,
                        "name": "delegate",
                        "arguments": arguments,
                        "turn_id": turn_id,
                    },
                },
                {
                    "seq": 2,
                    "type": "tool_result",
                    "data": {
                        **ToolResult(
                            call_id=call_id,
                            name="delegate",
                            output="Delegation cancelled.",
                            is_error=True,
                        ).model_dump(),
                        "turn_id": turn_id,
                    },
                },
            ]
            recovered = await recover_task_tools(
                loop,
                ctx,
                await fence.recovery_batches(),
                collect_results=True,
            )
            result = recovered.results[call_id]
        else:
            result = await loop._handle_delegate(
                ToolCall(call_id=call_id, name="delegate", arguments=arguments),
                ctx=ctx,
                events_to_record=[],
            )
    finally:
        reset_task_execution_fence(token)
    assert result is not None
    if cancelled_child:
        assert result.is_error and "cancelled" in result.output.lower()
    else:
        assert (
            not result.is_error and "Child completed from its recovered receipt." in result.output
        )
    results = [event["data"] for event in ledger.events[child_id] if event["type"] == "tool_result"]
    assert len(results) == 1 and "Recovered receipt" in results[0]["output"]
    assert all(
        batch["session_id"] != child_id for batch in (await fence.recovery_batches()).values()
    )
    if parent_result_recorded or not cancelled_child:
        assert await fence.recovery_batches() == {}
    async with database() as db:
        assert (await db.get(Session, child_id)).status == (
            "cancelled" if cancelled_child else "completed"
        )

"""Capacity rejection must not consume a second logical admission."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.admitted_dispatch import dispatch_admitted_tool
from cognis.core.agent_loop import AgentLoop
from cognis.core.tool_router import ToolRouter
from cognis.models.tool import EvaluationResult, Permission, ToolCall, ToolResult
from cognis.providers.executor.delivery import ExecutorDeliveryError
from cognis.providers.guardrails.conflicts import EvaluationConflict
from tests.unit.test_tool_router import (
    BASH_TOOL,
    RegisteredTool,
    ToolRegistry,
    _agent,
    _session,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failures,state", [(1, "not_sent"), (9, "not_sent"), (1, "accepted_unknown")]
)
async def test_admitted_capacity_dispatch(failures, state):
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=BASH_TOOL))
    guardrails = AsyncMock()
    guardrails.evaluate.side_effect = [
        EvaluationResult(decision="approve", call_id="admitted-capacity"),
        EvaluationConflict("duplicate_admission", "Logical call already admitted"),
        EvaluationConflict("duplicate_admission", "Logical call already admitted"),
    ]
    attempts = []
    executions = []

    async def dispatch(call, **kwargs):
        attempts.append(call)
        if len(attempts) <= failures:
            if state == "accepted_unknown":
                executions.append(call)
            raise ExecutorDeliveryError(
                "Bridge call limit exceeded", state, code="executor_bridge_capacity"
            )
        executions.append(call)
        return ToolResult(output="completed")

    executor = AsyncMock()
    executor.executor_id = "sample-desktop"
    executor.tool_execute.side_effect = dispatch
    router = ToolRouter(guardrails)
    call = ToolCall(
        call_id="capacity-dispatch",
        name="bash",
        arguments={"command": "printf completed"},
    )
    result = await router.execute(
        call, _session(), _agent({"*": Permission.EVALUATE}), registry, executor
    )
    # Reproduce the old outer retry: it re-enters admission after not_sent.
    if state == "not_sent" and AgentLoop._is_same_executor_transient_failure(None, result):
        result = await router.execute(
            call, _session(), _agent({"*": Permission.EVALUATE}), registry, executor
        )
    guardrails.evaluate.assert_awaited_once()
    assert len(executions) <= 1
    assert all(attempt.call_id == call.call_id for attempt in attempts)
    assert all(attempt.arguments == call.arguments for attempt in attempts)
    if state == "accepted_unknown":
        assert len(attempts) == 1
        assert result.is_error
        assert result.metadata["delivery_state"] == state
    elif failures == 1:
        assert not result.is_error
        assert len(attempts) == 2
        assert len(executions) == 1
    else:
        assert result.is_error
        assert len(attempts) == 3
        assert not executions
        assert result.metadata["code"] == "executor_bridge_capacity"
        assert not AgentLoop._is_same_executor_transient_failure(None, result)
        assert "capacity" in result.output.lower()
        assert "disconnect" not in result.output.lower()


@pytest.mark.asyncio
async def test_capacity_retry_preserves_callbacks_and_stops_at_lost_fence():
    before_send = AsyncMock(side_effect=[None, RuntimeError("ownership lost")])
    on_sent = AsyncMock()

    async def dispatch(call, **kwargs):
        assert kwargs["before_send"] is before_send
        assert kwargs["on_sent"] is on_sent
        await kwargs["before_send"]("sample-desktop", None)
        raise ExecutorDeliveryError("capacity", "not_sent", code="executor_bridge_capacity")

    executor = SimpleNamespace(tool_execute=AsyncMock(side_effect=dispatch))
    with pytest.raises(RuntimeError, match="ownership lost"):
        await dispatch_admitted_tool(
            executor,
            ToolCall(call_id="fenced-capacity", name="bash", arguments={}),
            before_send=before_send,
            on_sent=on_sent,
        )
    assert executor.tool_execute.await_count == 2
    assert before_send.await_count == 2
    on_sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_during_backoff_never_resends(monkeypatch):
    from cognis.core import admitted_dispatch

    entered_backoff = asyncio.Event()

    async def backoff(delay):
        entered_backoff.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(admitted_dispatch, "asyncio", SimpleNamespace(sleep=backoff))
    executor = SimpleNamespace(
        tool_execute=AsyncMock(
            side_effect=ExecutorDeliveryError(
                "capacity", "not_sent", code="executor_bridge_capacity"
            )
        )
    )
    task = asyncio.create_task(
        dispatch_admitted_tool(
            executor, ToolCall(call_id="cancelled-capacity", name="bash", arguments={})
        )
    )
    await entered_backoff.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    executor.tool_execute.assert_awaited_once()

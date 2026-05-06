"""Stage 36: target_executor parameter stripping and routing semantics."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.core.executor_pool import (
    ExecutorAvailability,
    ExecutorPool,
    ResolvedExecutorTarget,
)


def _target(executor_id: str, *, usable: bool = True) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type="websocket",
        is_primary=True,
        selection_source="explicit",
        description=None,
        state=ExecutorAvailability.USABLE if usable else ExecutorAvailability.OFFLINE,
        observed_tools=[{"name": "bash"}],
    )


@pytest.fixture
def loop_with_pool():
    """Build a thin AgentLoop-shaped object exercising _execute_regular_tool."""
    from cognis.core.agent_loop import AgentLoop

    # We build a minimal stub — only the methods/attributes
    # _execute_regular_tool touches.
    instance = AgentLoop.__new__(AgentLoop)
    instance.tool_router = MagicMock()
    instance.tool_router.execute = AsyncMock()
    instance.providers = MagicMock()
    return instance


def _ctx(pool: ExecutorPool, *, active_executor_id: str | None = None) -> Any:
    """Build a minimal StepContext-like object."""

    ctx = MagicMock()
    ctx.executor_pool = pool
    ctx.executor_connection = MagicMock(name="active_connection")
    ctx.active_executor_id = active_executor_id or (pool.primary[0].executor_id if pool.primary else None)
    ctx.session = MagicMock()
    ctx.agent = MagicMock()
    ctx.tool_registry = MagicMock()
    return ctx


def _toolcall(name: str, arguments: dict[str, Any]) -> Any:
    """Build a minimal ToolCall-like object."""
    from cognis.models.tool import ToolCall

    return ToolCall(call_id="call-1", name=name, arguments=arguments)


@pytest.mark.asyncio
async def test_target_executor_stripped_before_dispatch(loop_with_pool, monkeypatch) -> None:
    """target_executor must be stripped from arguments before reaching the router."""
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-other")])
    ctx = _ctx(pool, active_executor_id="exec-active")

    # Make get_connection return a different connection for the target
    other_conn = MagicMock(name="other_connection")
    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(return_value=other_conn)
    loop_with_pool.providers.executor.websocket = ws_provider

    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(output="ok", is_error=False)
    )

    # Stub _get_tool_registry / _tool_runtime_metadata via monkeypatch on instance
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection

    tc = _toolcall("bash", {"command": "ls", "target_executor": "exec-other"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)
    assert result.is_error is False

    # The router must have been called with arguments missing target_executor
    call = loop_with_pool.tool_router.execute.await_args
    routed_tc = call.args[0]
    assert "target_executor" not in routed_tc.arguments
    assert routed_tc.arguments == {"command": "ls"}
    # Connection used should be the other one (not the active)
    assert call.args[4] is other_conn


@pytest.mark.asyncio
async def test_target_executor_unassigned_returns_factual_error(loop_with_pool) -> None:
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection

    tc = _toolcall("bash", {"command": "ls", "target_executor": "exec-ghost"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)
    assert result.is_error is True
    assert "exec-ghost" in result.output
    assert "not assigned" in result.output.lower()
    # Router must NOT have been called for an unassigned target
    loop_with_pool.tool_router.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_executor_offline_returns_factual_error(loop_with_pool) -> None:
    pool = ExecutorPool(
        primary=[_target("exec-active"), _target("exec-down", usable=False)]
    )
    ctx = _ctx(pool, active_executor_id="exec-active")

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection

    tc = _toolcall("bash", {"command": "ls", "target_executor": "exec-down"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)
    assert result.is_error is True
    assert "offline" in result.output.lower() or "not usable" in result.output.lower()
    loop_with_pool.tool_router.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_executor_same_as_active_uses_active_connection(loop_with_pool) -> None:
    """When target_executor == active, reuse the existing active connection."""
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-other")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(output="ok", is_error=False)
    )

    tc = _toolcall("bash", {"command": "ls", "target_executor": "exec-active"})
    await loop_with_pool._execute_regular_tool(ctx, tc)
    call = loop_with_pool.tool_router.execute.await_args
    # Should use the active connection
    assert call.args[4] is ctx.executor_connection


@pytest.mark.asyncio
async def test_no_target_executor_uses_active_connection(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(output="ok", is_error=False)
    )

    tc = _toolcall("bash", {"command": "ls"})
    await loop_with_pool._execute_regular_tool(ctx, tc)
    call = loop_with_pool.tool_router.execute.await_args
    assert call.args[4] is ctx.executor_connection
    # Arguments unchanged
    assert call.args[0].arguments == {"command": "ls"}

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
from cognis.providers.executor.delivery import AmbiguousToolOutcome


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


def _read_target(executor_id: str, *, usable: bool = True) -> ResolvedExecutorTarget:
    target = _target(executor_id, usable=usable)
    return ResolvedExecutorTarget(
        executor_id=target.executor_id,
        executor_type=target.executor_type,
        is_primary=target.is_primary,
        selection_source=target.selection_source,
        description=target.description,
        state=target.state,
        observed_tools=[{"name": "read"}],
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
    from cognis.core.harness_guards import SameTurnToolCallLedger
    from cognis.models.tool import NativeToolDefinition as ToolDefinition
    from cognis.models.tool import ToolSource
    from cognis.tools.registry import RegisteredTool, ToolRegistry

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="bash",
                description="Run a shell command",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
                source=ToolSource(type="executor"),
                category="shell",
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="read",
                description="Read a file",
                parameters={
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                },
                source=ToolSource(type="executor"),
                category="filesystem",
                read_only=True,
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="web_fetch",
                description="Fetch a URL",
                parameters={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                },
                source=ToolSource(type="executor"),
                category="web",
                read_only=True,
            )
        )
    )
    ctx = MagicMock()
    ctx.executor_pool = pool
    ctx.executor_connection = MagicMock(name="active_connection")
    ctx.active_executor_id = active_executor_id or (
        pool.primary[0].executor_id if pool.primary else None
    )
    ctx.session = MagicMock()
    ctx.agent = MagicMock()
    ctx.tool_registry = registry
    ctx.same_turn_tool_call_ledger = SameTurnToolCallLedger()
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
    assert routed_tc.runtime_metadata == {
        "tool_call_id": "call-1",
        "tool_name": "bash",
    }
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
    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-down", usable=False)])
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
async def test_target_executor_rejected_on_non_executor_tool(loop_with_pool) -> None:
    """Defensive: target_executor on a builtin/memory tool returns a factual error."""
    from cognis.models.tool import NativeToolDefinition as ToolDefinition
    from cognis.models.tool import ToolSource
    from cognis.tools.registry import RegisteredTool, ToolRegistry

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="recall_memories",
                description="Recall memories",
                parameters={"type": "object"},
                source=ToolSource(type="builtin"),  # not executor
                category="memory",
            )
        )
    )
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = MagicMock()
    ctx.executor_pool = pool
    ctx.executor_connection = MagicMock()
    ctx.active_executor_id = "exec-active"
    ctx.tool_registry = registry

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection

    tc = _toolcall("recall_memories", {"query": "x", "target_executor": "exec-active"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)
    assert result.is_error is True
    assert "executor-routed" in result.output.lower()
    loop_with_pool.tool_router.execute.assert_not_awaited()


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


def test_install_active_executor_target_rebinds_same_turn_runtime(loop_with_pool) -> None:
    """switch_executor must affect later no-target tool calls in the same turn."""

    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-other")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ctx.conversation.active_executor_id = "exec-active"
    old_conn = ctx.executor_connection
    other_conn = MagicMock(name="other_connection")

    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(return_value=other_conn)
    ws_provider.get_handle_metadata = MagicMock(
        return_value={
            "environment": {
                "user": "fpytloun",
                "home": "/Users/fpytloun",
                "cwd": "/Users/fpytloun",
                "hostname": "olorin",
            },
            "platform": {"os": "darwin", "arch": "arm64", "python": "3.12"},
        }
    )
    loop_with_pool.providers.executor.websocket = ws_provider

    switched = loop_with_pool._install_active_executor_target(
        ctx,
        pool.by_id("exec-other"),
    )

    assert switched is True
    assert ctx.active_executor_id == "exec-other"
    assert ctx.conversation.active_executor_id == "exec-other"
    assert ctx.executor_connection is other_conn
    assert ctx.executor_connection is not old_conn
    assert ctx.executor_environment.executor_id == "exec-other"
    assert ctx.executor_environment.home == "/Users/fpytloun"


@pytest.mark.asyncio
async def test_no_target_after_switch_uses_rebound_connection(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-other")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    other_conn = MagicMock(name="other_connection")

    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(return_value=other_conn)
    ws_provider.get_handle_metadata = MagicMock(return_value={"environment": {}})
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._install_active_executor_target(ctx, pool.by_id("exec-other"))

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(output="ok", is_error=False)
    )

    tc = _toolcall("bash", {"command": "pwd"})
    await loop_with_pool._execute_regular_tool(ctx, tc)

    call = loop_with_pool.tool_router.execute.await_args
    assert call.args[4] is other_conn
    assert call.args[0].arguments == {"command": "pwd"}


@pytest.mark.asyncio
async def test_active_executor_connection_is_refreshed_before_same_turn_tool(
    loop_with_pool,
) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    stale_conn = ctx.executor_connection
    fresh_conn = MagicMock(name="fresh_connection")

    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(return_value=fresh_conn)
    loop_with_pool.providers.executor.websocket = ws_provider

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(output="ok", is_error=False)
    )

    tc = _toolcall("bash", {"command": "pwd"})
    await loop_with_pool._execute_regular_tool(ctx, tc)

    call = loop_with_pool.tool_router.execute.await_args
    assert call.args[4] is fresh_conn
    assert ctx.executor_connection is fresh_conn
    assert ctx.executor_connection is not stale_conn


@pytest.mark.asyncio
async def test_read_only_tool_retries_after_same_executor_reconnect(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    stale_conn = ctx.executor_connection
    fresh_conn = MagicMock(name="fresh_connection")

    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(side_effect=[None])
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    ws_provider.invalidate_forwarded_connection = AsyncMock()
    loop_with_pool.providers.executor.websocket = ws_provider

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        side_effect=[
            ToolResult(
                output="disconnected",
                is_error=True,
                metadata={
                    "code": "executor_disconnected",
                    "delivery_state": "accepted_unknown",
                    "executor_id": "exec-active",
                    "epoch": 3,
                    "retryable": True,
                    "same_executor_only": True,
                },
            ),
            ToolResult(output="ok", is_error=False),
        ]
    )

    tc = _toolcall("read", {"file_path": "/tmp/example"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)

    assert result.is_error is False
    assert result.output == "ok"
    assert result.metadata["auto_retried"] is True
    assert result.metadata["same_executor_reconnected"] is True
    assert ctx.executor_connection is fresh_conn
    assert ctx.executor_connection is not stale_conn
    assert loop_with_pool.tool_router.execute.await_count == 2
    ws_provider.invalidate_forwarded_connection.assert_awaited_once_with("exec-active", stale_conn)
    assert ws_provider.wait_for_connection.await_args.kwargs["failed_connection"] is stale_conn
    assert ws_provider.wait_for_connection.await_args.kwargs["delivery_state"] == "accepted_unknown"
    assert ws_provider.wait_for_connection.await_args.kwargs["failed_epoch"] == 3
    assert ws_provider.wait_for_connection.await_args.kwargs["timeout"] >= 60.0


@pytest.mark.asyncio
async def test_mutating_tool_is_not_automatically_retried_after_reconnect(
    loop_with_pool,
) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")

    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(return_value=fresh_conn)
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    loop_with_pool.providers.executor.websocket = ws_provider

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(
            output="disconnected",
            is_error=True,
            metadata={
                "code": "executor_disconnected",
                "delivery_state": "accepted_unknown",
                "executor_id": "exec-active",
                "retryable": True,
                "same_executor_only": True,
            },
        )
    )

    tc = _toolcall("bash", {"command": "mv a b"})
    with pytest.raises(AmbiguousToolOutcome) as exc:
        await loop_with_pool._execute_regular_tool(ctx, tc)

    assert exc.value.tool_name == "bash"
    assert exc.value.argument_fingerprint
    assert ctx.same_turn_tool_call_ledger.already_executed(tc.name, tc.arguments)
    loop_with_pool.tool_router.execute.assert_awaited_once()
    ws_provider.wait_for_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguity_uses_registered_canonical_name_for_alias(loop_with_pool) -> None:
    from cognis.core.agent_loop import _same_turn_duplicate_tool_call_indexes
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    registered = ctx.tool_registry.get("bash")

    class _AliasRegistry:
        def get(self, name: str) -> Any:
            return registered if name in {"bash", "mcp_server__bash"} else None

    ctx.tool_registry = _AliasRegistry()
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(
            output="outcome unknown",
            is_error=True,
            metadata={
                "code": "executor_disconnected",
                "delivery_state": "accepted_unknown",
                "executor_id": "exec-active",
                "same_executor_only": True,
            },
        )
    )
    dispatched_arguments = {"command": "touch /tmp/alias-side-effect"}
    arguments = {**dispatched_arguments, "target_executor": "exec-active"}

    with pytest.raises(AmbiguousToolOutcome) as exc:
        await loop_with_pool._execute_regular_tool(
            ctx,
            _toolcall("mcp_server__bash", arguments),
        )

    assert exc.value.tool_name == "bash"
    assert ctx.same_turn_tool_call_ledger.already_executed("bash", dispatched_arguments)
    assert not ctx.same_turn_tool_call_ledger.already_executed("mcp_server__bash", arguments)
    assert _same_turn_duplicate_tool_call_indexes(
        ctx.same_turn_tool_call_ledger,
        [
            _toolcall("mcp_server__bash", arguments),
            _toolcall("bash", dispatched_arguments),
        ],
        ctx.tool_registry,
    ) == {0, 1}


@pytest.mark.asyncio
async def test_pre_send_write_retries_once_on_same_executor(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")
    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    ws_provider.invalidate_forwarded_connection = AsyncMock()
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        side_effect=[
            ToolResult(
                output="not sent",
                is_error=True,
                metadata={
                    "code": "executor_delivery_failure",
                    "delivery_state": "not_sent",
                    "executor_id": "exec-active",
                    "same_executor_only": True,
                },
            ),
            ToolResult(output="ok", is_error=False),
        ]
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("bash", {"command": "touch /tmp/x"})
    )

    assert result.is_error is False
    assert loop_with_pool.tool_router.execute.await_count == 2


@pytest.mark.asyncio
async def test_web_fetch_retries_after_same_executor_reconnect(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")

    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(side_effect=[None])
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    loop_with_pool.providers.executor.websocket = ws_provider

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        side_effect=[
            ToolResult(
                output="disconnected",
                is_error=True,
                metadata={
                    "code": "executor_disconnected",
                    "executor_id": "exec-active",
                    "retryable": True,
                    "same_executor_only": True,
                },
            ),
            ToolResult(output="ok", is_error=False),
        ]
    )

    tc = _toolcall("web_fetch", {"url": "https://example.com"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)

    assert result.is_error is False
    assert result.output == "ok"
    assert result.metadata["auto_retried"] is True
    assert loop_with_pool.tool_router.execute.await_count == 2


@pytest.mark.asyncio
async def test_transient_failure_wait_timeout_has_clear_metadata(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")

    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(side_effect=[None])
    ws_provider.wait_for_connection = AsyncMock(return_value=None)
    loop_with_pool.providers.executor.websocket = ws_provider

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(
            output="disconnected",
            is_error=True,
            metadata={
                "code": "executor_disconnected",
                "executor_id": "exec-active",
                "retryable": True,
                "same_executor_only": True,
            },
        )
    )

    tc = _toolcall("read", {"file_path": "/tmp/example"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)

    assert result.is_error is True
    assert result.metadata["auto_retried"] is False
    assert result.metadata["auto_retry_skipped_reason"] == "same_executor_reconnect_timeout"
    assert result.metadata["same_executor_reconnected"] is False
    assert "did not reconnect" in result.output
    loop_with_pool.tool_router.execute.assert_awaited_once()
    ws_provider.wait_for_connection.assert_awaited_once()


@pytest.mark.asyncio
async def test_target_executor_retry_waits_same_target_not_primary(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("primary"), _read_target("secondary")])
    ctx = _ctx(pool, active_executor_id="primary")
    primary_conn = ctx.executor_connection
    secondary_conn = MagicMock(name="secondary_connection")
    secondary_reconnected = MagicMock(name="secondary_reconnected")

    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(return_value=secondary_conn)
    ws_provider.wait_for_connection = AsyncMock(return_value=secondary_reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider

    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        side_effect=[
            ToolResult(
                output="disconnected",
                is_error=True,
                metadata={
                    "code": "executor_disconnected",
                    "executor_id": "secondary",
                    "retryable": True,
                    "same_executor_only": True,
                },
            ),
            ToolResult(output="ok", is_error=False),
        ]
    )

    tc = _toolcall("read", {"file_path": "/tmp/example", "target_executor": "secondary"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)

    assert result.is_error is False
    assert result.metadata["auto_retried"] is True
    ws_provider.wait_for_connection.assert_awaited_once()
    assert ws_provider.wait_for_connection.await_args.args == ("secondary",)
    assert ctx.active_executor_id == "primary"
    assert ctx.executor_connection is primary_conn
    second_call = loop_with_pool.tool_router.execute.await_args_list[1]
    assert second_call.args[4] is secondary_reconnected


@pytest.mark.asyncio
async def test_parallel_write_ambiguity_cancels_and_ledgers_in_flight_sibling(
    loop_with_pool,
) -> None:
    import asyncio

    from cognis.core.agent_loop import _PreparedRegularToolCall
    from cognis.core.harness_guards import tool_call_argument_fingerprint
    from cognis.models.tool import NativeToolDefinition, ToolCall, ToolSource
    from cognis.tools.registry import RegisteredTool, ToolRegistry

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    registry = ToolRegistry()
    for name in ("write_one", "write_two"):
        registry.register(
            RegisteredTool(
                definition=NativeToolDefinition(
                    name=name,
                    description=name,
                    parameters={"type": "object", "properties": {}},
                    source=ToolSource(type="executor"),
                )
            )
        )
    ctx.tool_registry = registry
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    sibling_side_effect_completed = False
    first = ToolCall(call_id="call-write-1", name="write_one", arguments={"value": 1})
    sibling = ToolCall(call_id="call-write-2", name="write_two", arguments={"value": 2})

    async def execute(_ctx: Any, tool_call: ToolCall) -> Any:
        nonlocal sibling_side_effect_completed
        if tool_call.name == "write_one":
            await sibling_started.wait()
            fingerprint = tool_call_argument_fingerprint(tool_call.name, tool_call.arguments)
            ctx.same_turn_tool_call_ledger.record_fingerprint(tool_call.name, fingerprint)
            raise AmbiguousToolOutcome(
                tool_name=tool_call.name,
                argument_fingerprint=fingerprint,
                executor_id="exec-active",
                generation=1,
                epoch=7,
            )
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        sibling_side_effect_completed = True

    loop_with_pool._execute_regular_tool = execute

    with pytest.raises(AmbiguousToolOutcome) as exc:
        await loop_with_pool._execute_parallel_regular_tool_group(
            ctx,
            [
                _PreparedRegularToolCall(first, "tool-write-1"),
                _PreparedRegularToolCall(sibling, "tool-write-2"),
            ],
        )

    assert sibling_cancelled.is_set()
    assert sibling_side_effect_completed is False
    assert ctx.same_turn_tool_call_ledger.already_executed("write_one", first.arguments)
    assert ctx.same_turn_tool_call_ledger.already_executed("write_two", sibling.arguments)
    assert exc.value.detail()["uncertain_tool_calls"] == [
        {
            "tool_name": "write_two",
            "argument_fingerprint": tool_call_argument_fingerprint("write_two", sibling.arguments),
        }
    ]


@pytest.mark.asyncio
async def test_parallel_ambiguity_ledgers_completed_unfinalized_write_sibling(
    loop_with_pool,
) -> None:
    import asyncio

    from cognis.core.agent_loop import _PreparedRegularToolCall
    from cognis.core.harness_guards import tool_call_argument_fingerprint
    from cognis.models.tool import (
        NativeToolDefinition,
        ToolCall,
        ToolResult,
        ToolSource,
    )
    from cognis.tools.registry import RegisteredTool, ToolRegistry

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    registry = ToolRegistry()
    for name in ("write_one", "write_two"):
        registry.register(
            RegisteredTool(
                definition=NativeToolDefinition(
                    name=name,
                    description=name,
                    parameters={"type": "object", "properties": {}},
                    source=ToolSource(type="executor"),
                )
            )
        )
    ctx.tool_registry = registry
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    sibling_completed = asyncio.Event()
    first = ToolCall(call_id="call-write-1", name="write_one", arguments={"value": 1})
    sibling = ToolCall(call_id="call-write-2", name="write_two", arguments={"value": 2})

    async def execute(_ctx: Any, tool_call: ToolCall) -> ToolResult:
        if tool_call.name == "write_two":
            sibling_completed.set()
            return ToolResult(output="side effect completed", is_error=False)
        await sibling_completed.wait()
        fingerprint = tool_call_argument_fingerprint(tool_call.name, tool_call.arguments)
        ctx.same_turn_tool_call_ledger.record_fingerprint(tool_call.name, fingerprint)
        raise AmbiguousToolOutcome(
            tool_name=tool_call.name,
            argument_fingerprint=fingerprint,
            executor_id="exec-active",
            generation=1,
            epoch=7,
        )

    loop_with_pool._execute_regular_tool = execute

    with pytest.raises(AmbiguousToolOutcome) as exc:
        await loop_with_pool._execute_parallel_regular_tool_group(
            ctx,
            [
                _PreparedRegularToolCall(first, "tool-write-1"),
                _PreparedRegularToolCall(sibling, "tool-write-2"),
            ],
        )

    assert ctx.same_turn_tool_call_ledger.already_executed("write_two", sibling.arguments)
    assert exc.value.detail()["uncertain_tool_calls"] == [
        {
            "tool_name": "write_two",
            "argument_fingerprint": tool_call_argument_fingerprint("write_two", sibling.arguments),
        }
    ]

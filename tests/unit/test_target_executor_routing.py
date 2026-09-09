"""Stage 36: target_executor parameter stripping and routing semantics."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
    instance.session_manager = SimpleNamespace(session_factory=None)
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
    ctx.cancel_event = None
    ctx.execution_fence = None
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
    ws_provider.get_ready_connection = MagicMock(return_value=other_conn)
    ws_provider.get_handle_metadata = MagicMock(
        return_value={
            "environment": {
                "user": "fpytloun",
                "home": "/Users/fpytloun",
                "cwd": "/Users/fpytloun/src/project",
                "hostname": "olorin",
            }
        }
    )
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
    assert routed_tc.runtime_metadata["tool_call_id"] == "call-1"
    assert routed_tc.runtime_metadata["tool_name"] == "bash"
    assert routed_tc.runtime_metadata["working_directory"] == "/Users/fpytloun/src/project"
    assert routed_tc.runtime_metadata["executor_environment"]["executor_id"] == "exec-other"
    assert routed_tc.runtime_metadata["executor_environment"]["cwd"] == (
        "/Users/fpytloun/src/project"
    )
    assert "workspace_root" not in routed_tc.runtime_metadata
    # Connection used should be the other one (not the active)
    assert call.args[4] is other_conn


@pytest.mark.asyncio
async def test_target_executor_without_environment_omits_active_paths(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-other")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ctx.workspace_root = "/active/project"
    ctx.working_directory = "/active/project"
    other_conn = MagicMock(name="other_connection")
    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=other_conn)
    ws_provider.get_handle_metadata = MagicMock(return_value={})
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(output="ok", is_error=False)
    )
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry

    await loop_with_pool._execute_regular_tool(
        ctx,
        _toolcall("bash", {"command": "pwd", "target_executor": "exec-other"}),
    )

    routed_tc = loop_with_pool.tool_router.execute.await_args.args[0]
    assert "workspace_root" not in routed_tc.runtime_metadata
    assert "working_directory" not in routed_tc.runtime_metadata
    assert routed_tc.runtime_metadata["executor_environment"]["available"] is False
    assert routed_tc.runtime_metadata["executor_environment"]["executor_id"] == "exec-other"
    assert ctx.workspace_root == "/active/project"
    assert ctx.working_directory == "/active/project"


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
async def test_target_executor_refreshes_policy_with_target_paths_before_evaluation(
    loop_with_pool,
) -> None:
    """A per-call target must add its paths before guardrails evaluate the tool."""

    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-other")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ctx.session_policy = {"allow_policies": ["shell"]}
    other_conn = MagicMock(name="other_connection")
    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=other_conn)
    ws_provider.get_handle_metadata = MagicMock(
        return_value={
            "environment": {
                "home": "/Users/fpytloun",
                "cwd": "/Users/fpytloun/src/cognis",
                "tmpdir": "/var/folders/cognis/tmp",
                "hostname": "olorin",
            }
        }
    )
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.session_manager.refresh_intaris_session_policy = AsyncMock()
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection

    async def _execute(*args: Any, **kwargs: Any) -> ToolResult:
        del args, kwargs
        loop_with_pool.session_manager.refresh_intaris_session_policy.assert_awaited_once()
        return ToolResult(output="ok", is_error=False)

    loop_with_pool.tool_router.execute = AsyncMock(side_effect=_execute)

    tc = _toolcall(
        "bash",
        {
            "command": "git worktree add --detach /Users/fpytloun/src/worktrees/task HEAD",
            "workdir": "/Users/fpytloun/src/cognis",
            "target_executor": "exec-other",
        },
    )
    result = await loop_with_pool._execute_regular_tool(ctx, tc)

    assert result.is_error is False, result.output
    refresh = loop_with_pool.session_manager.refresh_intaris_session_policy.await_args
    assert refresh.args == (ctx.session,)
    assert refresh.kwargs == {
        "session_policy_override": {"allow_policies": ["shell"]},
        "additional_allowed_paths": [
            "/Users/fpytloun",
            "/Users/fpytloun/src/cognis",
        ],
    }
    assert loop_with_pool.tool_router.execute.await_args.args[4] is other_conn


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
    ws_provider.get_ready_connection = MagicMock(return_value=other_conn)
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
    assert ctx.workspace_root == "/Users/fpytloun"
    assert ctx.working_directory == "/Users/fpytloun"


def test_install_active_executor_target_keeps_current_target_when_replacement_is_pending(
    loop_with_pool,
) -> None:
    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-other")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ctx.conversation.active_executor_id = "exec-active"
    old_conn = ctx.executor_connection

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=None)
    loop_with_pool.providers.executor.websocket = ws_provider

    switched = loop_with_pool._install_active_executor_target(ctx, pool.by_id("exec-other"))

    assert switched is False
    assert ctx.active_executor_id == "exec-active"
    assert ctx.conversation.active_executor_id == "exec-active"
    assert ctx.executor_connection is old_conn


@pytest.mark.asyncio
async def test_no_target_after_switch_uses_rebound_connection(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active"), _target("exec-other")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    other_conn = MagicMock(name="other_connection")

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=other_conn)
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
    ws_provider.get_ready_connection = MagicMock(return_value=fresh_conn)
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
    ws_provider.get_ready_connection = MagicMock(side_effect=[None])
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
    # accepted_unknown is reconciled first; only the read-only fallback replay
    # is represented as not_sent after reconciliation could not identify the
    # accepting process in this legacy fixture.
    assert ws_provider.wait_for_connection.await_args.kwargs["delivery_state"] == "not_sent"
    assert (
        ws_provider.wait_for_connection.await_args.kwargs["accepted_unknown_replay_safe"] is False
    )
    assert ws_provider.wait_for_connection.await_args.kwargs["failed_epoch"] == 3
    assert 899.0 < ws_provider.wait_for_connection.await_args.kwargs["timeout"] <= 900.0


@pytest.mark.asyncio
async def test_mutating_tool_is_not_automatically_retried_after_reconnect(
    loop_with_pool,
) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=fresh_conn)
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
    result = await loop_with_pool._execute_regular_tool(ctx, tc)

    assert result.is_error is True
    assert result.metadata["code"] == "tool_outcome_ambiguous"
    assert result.metadata["retryable"] is False
    assert result.metadata["uncertain"] is True
    assert result.metadata["ambiguity"]["tool_name"] == "bash"
    assert result.metadata["ambiguity"]["generation"] is None
    payload = json.loads(result.output)
    assert payload["status"] == "outcome_unknown"
    assert payload["call_id"] == tc.call_id
    assert payload["executor_id"] == "exec-active"
    assert payload["safe_to_retry"] is False
    assert "external system" in payload["reconciliation"]
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome(tc.name, tc.arguments)
    assert not ctx.same_turn_tool_call_ledger.already_executed(tc.name, tc.arguments)
    loop_with_pool.tool_router.execute.assert_awaited_once()
    ws_provider.wait_for_connection.assert_not_awaited()


class _ReconnectedConnection:
    """Post-reconnect connection that can report a prior call's real outcome."""

    def __init__(self, *, instance_id: str, reports: list[dict[str, Any]]) -> None:
        self.executor_instance_id = instance_id
        self._reports = reports
        self.fetched: list[str] = []

    async def fetch_tool_result(self, call_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.fetched.append(call_id)
        report = self._reports[min(len(self.fetched) - 1, len(self._reports) - 1)]
        return {"executor_instance_id": self.executor_instance_id, **report}


def _accepted_unknown_result(instance_id: str) -> Any:
    from cognis.models.tool import ToolResult

    return ToolResult(
        output="Executor delivery failed; recovery is restricted to the same executor.",
        is_error=True,
        metadata={
            "code": "executor_disconnected",
            "delivery_state": "accepted_unknown",
            "executor_id": "exec-active",
            "retryable": True,
            "same_executor_only": True,
            "transport": {
                "route": "physical_websocket",
                "delivery_state": "accepted_unknown",
                "executor_instance_id": instance_id,
            },
        },
    )


def _prepare_reconcile_loop(loop_with_pool, ctx, *, stale_instance: str) -> Any:
    ctx.executor_connection = MagicMock(name="stale_connection")
    ctx.executor_connection.executor_instance_id = stale_instance
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection

    # Recovered results must cross the normal post-execution/trust boundary.
    async def _finalize(*, result, tool_call, registered_tool, session, agent):
        del tool_call, registered_tool, session, agent
        return result

    loop_with_pool.tool_router.finalize_recovered_executor_result = AsyncMock(side_effect=_finalize)
    return ctx.executor_connection


@pytest.mark.asyncio
async def test_reconnect_recovers_real_bash_outcome_instead_of_ambiguity(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: a dropped socket must not make a finished command unknown."""

    from cognis.core.executor_recovery import ExecutorRecoveryWindow

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[
            {
                "state": "terminal",
                "result": {
                    "call_id": "call-1",
                    "output": "build succeeded",
                    "is_error": False,
                    "duration_ms": 4200,
                },
            }
        ],
    )

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider
    vars(loop_with_pool.providers)["_session_factory"] = object()
    now = datetime.now(UTC)
    monkeypatch.setattr(
        "cognis.core.executor_recovery.begin_executor_recovery",
        AsyncMock(
            return_value=ExecutorRecoveryWindow(
                executor_id="exec-active",
                unavailable_since=now,
                deadline=now + timedelta(seconds=900),
                database_remaining_seconds=900,
            )
        ),
    )
    clear_recovery = AsyncMock()
    monkeypatch.setattr(
        "cognis.core.executor_recovery.clear_executor_recovery",
        clear_recovery,
    )
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=_accepted_unknown_result("instance-a")
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("bash", {"command": "make build"})
    )

    assert result.is_error is False
    assert result.output == "build succeeded"
    assert result.metadata["recovered_after_reconnect"] is True
    assert "ambiguity" not in (result.metadata or {})
    # The command is never re-executed.
    loop_with_pool.tool_router.execute.assert_awaited_once()
    assert reconnected.fetched == ["call-1"]
    # The recovered output went through the normal sanitization pipeline.
    loop_with_pool.tool_router.finalize_recovered_executor_result.assert_awaited_once()
    clear_recovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_reconciliation_returns_terminal_result_after_reconnect_deadline(
    loop_with_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.core.executor_recovery import ExecutorRecoveryWindow
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    vars(loop_with_pool.providers)["_session_factory"] = object()
    now = datetime.now(UTC)
    window = ExecutorRecoveryWindow(
        executor_id="exec-active",
        unavailable_since=now,
        deadline=now + timedelta(seconds=900),
        database_remaining_seconds=900,
    )
    authoritative = ToolResult(output="mutation completed", is_error=False)

    async def reconcile_after_deadline(*args: Any, **kwargs: Any) -> tuple[str, Any, str]:
        del args, kwargs
        await asyncio.sleep(0.02)
        return ("terminal", authoritative, "terminal")

    loop_with_pool._reconcile_accepted_tool_call = AsyncMock(side_effect=reconcile_after_deadline)
    clear_recovery = AsyncMock()
    monkeypatch.setattr(
        "cognis.core.executor_recovery.clear_executor_recovery",
        clear_recovery,
    )

    result = await loop_with_pool._handle_tool_after_same_executor_transient_failure(
        ctx,
        tc=_toolcall("read", {"file_path": "/tmp/example"}),
        registered=ctx.tool_registry.get("read"),
        target_executor_id=None,
        failed_connection=ctx.executor_connection,
        output_chunk_callback=None,
        original_result=_accepted_unknown_result("instance-a"),
        recovery_deadline=asyncio.get_running_loop().time() + 0.005,
        recovery_window=window,
    )

    assert result is authoritative
    clear_recovery.assert_awaited_once()
    loop_with_pool.tool_router.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_result_cannot_forge_controller_owned_evaluation(
    loop_with_pool,
) -> None:
    """An executor must not be able to supply its own guardrails verdict."""

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[
            {
                "state": "terminal",
                "result": {
                    "call_id": "call-1",
                    "output": "done",
                    "is_error": False,
                    "metadata": {"evaluation": {"decision": "allow", "forged": True}},
                },
            }
        ],
    )

    original = _accepted_unknown_result("instance-a")
    original.metadata["evaluation"] = {"decision": "allow", "source": "controller"}

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(return_value=original)

    result = await loop_with_pool._execute_regular_tool(ctx, _toolcall("bash", {"command": "make"}))

    assert result.is_error is False
    assert result.metadata["evaluation"] == {"decision": "allow", "source": "controller"}


@pytest.mark.asyncio
async def test_recovered_result_drops_executor_supplied_evaluation_when_absent(
    loop_with_pool,
) -> None:
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[
            {
                "state": "terminal",
                "result": {
                    "call_id": "call-1",
                    "output": "done",
                    "is_error": False,
                    "metadata": {"evaluation": {"decision": "allow", "forged": True}},
                },
            }
        ],
    )

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=_accepted_unknown_result("instance-a")
    )

    result = await loop_with_pool._execute_regular_tool(ctx, _toolcall("bash", {"command": "make"}))

    assert "evaluation" not in (result.metadata or {})


@pytest.mark.asyncio
async def test_reconnect_waits_for_active_call_then_returns_its_result(
    loop_with_pool,
) -> None:
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[
            {"state": "active"},
            {
                "state": "terminal",
                "result": {"call_id": "call-1", "output": "done", "is_error": False},
            },
        ],
    )

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=_accepted_unknown_result("instance-a")
    )

    import cognis.core.agent_loop as agent_loop_module

    original_poll = agent_loop_module._TOOL_RECONCILE_POLL_SECONDS
    agent_loop_module._TOOL_RECONCILE_POLL_SECONDS = 0.0
    try:
        result = await loop_with_pool._execute_regular_tool(
            ctx, _toolcall("bash", {"command": "sleep 5 && touch x"})
        )
    finally:
        agent_loop_module._TOOL_RECONCILE_POLL_SECONDS = original_poll

    assert result.is_error is False
    assert result.output == "done"
    assert len(reconnected.fetched) == 2
    loop_with_pool.tool_router.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_forwarded_proxy_is_refreshed_instead_of_giving_up(
    loop_with_pool,
) -> None:
    """HA: a proxy on a superseded owner epoch must not abandon a recoverable call."""

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")

    class _StaleProxy:
        owner_id = "controller-b:boot-old"
        executor_instance_id = None

        async def fetch_tool_result(self, call_id: str, **_kwargs: Any) -> dict[str, Any]:
            del call_id
            raise ConnectionError("bridge bound to superseded owner epoch")

    stale_proxy = _StaleProxy()
    fresh = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[
            {
                "state": "terminal",
                "result": {"call_id": "call-1", "output": "recovered", "is_error": False},
            }
        ],
    )

    ws_provider = MagicMock()
    ws_provider.invalidate_forwarded_connection = AsyncMock()
    ws_provider.get_ready_connection = MagicMock(side_effect=[stale_proxy, fresh, fresh])
    ws_provider.wait_for_connection = AsyncMock(return_value=stale_proxy)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=_accepted_unknown_result("instance-a")
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("bash", {"command": "make release"})
    )

    assert result.is_error is False
    assert result.output == "recovered"
    ws_provider.invalidate_forwarded_connection.assert_awaited_once_with("exec-active", stale_proxy)
    loop_with_pool.tool_router.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_reporting_unknown_call_retries_as_definitely_unsent(
    loop_with_pool,
) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[{"state": "unknown"}],
    )

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(
        side_effect=[
            _accepted_unknown_result("instance-a"),
            ToolResult(output="ran once", is_error=False),
        ]
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("bash", {"command": "touch y"})
    )

    assert result.is_error is False
    assert result.output == "ran once"
    assert result.metadata["auto_retried"] is True
    assert loop_with_pool.tool_router.execute.await_count == 2


@pytest.mark.asyncio
async def test_executor_process_restart_still_reports_ambiguous(loop_with_pool) -> None:
    """A genuinely dead executor process keeps the strict no-replay guarantee."""

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    restarted = _ReconnectedConnection(
        instance_id="instance-b",
        reports=[{"state": "unknown"}],
    )

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=restarted)
    ws_provider.wait_for_connection = AsyncMock(return_value=restarted)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=_accepted_unknown_result("instance-a")
    )

    tc = _toolcall("bash", {"command": "rm -rf build"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)

    assert result.is_error is True
    assert result.metadata["code"] == "tool_outcome_ambiguous"
    assert result.metadata["uncertain"] is True
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome(tc.name, tc.arguments)
    assert not ctx.same_turn_tool_call_ledger.already_executed(tc.name, tc.arguments)
    loop_with_pool.tool_router.execute.assert_awaited_once()
    assert restarted.fetched == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        # Ran, but the executor no longer holds the result.
        {"state": "terminal_result_unavailable"},
        # Accept history was truncated: absence proves nothing.
        {"state": "indeterminate"},
        # Terminal claim without a payload cannot be trusted.
        {"state": "terminal"},
    ],
)
async def test_unrecoverable_reports_stay_ambiguous_and_are_never_replayed(
    loop_with_pool,
    report,
) -> None:
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    reconnected = _ReconnectedConnection(instance_id="instance-a", reports=[report])

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=_accepted_unknown_result("instance-a")
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("bash", {"command": "touch z"})
    )

    assert result.is_error is True
    assert result.metadata["code"] == "tool_outcome_ambiguous"
    loop_with_pool.tool_router.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_report_without_instance_identity_is_not_trusted(loop_with_pool) -> None:
    """An unidentified report must never be accepted as proof of non-execution."""

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")

    class _AnonymousConnection:
        executor_instance_id = None

        async def fetch_tool_result(self, call_id: str, **_kwargs: Any) -> dict[str, Any]:
            del call_id
            return {"state": "unknown"}

    anonymous = _AnonymousConnection()
    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=anonymous)
    ws_provider.wait_for_connection = AsyncMock(return_value=anonymous)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=_accepted_unknown_result("instance-a")
    )

    tc = _toolcall("bash", {"command": "rm -rf out"})
    result = await loop_with_pool._execute_regular_tool(ctx, tc)

    assert result.is_error is True
    assert result.metadata["code"] == "tool_outcome_ambiguous"
    loop_with_pool.tool_router.execute.assert_awaited_once()


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

    result = await loop_with_pool._execute_regular_tool(
        ctx,
        _toolcall("mcp_server__bash", arguments),
    )

    assert result.metadata["ambiguity"]["tool_name"] == "bash"
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("bash", dispatched_arguments)
    assert not ctx.same_turn_tool_call_ledger.uncertain_outcome("mcp_server__bash", arguments)
    executed_indexes, uncertain_indexes = _same_turn_duplicate_tool_call_indexes(
        ctx.same_turn_tool_call_ledger,
        [
            _toolcall("mcp_server__bash", arguments),
            _toolcall("bash", dispatched_arguments),
        ],
        ctx.tool_registry,
    )
    assert executed_indexes == set()
    assert uncertain_indexes == {0, 1}


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
    ws_provider.get_ready_connection = MagicMock(side_effect=[None])
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
async def test_transient_failure_wait_timeout_terminates_turn(
    loop_with_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.core.executor_recovery import (
        EXECUTOR_RECOVERY_WINDOW_SECONDS,
        ExecutorRecoveryTimeout,
        ExecutorRecoveryWindow,
    )
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(side_effect=[None])
    ws_provider.wait_for_connection = AsyncMock(return_value=None)
    loop_with_pool.providers.executor.websocket = ws_provider
    vars(loop_with_pool.providers)["_session_factory"] = object()
    unavailable_since = datetime(2026, 9, 3, 21, 30, tzinfo=UTC)
    window = ExecutorRecoveryWindow(
        executor_id="exec-active",
        unavailable_since=unavailable_since,
        deadline=unavailable_since + timedelta(seconds=EXECUTOR_RECOVERY_WINDOW_SECONDS),
        database_remaining_seconds=0,
    )
    monkeypatch.setattr(
        "cognis.core.executor_recovery.begin_executor_recovery",
        AsyncMock(return_value=window),
    )

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
    with pytest.raises(ExecutorRecoveryTimeout) as raised:
        await loop_with_pool._execute_regular_tool(ctx, tc)

    assert raised.value.detail["code"] == "executor_recovery_timeout"
    assert raised.value.detail["phase"] == "tool_reconciliation"
    assert raised.value.detail["deadline"] == window.deadline.isoformat()
    loop_with_pool.tool_router.execute.assert_awaited_once()
    ws_provider.wait_for_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_after_deadline_raises_without_clearing_marker(
    loop_with_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.core.executor_recovery import (
        ExecutorRecoveryTimeout,
        ExecutorRecoveryWindow,
    )
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ws_provider = MagicMock()

    async def reconnect_after_deadline(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        await asyncio.sleep(0.02)
        return MagicMock(name="reconnected")

    ws_provider.wait_for_connection = AsyncMock(side_effect=reconnect_after_deadline)
    loop_with_pool.providers.executor.websocket = ws_provider
    vars(loop_with_pool.providers)["_session_factory"] = object()
    now = datetime.now(UTC)
    window = ExecutorRecoveryWindow(
        executor_id="exec-active",
        unavailable_since=now,
        deadline=now + timedelta(seconds=900),
        database_remaining_seconds=900,
    )
    begin = AsyncMock(return_value=window)
    clear = AsyncMock()
    monkeypatch.setattr("cognis.core.executor_recovery.begin_executor_recovery", begin)
    monkeypatch.setattr("cognis.core.executor_recovery.clear_executor_recovery", clear)
    with pytest.raises(ExecutorRecoveryTimeout) as raised:
        await loop_with_pool._handle_tool_after_same_executor_transient_failure(
            ctx,
            tc=_toolcall("read", {"file_path": "/tmp/example"}),
            registered=ctx.tool_registry.get("read"),
            target_executor_id=None,
            failed_connection=ctx.executor_connection,
            output_chunk_callback=None,
            original_result=ToolResult(
                output="disconnected",
                is_error=True,
                metadata={
                    "code": "executor_disconnected",
                    "delivery_state": "not_sent",
                },
            ),
            recovery_deadline=asyncio.get_running_loop().time() + 0.005,
            recovery_window=window,
        )

    assert raised.value.detail["phase"] == "tool_reconciliation"
    begin.assert_not_awaited()
    clear.assert_not_awaited()
    loop_with_pool.tool_router.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_timeout_reuses_window_and_does_not_dispatch_twice(
    loop_with_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.core.executor_recovery import (
        ExecutorRecoveryTimeout,
        ExecutorRecoveryWindow,
    )
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=MagicMock(name="reconnected"))
    loop_with_pool.providers.executor.websocket = ws_provider
    vars(loop_with_pool.providers)["_session_factory"] = object()
    now = datetime.now(UTC)
    window = ExecutorRecoveryWindow(
        executor_id="exec-active",
        unavailable_since=now,
        deadline=now + timedelta(seconds=900),
        database_remaining_seconds=900,
    )
    begin = AsyncMock(return_value=window)
    monkeypatch.setattr("cognis.core.executor_recovery.begin_executor_recovery", begin)
    monkeypatch.setattr(
        "cognis.core.executor_recovery.clear_executor_recovery",
        AsyncMock(),
    )

    async def slow_retry(*args: Any, **kwargs: Any) -> ToolResult:
        del args, kwargs
        await asyncio.sleep(1)
        return ToolResult(output="unexpected")

    loop_with_pool.tool_router.execute = AsyncMock(side_effect=slow_retry)

    with pytest.raises(ExecutorRecoveryTimeout):
        await loop_with_pool._handle_tool_after_same_executor_transient_failure(
            ctx,
            tc=_toolcall("read", {"file_path": "/tmp/example"}),
            registered=ctx.tool_registry.get("read"),
            target_executor_id=None,
            failed_connection=ctx.executor_connection,
            output_chunk_callback=None,
            original_result=ToolResult(
                output="disconnected",
                is_error=True,
                metadata={
                    "code": "executor_disconnected",
                    "delivery_state": "not_sent",
                },
            ),
            recovery_deadline=asyncio.get_running_loop().time() + 0.2,
            recovery_window=window,
        )

    begin.assert_not_awaited()
    assert loop_with_pool.tool_router.execute.await_count == 1


@pytest.mark.asyncio
async def test_accepted_unknown_recursion_reuses_original_recovery_window(
    loop_with_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.core.executor_recovery import (
        ExecutorRecoveryTimeout,
        ExecutorRecoveryWindow,
    )
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ws_provider = MagicMock()
    reconnect_calls = 0
    reconciled_not_sent = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[{"state": "not_sent"}],
    )
    replay_connection = MagicMock(name="replay-connection")

    async def reconnect_then_expire(*args: Any, **kwargs: Any) -> Any:
        nonlocal reconnect_calls
        del args, kwargs
        reconnect_calls += 1
        if reconnect_calls == 1:
            return reconciled_not_sent
        if reconnect_calls == 2:
            return replay_connection
        await asyncio.sleep(0.21)
        return None

    ws_provider.wait_for_connection = AsyncMock(side_effect=reconnect_then_expire)
    loop_with_pool.providers.executor.websocket = ws_provider
    vars(loop_with_pool.providers)["_session_factory"] = object()
    now = datetime.now(UTC)
    window = ExecutorRecoveryWindow(
        executor_id="exec-active",
        unavailable_since=now,
        deadline=now + timedelta(seconds=900),
        database_remaining_seconds=900,
    )
    begin = AsyncMock(return_value=window)
    clear = AsyncMock()
    monkeypatch.setattr("cognis.core.executor_recovery.begin_executor_recovery", begin)
    monkeypatch.setattr("cognis.core.executor_recovery.clear_executor_recovery", clear)
    monkeypatch.setattr(
        loop_with_pool,
        "_is_same_executor_transient_failure",
        lambda _result: True,
    )
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=ToolResult(
            output="accepted outcome unknown",
            is_error=True,
            metadata={
                "code": "executor_delivery_failure",
                "delivery_state": "accepted_unknown",
                "transport": {"executor_instance_id": "instance-a"},
            },
        )
    )

    with pytest.raises(ExecutorRecoveryTimeout):
        await loop_with_pool._handle_tool_after_same_executor_transient_failure(
            ctx,
            tc=_toolcall("read", {"file_path": "/tmp/example"}),
            registered=ctx.tool_registry.get("read"),
            target_executor_id=None,
            failed_connection=ctx.executor_connection,
            output_chunk_callback=None,
            original_result=_accepted_unknown_result("instance-a"),
            recovery_deadline=asyncio.get_running_loop().time() + 0.2,
            recovery_window=window,
        )

    begin.assert_not_awaited()
    clear.assert_not_awaited()
    assert loop_with_pool.tool_router.execute.await_count == 1
    assert ws_provider.wait_for_connection.await_count == 3
    assert reconciled_not_sent.fetched == ["call-1"]


@pytest.mark.asyncio
async def test_accepted_unknown_fetch_observes_fence_loss(loop_with_pool) -> None:
    from cognis.core.agent_loop import StepInterrupted
    from cognis.models.tool import ToolResult

    ctx = _ctx(ExecutorPool(primary=[_read_target("exec-active")]))

    class Fence:
        checks = 0

        async def assert_current(self) -> None:
            self.checks += 1
            if self.checks > 1:
                raise StepInterrupted("stale owner")

    ctx.execution_fence = Fence()
    fetch_cancelled = asyncio.Event()

    async def blocked_fetch(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        try:
            await asyncio.sleep(60)
        finally:
            fetch_cancelled.set()

    conn = MagicMock()
    conn.executor_instance_id = "instance-a"
    conn.fetch_tool_result = blocked_fetch
    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=conn)
    loop_with_pool.providers.executor.websocket = ws_provider

    with pytest.raises(StepInterrupted, match="stale owner"):
        await asyncio.wait_for(
            loop_with_pool._reconcile_accepted_tool_call(
                ctx,
                tc=_toolcall("read", {"file_path": "/tmp/example"}),
                registered=ctx.tool_registry.get("read"),
                executor_id="exec-active",
                failed_connection=ctx.executor_connection,
                original_result=ToolResult(
                    output="accepted outcome unknown",
                    is_error=True,
                    metadata={
                        "code": "executor_delivery_failure",
                        "delivery_state": "accepted_unknown",
                        "transport": {"executor_instance_id": "instance-a"},
                    },
                ),
                deadline=asyncio.get_running_loop().time() + 30,
            ),
            timeout=2,
        )
    assert fetch_cancelled.is_set()


@pytest.mark.asyncio
async def test_target_executor_retry_waits_same_target_not_primary(loop_with_pool) -> None:
    from cognis.models.tool import ToolResult

    pool = ExecutorPool(primary=[_read_target("primary"), _read_target("secondary")])
    ctx = _ctx(pool, active_executor_id="primary")
    primary_conn = ctx.executor_connection
    secondary_conn = MagicMock(name="secondary_connection")
    secondary_reconnected = MagicMock(name="secondary_reconnected")

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=secondary_conn)
    ws_provider.wait_for_connection = AsyncMock(return_value=secondary_reconnected)
    ws_provider.get_handle_metadata = MagicMock(
        return_value={
            "environment": {
                "home": "/secondary",
                "cwd": "/secondary/project",
            }
        }
    )
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
    first_tc = loop_with_pool.tool_router.execute.await_args_list[0].args[0]
    second_tc = second_call.args[0]
    assert first_tc.runtime_metadata["working_directory"] == "/secondary/project"
    assert second_tc.runtime_metadata == first_tc.runtime_metadata


@pytest.mark.asyncio
async def test_parallel_ordinary_ambiguity_does_not_cancel_or_duplicate_siblings(
    loop_with_pool,
) -> None:
    from cognis.core.agent_loop import (
        _PreparedRegularToolCall,
        _same_turn_duplicate_tool_call_indexes,
    )
    from cognis.models.tool import ToolCall, ToolResult

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    connection = ctx.executor_connection
    ws_provider = MagicMock()
    ws_provider.get_connection = MagicMock(return_value=connection)
    ws_provider.wait_for_connection = AsyncMock()
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    bash = ToolCall(
        call_id="call-bash",
        name="bash",
        arguments={"command": "touch /tmp/uncertain"},
    )
    read = ToolCall(
        call_id="call-read",
        name="read",
        arguments={"file_path": "/tmp/example"},
    )

    async def execute(tool_call: ToolCall, *_args: Any, **_kwargs: Any) -> ToolResult:
        if tool_call.name == "bash":
            return ToolResult(
                output="bridge disconnected",
                is_error=True,
                metadata={
                    "code": "executor_disconnected",
                    "delivery_state": "accepted_unknown",
                    "executor_id": "exec-active",
                    "generation": None,
                    "epoch": 7,
                    "same_executor_only": True,
                },
            )
        return ToolResult(output="read completed", is_error=False)

    loop_with_pool.tool_router.execute = AsyncMock(side_effect=execute)

    results = await loop_with_pool._execute_parallel_regular_tool_group(
        ctx,
        [
            _PreparedRegularToolCall(bash, "tool-bash"),
            _PreparedRegularToolCall(read, "tool-read"),
        ],
    )

    assert len(results) == 2
    assert results[0].metadata["code"] == "tool_outcome_ambiguous"
    assert results[0].metadata["ambiguity"]["tool_name"] == "bash"
    assert results[1].output == "read completed"
    assert results[1].is_error is False
    assert loop_with_pool.tool_router.execute.await_count == 2
    ws_provider.wait_for_connection.assert_not_awaited()
    executed_indexes, uncertain_indexes = _same_turn_duplicate_tool_call_indexes(
        ctx.same_turn_tool_call_ledger,
        [bash, read],
        ctx.tool_registry,
    )
    assert executed_indexes == set()
    assert uncertain_indexes == {0}


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
            ctx.same_turn_tool_call_ledger.record_uncertain_fingerprint(tool_call.name, fingerprint)
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
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("write_one", first.arguments)
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("write_two", sibling.arguments)
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

    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("write_two", sibling.arguments)
    assert exc.value.detail()["uncertain_tool_calls"] == [
        {
            "tool_name": "write_two",
            "argument_fingerprint": tool_call_argument_fingerprint("write_two", sibling.arguments),
        }
    ]

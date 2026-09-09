"""HA bridged tool-call recovery: proxy retirement, bounded retries, diagnostics.

These tests cover the failure mode where a controller that does not own an
executor's WebSocket forwards tool calls through the peer-controller bridge.
A single bridged call failure previously (a) aborted every other session's
in-flight call on the shared proxy, (b) got exactly one retry regardless of the
configured recovery budget, and (c) produced an unattributable error because
the real cause was discarded.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.providers.executor.delivery import DeliveryState, ExecutorDeliveryError
from cognis.providers.executor.forwarding import (
    ForwardedDeliveryError,
    ForwardedExecutorConnection,
)


@pytest.fixture
def loop_with_pool():
    """Minimal AgentLoop-shaped object exercising _execute_regular_tool."""

    from cognis.core.agent_loop import AgentLoop

    instance = AgentLoop.__new__(AgentLoop)
    instance.tool_router = MagicMock()
    instance.tool_router.execute = AsyncMock()
    instance.providers = MagicMock()
    instance.session_manager = SimpleNamespace(session_factory=None)
    return instance


def _executor_registry() -> Any:
    """Registry holding one executor-routed tool so dispatch leaves the controller."""

    from cognis.models.tool import NativeToolDefinition as ToolDefinition
    from cognis.models.tool import ToolSource
    from cognis.tools.registry import RegisteredTool, ToolRegistry

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="grep",
                description="Search file contents",
                parameters={
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                },
                source=ToolSource(type="executor"),
                category="search",
                read_only=True,
                timeout_seconds=5,
            )
        )
    )
    return registry


def _proxy(*, executor_id: str = "maitrea_riker", epoch: int = 236) -> Any:
    return ForwardedExecutorConnection(
        executor_id=executor_id,
        capabilities=MagicMock(),
        owner_id="cognis-1",
        epoch=epoch,
        owner_internal_url="http://cognis-1.internal:8080",
        requester_owner_id="cognis-0",
        auth_provider=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Proxy retirement: one session's failure must not abort another's tool call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_keeps_in_flight_calls_alive() -> None:
    """Retiring a shared proxy must not fail calls belonging to other sessions."""

    connection = _proxy()
    loop = asyncio.get_running_loop()
    other_session_call: asyncio.Future[dict[str, Any]] = loop.create_future()
    pending = MagicMock()
    pending.future = other_session_call
    pending.submitted = True
    pending.assembly = None
    connection._pending["bridge-call-other-session"] = pending

    await connection.retire()

    assert connection.retiring is True
    assert connection.closing is False
    assert not other_session_call.done(), "in-flight call of another session was aborted"

    # The retired proxy refuses new work so callers resolve a fresh transport.
    with pytest.raises(ForwardedDeliveryError) as excinfo:
        await connection._start_call("tool", {}, timeout=5.0)
    assert excinfo.value.delivery_state == DeliveryState.NOT_SENT

    # Once the other session's call completes, the proxy closes itself.
    other_session_call.set_result({"output": "ok"})
    connection._pop_pending("bridge-call-other-session")
    for _ in range(80):
        if connection.closing:
            break
        await asyncio.sleep(0.05)
    assert connection.closing is True


@pytest.mark.asyncio
async def test_retire_closes_immediately_when_idle() -> None:
    """An idle proxy needs no drain window."""

    connection = _proxy()
    await connection.retire()
    assert connection.closing is True
    assert connection._drain_task is None


@pytest.mark.asyncio
async def test_retire_drain_timeout_closes_and_fails_stragglers() -> None:
    """A stuck call cannot keep a retired proxy alive forever."""

    connection = _proxy()
    loop = asyncio.get_running_loop()
    stuck: asyncio.Future[dict[str, Any]] = loop.create_future()
    pending = MagicMock()
    pending.future = stuck
    pending.submitted = True
    pending.assembly = None
    connection._pending["stuck"] = pending

    await connection.retire(drain_timeout=0.05)
    for _ in range(80):
        if connection.closing:
            break
        await asyncio.sleep(0.05)

    assert connection.closing is True
    assert stuck.done()
    with pytest.raises(ForwardedDeliveryError) as excinfo:
        stuck.result()
    # The call was physically submitted, so its outcome stays unknown rather
    # than being reported as never sent.
    assert excinfo.value.delivery_state == DeliveryState.ACCEPTED_UNKNOWN


@pytest.mark.asyncio
async def test_provider_invalidation_retires_and_stops_routing() -> None:
    """Invalidation removes the proxy from routing without closing live calls."""

    from cognis.providers.executor.websocket import WebSocketExecutorProvider

    provider = WebSocketExecutorProvider.__new__(WebSocketExecutorProvider)
    provider._cluster_refresh_lock = asyncio.Lock()
    provider._connections = {}
    provider._forwarded_connections = {}
    provider._forwarded_by_executor = {}
    provider._retiring_forwarded = set()
    provider._handles = {}

    connection = _proxy()
    loop = asyncio.get_running_loop()
    in_flight: asyncio.Future[dict[str, Any]] = loop.create_future()
    pending = MagicMock()
    pending.future = in_flight
    pending.submitted = True
    pending.assembly = None
    connection._pending["in-flight"] = pending

    provider._forwarded_by_executor["maitrea_riker"] = connection
    provider._forwarded_connections[("maitrea_riker", "cognis-1", 236)] = connection

    await provider.invalidate_forwarded_connection("maitrea_riker", connection)

    assert provider.get_connection("maitrea_riker") is None
    assert connection in provider._retiring_forwarded
    assert not in_flight.done()
    assert connection.retiring is True

    # A retired proxy is never eligible for a same-executor retry.
    assert (
        provider._connection_is_retry_eligible(
            "maitrea_riker",
            connection,
            failed_connection=None,
            delivery_state=DeliveryState.NOT_SENT,
            accepted_unknown_replay_safe=False,
            failed_generation=None,
            failed_owner_id=None,
            failed_epoch=None,
            require_recovered_connection=False,
        )
        is False
    )

    await connection.close()


@pytest.mark.asyncio
async def test_retiring_proxy_budget_evicts_oldest_per_executor() -> None:
    """Repeated churn cannot retain an unbounded number of proxy resources."""

    from cognis.providers.executor.websocket import (
        _MAX_RETIRING_FORWARDED_PROXIES_PER_EXECUTOR,
        WebSocketExecutorProvider,
    )

    provider = WebSocketExecutorProvider.__new__(WebSocketExecutorProvider)
    provider._retiring_forwarded = set()
    proxies = []
    pending_futures = []
    for epoch in range(_MAX_RETIRING_FORWARDED_PROXIES_PER_EXECUTOR + 1):
        connection = _proxy(epoch=epoch)
        pending = MagicMock()
        pending.future = asyncio.get_running_loop().create_future()
        pending_futures.append(pending.future)
        pending.submitted = True
        pending.accepted = True
        pending.assembly = None
        connection._pending[f"call-{epoch}"] = pending
        proxies.append(connection)
        await provider._retire_forwarded_proxy(connection)

    assert len(provider._retiring_forwarded) == _MAX_RETIRING_FORWARDED_PROXIES_PER_EXECUTOR
    assert proxies[0].closing is True
    assert proxies[-1].closing is False

    for connection in proxies:
        await connection.close()
    for future in pending_futures:
        if future.done() and not future.cancelled():
            future.exception()


@pytest.mark.asyncio
async def test_retiring_proxy_global_budget_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different executors cannot bypass the provider-wide retirement bound."""

    import cognis.providers.executor.websocket as websocket_module
    from cognis.providers.executor.websocket import WebSocketExecutorProvider

    monkeypatch.setattr(websocket_module, "_MAX_RETIRING_FORWARDED_PROXIES", 2)
    provider = WebSocketExecutorProvider.__new__(WebSocketExecutorProvider)
    provider._retiring_forwarded = set()
    proxies = []
    futures = []
    for index in range(3):
        connection = _proxy(executor_id=f"exec-{index}", epoch=index)
        pending = MagicMock()
        pending.future = asyncio.get_running_loop().create_future()
        futures.append(pending.future)
        pending.submitted = True
        pending.accepted = True
        pending.assembly = None
        connection._pending[f"call-{index}"] = pending
        proxies.append(connection)
        await provider._retire_forwarded_proxy(connection)

    assert len(provider._retiring_forwarded) == 2
    assert proxies[0].closing is True
    for connection in proxies:
        await connection.close()
    for future in futures:
        if future.done() and not future.cancelled():
            future.exception()


# ---------------------------------------------------------------------------
# Bridge diagnostics: the real cause must survive to metadata and logs
# ---------------------------------------------------------------------------


class _BridgeFailingExecutor:
    """Forwarded proxy shape whose tool dispatch fails inside the bridge."""

    def __init__(self, error: ForwardedDeliveryError) -> None:
        self.executor_id = "maitrea_riker"
        self.owner_id = "cognis-1"
        self.epoch = 236
        self._error = error
        self.cancelled: list[str] = []

    async def tool_execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self._error

    async def cancel_call(self, call_id: str) -> None:
        self.cancelled.append(call_id)


@pytest.mark.parametrize(
    ("delivery_state", "expected_retryable"),
    [
        (DeliveryState.NOT_SENT, True),
        (DeliveryState.ACCEPTED_UNKNOWN, True),
        (DeliveryState.TERMINAL, False),
    ],
)
@pytest.mark.asyncio
async def test_forwarded_delivery_failure_preserves_cause_and_retryability(
    delivery_state: DeliveryState,
    expected_retryable: bool,
) -> None:
    """A bridged failure must carry its cause, identity, and retryability."""

    from cognis.core.tool_router import ToolRouter
    from cognis.models.tool import ToolCall
    from tests.unit.test_tool_router import _agent, _Guardrails, _session

    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    executor = _BridgeFailingExecutor(
        ForwardedDeliveryError(
            "Bridge owner changed before physical send",
            delivery_state,
            executor_id="maitrea_riker",
            owner_id="cognis-1",
            epoch=236,
            executor_instance_id="instance-a",
        )
    )

    result = await router.execute(
        ToolCall(call_id="call-1", name="grep", arguments={"pattern": "x"}),
        _session(),
        _agent(),
        _executor_registry(),
        executor,
    )

    assert result.is_error is True
    metadata = result.metadata or {}
    assert metadata["retryable"] is expected_retryable
    assert metadata["same_executor_only"] is True
    # Route and accepting-process identity are what make the failure diagnosable
    # and, for accepted calls, reconcilable.
    assert metadata["transport"]["route"] == "forwarded_bridge"
    assert metadata["transport"]["executor_instance_id"] == "instance-a"
    assert metadata["transport"]["delivery_state"] == delivery_state.value


def test_bridge_failure_log_data_is_redaction_safe() -> None:
    """Bridge failure logs carry identities and categories, never payloads."""

    from cognis.api.controller_ws import _bridge_failure_log_data

    data = _bridge_failure_log_data(
        executor_id="maitrea_riker",
        bridge_call_id="bridge-1",
        requester_owner_id="cognis-0",
        operation="tool",
        delivery_state="accepted_unknown",
        reason="bridge_local_error",
        code=None,
        executor_instance_id="instance-a",
    )

    assert data["reason"] == "bridge_local_error"
    assert data["executor_instance_id"] == "instance-a"
    assert "message" not in data
    assert set(data) == {
        "executor_id",
        "bridge_call_id",
        "requester_owner_id",
        "operation",
        "delivery_state",
        "code",
        "reason",
        "executor_instance_id",
    }


# ---------------------------------------------------------------------------
# Bounded retries: a single retry is not enough when the shared proxy churns
# ---------------------------------------------------------------------------


def _bridge_failure_result(*, delivery_state: str, instance_id: str | None) -> Any:
    from cognis.models.tool import ToolResult

    transport: dict[str, Any] = {
        "route": "forwarded_bridge",
        "delivery_state": delivery_state,
    }
    if instance_id is not None:
        transport["executor_instance_id"] = instance_id
    return ToolResult(
        output="Executor delivery failed; recovery is restricted to the same executor.",
        is_error=True,
        metadata={
            "code": "executor_delivery_failure",
            "delivery_state": delivery_state,
            "executor_id": "exec-active",
            "retryable": True,
            "same_executor_only": True,
            "transport": transport,
        },
    )


@pytest.mark.asyncio
async def test_replay_safe_tool_retries_more_than_once_on_bridge_churn(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive bridge failures must not end a read-only tool call.

    Retiring the shared proxy can fail the attempt immediately following it, so
    the recovery budget has to cover more than one retry.
    """

    from cognis.core.executor_pool import ExecutorPool
    from cognis.models.tool import ToolResult
    from tests.unit.test_target_executor_routing import _ctx, _read_target, _toolcall

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    pool = ExecutorPool(primary=[_read_target("exec-active")])
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
        side_effect=[
            _bridge_failure_result(delivery_state="not_sent", instance_id=None),
            _bridge_failure_result(delivery_state="not_sent", instance_id=None),
            ToolResult(output="matches", is_error=False),
        ]
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("read", {"file_path": "/tmp/example"})
    )

    assert result.is_error is False
    assert result.output == "matches"
    assert loop_with_pool.tool_router.execute.await_count == 3
    assert result.metadata["auto_retried"] is True


@pytest.mark.asyncio
async def test_bounded_retries_stop_at_the_attempt_ceiling(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retries are bounded so a persistently broken bridge cannot loop forever."""

    from cognis.core.agent_loop import _SAME_EXECUTOR_MAX_DISPATCH_ATTEMPTS
    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import _ctx, _read_target, _toolcall

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    pool = ExecutorPool(primary=[_read_target("exec-active")])
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
        return_value=_bridge_failure_result(delivery_state="not_sent", instance_id=None)
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("read", {"file_path": "/tmp/example"})
    )

    assert result.is_error is True
    assert loop_with_pool.tool_router.execute.await_count == _SAME_EXECUTOR_MAX_DISPATCH_ATTEMPTS


@pytest.mark.asyncio
async def test_reconciled_unknown_preserves_cumulative_attempt_ceiling(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconciliation must not reset attempts when it proves a call never ran."""

    from cognis.core.agent_loop import _SAME_EXECUTOR_MAX_DISPATCH_ATTEMPTS
    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import (
        _ctx,
        _read_target,
        _ReconnectedConnection,
        _toolcall,
    )

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[{"state": "unknown"}],
    )
    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        side_effect=[
            _bridge_failure_result(delivery_state="not_sent", instance_id=None),
            _bridge_failure_result(delivery_state="accepted_unknown", instance_id="instance-a"),
            _bridge_failure_result(delivery_state="not_sent", instance_id=None),
        ]
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("read", {"file_path": "/tmp/example"})
    )

    assert result.is_error is True
    assert result.metadata["auto_retry_skipped_reason"] == "dispatch_attempt_limit"
    assert "recovery is restricted" not in result.output
    assert "Check that executor 'exec-active' is active" in result.output
    assert loop_with_pool.tool_router.execute.await_count == _SAME_EXECUTOR_MAX_DISPATCH_ATTEMPTS
    assert reconnected.fetched == ["call-1"]


@pytest.mark.asyncio
async def test_recovery_dispatch_is_bounded_by_absolute_deadline(loop_with_pool) -> None:
    """A retry started near deadline cannot run for the tool's full timeout."""

    from cognis.core.executor_pool import ExecutorPool
    from cognis.core.executor_recovery import (
        ExecutorRecoveryTimeout,
        ExecutorRecoveryWindow,
    )
    from tests.unit.test_target_executor_routing import _ctx, _read_target, _toolcall

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")
    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}

    async def never_finishes(*_args: Any, **_kwargs: Any) -> Any:
        await _kwargs["after_executor_send"]("exec-active", "instance-a")
        await asyncio.Event().wait()

    loop_with_pool.tool_router.execute = AsyncMock(side_effect=never_finishes)
    tc = _toolcall("read", {"file_path": "/tmp/example"})
    registered = ctx.tool_registry.get("read")
    deadline = asyncio.get_running_loop().time() + 0.02
    unavailable_since = datetime.now(UTC)
    window = ExecutorRecoveryWindow(
        executor_id="exec-active",
        unavailable_since=unavailable_since,
        deadline=unavailable_since + timedelta(seconds=900),
        database_remaining_seconds=900,
    )

    with pytest.raises(ExecutorRecoveryTimeout) as raised:
        await loop_with_pool._handle_tool_after_same_executor_transient_failure(
            ctx,
            tc=tc,
            registered=registered,
            target_executor_id=None,
            failed_connection=ctx.executor_connection,
            output_chunk_callback=None,
            original_result=_bridge_failure_result(delivery_state="not_sent", instance_id=None),
            recovery_deadline=deadline,
            recovery_window=window,
        )

    assert raised.value.detail["code"] == "executor_recovery_timeout"
    assert loop_with_pool.tool_router.execute.await_count == 1


@pytest.mark.asyncio
async def test_terminal_replay_result_wins_after_recovery_deadline(
    loop_with_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal replay result cannot be replaced by an infrastructure timeout."""

    from cognis.core.executor_pool import ExecutorPool
    from cognis.core.executor_recovery import ExecutorRecoveryWindow
    from cognis.models.tool import ToolResult
    from tests.unit.test_target_executor_routing import _ctx, _read_target, _toolcall

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")
    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    loop_with_pool.providers.executor.websocket = ws_provider
    vars(loop_with_pool.providers)["_session_factory"] = object()
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    authoritative = ToolResult(
        output="authoritative terminal output",
        is_error=False,
        metadata={"delivery_state": "completed", "result_id": "result-1"},
    )
    loop_with_pool.tool_router.execute = AsyncMock(return_value=authoritative)
    real_wait_for = asyncio.wait_for

    async def finish_after_deadline(awaitable: Any, *, timeout: float) -> Any:
        await asyncio.sleep(timeout + 0.01)
        return await awaitable

    monkeypatch.setattr(asyncio, "wait_for", finish_after_deadline)
    clear_recovery = AsyncMock()
    monkeypatch.setattr(
        "cognis.core.executor_recovery.clear_executor_recovery",
        clear_recovery,
    )
    now = datetime.now(UTC)
    window = ExecutorRecoveryWindow(
        executor_id="exec-active",
        unavailable_since=now,
        deadline=now + timedelta(seconds=900),
        database_remaining_seconds=900,
    )
    recovery_deadline = asyncio.get_running_loop().time() + 0.005

    try:
        result = await loop_with_pool._handle_tool_after_same_executor_transient_failure(
            ctx,
            tc=_toolcall("read", {"file_path": "/tmp/example"}),
            registered=ctx.tool_registry.get("read"),
            target_executor_id=None,
            failed_connection=ctx.executor_connection,
            output_chunk_callback=None,
            original_result=_bridge_failure_result(delivery_state="not_sent", instance_id=None),
            recovery_deadline=recovery_deadline,
            recovery_window=window,
        )
    finally:
        monkeypatch.setattr(asyncio, "wait_for", real_wait_for)

    assert asyncio.get_running_loop().time() > recovery_deadline
    assert result is not None
    assert result.output == authoritative.output
    assert result.is_error == authoritative.is_error
    assert result.metadata["result_id"] == authoritative.metadata["result_id"]
    assert result.metadata.get("code") != "executor_recovery_timeout"
    clear_recovery.assert_awaited_once()
    loop_with_pool.tool_router.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_accepted_mutation_at_recovery_deadline_fails_closed(loop_with_pool) -> None:
    """Deadline cancellation after acceptance must become an ambiguous mutation."""

    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import _ctx, _target, _toolcall

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")
    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}

    async def accepted_then_hangs(*_args: Any, **kwargs: Any) -> Any:
        await kwargs["after_executor_send"]("exec-active", "instance-a")
        await asyncio.Event().wait()

    loop_with_pool.tool_router.execute = AsyncMock(side_effect=accepted_then_hangs)
    tc = _toolcall("bash", {"command": "mv a b"})
    result = await loop_with_pool._handle_tool_after_same_executor_transient_failure(
        ctx,
        tc=tc,
        registered=ctx.tool_registry.get("bash"),
        target_executor_id=None,
        failed_connection=ctx.executor_connection,
        output_chunk_callback=None,
        original_result=_bridge_failure_result(delivery_state="not_sent", instance_id=None),
        recovery_deadline=asyncio.get_running_loop().time() + 0.02,
    )

    assert result is not None
    assert result.metadata["code"] == "tool_outcome_ambiguous"
    assert result.metadata["uncertain"] is True
    # With the guaranteed minimal reconcile window, an accepted call is now
    # queried even past the recovery deadline; the mock connection cannot
    # answer, so the bounded fetch retries fail closed.
    assert result.metadata["ambiguity"]["reconcile_outcome"] == "fetch_failed"
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("bash", {"command": "mv a b"})


def test_destructive_or_privileged_read_tool_is_not_replay_safe(loop_with_pool) -> None:
    """READ combined with an unsafe capability must never authorize replay."""

    from cognis.models.tool import NativeToolDefinition, ToolCapability, ToolSource
    from cognis.tools.registry import RegisteredTool

    for unsafe in (ToolCapability.DESTRUCTIVE, ToolCapability.PRIVILEGED):
        registered = RegisteredTool(
            definition=NativeToolDefinition(
                name=f"unsafe_{unsafe.value}",
                description="unsafe",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                capabilities={ToolCapability.READ, unsafe},
            )
        )
        assert loop_with_pool._tool_safe_for_same_executor_retry(registered) is False


@pytest.mark.asyncio
async def test_bridge_capacity_failure_does_not_retire_healthy_proxy(loop_with_pool) -> None:
    """Local capacity pressure must not create a succession of draining proxies."""

    from cognis.core.executor_pool import ExecutorPool
    from cognis.models.tool import ToolResult
    from tests.unit.test_target_executor_routing import _ctx, _read_target, _toolcall

    pool = ExecutorPool(primary=[_read_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    forwarded = MagicMock(name="forwarded_connection")
    forwarded.owner_id = "cognis-1"
    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=forwarded)
    ws_provider.invalidate_forwarded_connection = AsyncMock()
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: forwarded
    loop_with_pool.tool_router.execute = AsyncMock(
        side_effect=[
            ToolResult(
                output="capacity",
                is_error=True,
                metadata={
                    "code": "executor_bridge_capacity",
                    "delivery_state": "not_sent",
                    "executor_id": "exec-active",
                    "same_executor_only": True,
                    "retryable": True,
                },
            ),
            ToolResult(output="ok", is_error=False),
        ]
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("read", {"file_path": "/tmp/example"})
    )

    assert result.is_error is False
    ws_provider.invalidate_forwarded_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutating_tool_is_never_redispatched_on_bridge_failure(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety invariant: an accepted mutating call is never replayed."""

    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import _ctx, _target, _toolcall

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")
    # A restarted process: reconciliation cannot prove the call never ran.
    fresh_conn.executor_instance_id = "instance-b"

    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=fresh_conn)
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        return_value=_bridge_failure_result(
            delivery_state="accepted_unknown", instance_id="instance-a"
        )
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("bash", {"command": "mv a b"})
    )

    assert result.metadata["code"] == "tool_outcome_ambiguous"
    loop_with_pool.tool_router.execute.assert_awaited_once()
    ambiguity = result.metadata["ambiguity"]
    # Diagnosability: the payload states why the outcome stayed unresolved and
    # which process had accepted the call.
    assert ambiguity["reconcile_outcome"] == "instance_mismatch"
    assert ambiguity["transport"]["executor_instance_id"] == "instance-a"
    assert ambiguity["transport"]["route"] == "forwarded_bridge"


@pytest.mark.asyncio
async def test_mutating_not_sent_then_accepted_unknown_is_reconciled_not_replayed(
    loop_with_pool,
) -> None:
    """A mutation may retry only until an executor accepts it."""

    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import _ctx, _target, _toolcall

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    fresh_conn = MagicMock(name="fresh_connection")
    fresh_conn.executor_instance_id = "instance-b"

    ws_provider = MagicMock()
    ws_provider.wait_for_connection = AsyncMock(return_value=fresh_conn)
    loop_with_pool.providers.executor.websocket = ws_provider
    loop_with_pool._get_tool_registry = lambda c: c.tool_registry
    loop_with_pool._tool_runtime_metadata = lambda c: {}
    loop_with_pool._get_executor = lambda c: c.executor_connection
    loop_with_pool.tool_router.execute = AsyncMock(
        side_effect=[
            _bridge_failure_result(delivery_state="not_sent", instance_id=None),
            _bridge_failure_result(delivery_state="accepted_unknown", instance_id="instance-a"),
        ]
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("bash", {"command": "mv a b"})
    )

    assert result.metadata["code"] == "tool_outcome_ambiguous"
    assert result.metadata["uncertain"] is True
    assert result.metadata["ambiguity"]["reconcile_outcome"] == "instance_mismatch"
    assert loop_with_pool.tool_router.execute.await_count == 2
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("bash", {"command": "mv a b"})


@pytest.mark.asyncio
async def test_ambiguity_records_missing_accepting_instance(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge-local failure without attributable identity is labelled as such."""

    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import _ctx, _target, _toolcall

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
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
        return_value=_bridge_failure_result(delivery_state="accepted_unknown", instance_id=None)
    )

    result = await loop_with_pool._execute_regular_tool(
        ctx, _toolcall("bash", {"command": "mv a b"})
    )

    assert result.metadata["ambiguity"]["reconcile_outcome"] == "no_accepted_instance"


def test_physical_delivery_failure_preserves_identity_without_raw_message() -> None:
    """Physical delivery metadata is diagnostic without carrying raw errors."""

    error = ExecutorDeliveryError(
        "Executor connection replaced",
        DeliveryState.ACCEPTED_UNKNOWN,
        executor_id="maitrea_riker",
        executor_instance_id="instance-a",
    )
    metadata = error.metadata()
    assert metadata["executor_instance_id"] == "instance-a"
    assert "delivery_error_message" not in metadata


# ---------------------------------------------------------------------------
# Reconcile deadlines: long-running calls must not become falsely ambiguous
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_waits_for_active_call_beyond_recovery_budget(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed ACTIVE call is awaited within the tool's completion budget.

    Regression for the production ``reconcile_outcome: deadline_exceeded``
    ambiguity: a long-running bash command outlived the 60s reconnect budget
    while the accepting executor process kept reporting it active. The real
    outcome must be recovered, not abandoned at the recovery deadline.
    """

    import cognis.core.agent_loop as agent_loop_module
    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import (
        _ctx,
        _prepare_reconcile_loop,
        _ReconnectedConnection,
        _target,
        _toolcall,
    )

    monkeypatch.setattr(agent_loop_module, "_TOOL_RECONCILE_POLL_SECONDS", 0.001)
    # Shrink the guaranteed minimal window below the simulated recovery
    # deadline so the recovery only succeeds through the ACTIVE-state
    # completion-budget extension, not through the floor.
    monkeypatch.setattr(agent_loop_module, "_TOOL_RECONCILE_MIN_WINDOW_SECONDS", 0.001)
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    # Enough active reports that polling provably crosses the recovery deadline
    # before the terminal result is retained.
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[
            *([{"state": "active"}] * 40),
            {
                "state": "terminal",
                "result": {"call_id": "call-1", "output": "finished late", "is_error": False},
            },
        ],
    )
    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider

    tc = _toolcall("bash", {"command": "sleep 600 && make release"})
    loop = asyncio.get_running_loop()
    recovery_deadline = loop.time() + 0.005

    state, result, reason = await loop_with_pool._reconcile_accepted_tool_call(
        ctx,
        tc=tc,
        registered=ctx.tool_registry.get("bash"),
        executor_id="exec-active",
        failed_connection=ctx.executor_connection,
        original_result=_bridge_failure_result(
            delivery_state="accepted_unknown", instance_id="instance-a"
        ),
        deadline=recovery_deadline,
    )

    assert state == "terminal"
    assert reason == "recovered"
    assert result is not None
    assert result.output == "finished late"
    assert loop.time() > recovery_deadline
    assert len(reconnected.fetched) == 41


@pytest.mark.asyncio
async def test_reconcile_active_wait_is_bounded_by_completion_budget(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An eternally-active report cannot hold reconciliation forever."""

    import cognis.core.agent_loop as agent_loop_module
    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import (
        _ctx,
        _prepare_reconcile_loop,
        _ReconnectedConnection,
        _target,
        _toolcall,
    )

    monkeypatch.setattr(agent_loop_module, "_TOOL_RECONCILE_POLL_SECONDS", 0.001)
    monkeypatch.setattr(
        agent_loop_module, "_TOOL_RECONCILE_DEFAULT_COMPLETION_BUDGET_SECONDS", 0.01
    )
    monkeypatch.setattr(agent_loop_module, "_TOOL_RECONCILE_COMPLETION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(agent_loop_module, "_TOOL_RECONCILE_MIN_WINDOW_SECONDS", 0.01)
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[{"state": "active"}],
    )
    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider

    tc = _toolcall("bash", {"command": "sleep infinity"})
    # No registered tool: the default completion budget applies.
    state, result, reason = await loop_with_pool._reconcile_accepted_tool_call(
        ctx,
        tc=tc,
        registered=None,
        executor_id="exec-active",
        failed_connection=ctx.executor_connection,
        original_result=_bridge_failure_result(
            delivery_state="accepted_unknown", instance_id="instance-a"
        ),
        deadline=asyncio.get_running_loop().time() + 0.005,
    )

    assert state == "unresolved"
    assert result is None
    assert reason == "deadline_exceeded"


@pytest.mark.asyncio
async def test_reconcile_min_window_queries_accepted_call_after_expired_budget(
    loop_with_pool,
) -> None:
    """An expired recovery budget must not skip the outcome query entirely."""

    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import (
        _ctx,
        _prepare_reconcile_loop,
        _ReconnectedConnection,
        _target,
        _toolcall,
    )

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")
    reconnected = _ReconnectedConnection(
        instance_id="instance-a",
        reports=[
            {
                "state": "terminal",
                "result": {"call_id": "call-1", "output": "already done", "is_error": False},
            }
        ],
    )
    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=reconnected)
    ws_provider.wait_for_connection = AsyncMock(return_value=reconnected)
    loop_with_pool.providers.executor.websocket = ws_provider

    tc = _toolcall("bash", {"command": "mv a b"})
    state, result, reason = await loop_with_pool._reconcile_accepted_tool_call(
        ctx,
        tc=tc,
        registered=ctx.tool_registry.get("bash"),
        executor_id="exec-active",
        failed_connection=ctx.executor_connection,
        original_result=_bridge_failure_result(
            delivery_state="accepted_unknown", instance_id="instance-a"
        ),
        # Recovery budget already spent: reconciliation still gets its
        # guaranteed minimal window instead of returning deadline_exceeded.
        deadline=asyncio.get_running_loop().time() - 1.0,
    )

    assert state == "terminal"
    assert reason == "recovered"
    assert result is not None
    assert result.output == "already done"
    assert reconnected.fetched == ["call-1"]


@pytest.mark.asyncio
async def test_reconcile_isolated_fetch_hiccups_do_not_accumulate(
    loop_with_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetch failures separated by authoritative reports must not add up."""

    import cognis.core.agent_loop as agent_loop_module
    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import (
        _ctx,
        _prepare_reconcile_loop,
        _target,
        _toolcall,
    )

    monkeypatch.setattr(agent_loop_module, "_TOOL_RECONCILE_POLL_SECONDS", 0.001)
    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    _prepare_reconcile_loop(loop_with_pool, ctx, stale_instance="instance-a")

    class _FlakyConnection:
        """Alternates one fetch failure with one authoritative ACTIVE report."""

        executor_instance_id = "instance-a"

        def __init__(self) -> None:
            self.fetches = 0

        async def fetch_tool_result(self, call_id: str, **_kwargs: Any) -> dict[str, Any]:
            del call_id
            self.fetches += 1
            # More isolated failures in total than the consecutive-failure cap.
            if self.fetches in (2, 4, 6, 8):
                raise ConnectionError("transient fetch hiccup")
            if self.fetches < 10:
                return {"executor_instance_id": "instance-a", "state": "active"}
            return {
                "executor_instance_id": "instance-a",
                "state": "terminal",
                "result": {"call_id": "call-1", "output": "survived hiccups", "is_error": False},
            }

    flaky = _FlakyConnection()
    ws_provider = MagicMock()
    ws_provider.get_ready_connection = MagicMock(return_value=flaky)
    ws_provider.wait_for_connection = AsyncMock(return_value=flaky)
    loop_with_pool.providers.executor.websocket = ws_provider

    state, result, reason = await loop_with_pool._reconcile_accepted_tool_call(
        ctx,
        tc=_toolcall("bash", {"command": "sleep 600"}),
        registered=ctx.tool_registry.get("bash"),
        executor_id="exec-active",
        failed_connection=ctx.executor_connection,
        original_result=_bridge_failure_result(
            delivery_state="accepted_unknown", instance_id="instance-a"
        ),
        deadline=asyncio.get_running_loop().time() + 30.0,
    )

    assert state == "terminal"
    assert reason == "recovered"
    assert result is not None
    assert result.output == "survived hiccups"


# ---------------------------------------------------------------------------
# Ledger recording: no path may convert uncertainty into confirmed execution
# ---------------------------------------------------------------------------


def test_finalize_ledger_recording_keeps_ambiguity_uncertain(loop_with_pool) -> None:
    """The finalize fast path must not record an ambiguous result as executed."""

    from cognis.core.agent_loop import _record_tool_outcome_in_same_turn_ledger
    from cognis.core.executor_pool import ExecutorPool
    from cognis.models.tool import ToolResult
    from tests.unit.test_target_executor_routing import _ctx, _target, _toolcall

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    tc = _toolcall("bash", {"command": "mv a b"})

    ambiguous = ToolResult(
        output="ambiguous",
        is_error=True,
        metadata={"code": "tool_outcome_ambiguous", "uncertain": True},
    )
    _record_tool_outcome_in_same_turn_ledger(ctx, tc, ambiguous)
    assert not ctx.same_turn_tool_call_ledger.already_executed(tc.name, tc.arguments)
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome(tc.name, tc.arguments)

    # Plain errors record nothing; successes record executed and supersede.
    failed = ToolResult(output="boom", is_error=True, metadata={"code": "tool_failed"})
    _record_tool_outcome_in_same_turn_ledger(ctx, tc, failed)
    assert not ctx.same_turn_tool_call_ledger.already_executed(tc.name, tc.arguments)

    succeeded = ToolResult(output="ok", is_error=False)
    _record_tool_outcome_in_same_turn_ledger(ctx, tc, succeeded)
    assert ctx.same_turn_tool_call_ledger.already_executed(tc.name, tc.arguments)
    assert not ctx.same_turn_tool_call_ledger.uncertain_outcome(tc.name, tc.arguments)


def test_persisted_event_seeding_keeps_ambiguity_uncertain(loop_with_pool) -> None:
    """Persisted ambiguous tool results must reconstruct as uncertain."""

    from types import SimpleNamespace

    from cognis.core.agent_loop import _record_tool_call_ledger_events
    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import _ctx, _target

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ctx.tool_call_ledger_candidates = {}
    events = [
        SimpleNamespace(
            type="tool_call",
            data={
                "call_id": "call-amb",
                "name": "bash",
                "arguments": {"command": "mv a b"},
            },
        ),
        SimpleNamespace(
            type="tool_result",
            data={
                "call_id": "call-amb",
                "is_error": True,
                "ambiguity": {"tool_name": "bash"},
            },
        ),
    ]

    _record_tool_call_ledger_events(ctx, events)

    assert not ctx.same_turn_tool_call_ledger.already_executed("bash", {"command": "mv a b"})
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("bash", {"command": "mv a b"})


def test_persisted_restart_recovery_marker_seeds_uncertainty(loop_with_pool) -> None:
    """Startup-recovery results ("uncertain": true, no "ambiguity") also count."""

    from types import SimpleNamespace

    from cognis.core.agent_loop import _record_tool_call_ledger_events
    from cognis.core.executor_pool import ExecutorPool
    from tests.unit.test_target_executor_routing import _ctx, _target

    pool = ExecutorPool(primary=[_target("exec-active")])
    ctx = _ctx(pool, active_executor_id="exec-active")
    ctx.tool_call_ledger_candidates = {}
    events = [
        SimpleNamespace(
            type="tool_call",
            data={
                "call_id": "call-recovered",
                "name": "bash",
                "arguments": {"command": "mv a b"},
            },
        ),
        SimpleNamespace(
            type="tool_result",
            data={
                "call_id": "call-recovered",
                "is_error": True,
                "recovery": True,
                "uncertain": True,
            },
        ),
    ]

    _record_tool_call_ledger_events(ctx, events)

    assert not ctx.same_turn_tool_call_ledger.already_executed("bash", {"command": "mv a b"})
    assert ctx.same_turn_tool_call_ledger.uncertain_outcome("bash", {"command": "mv a b"})

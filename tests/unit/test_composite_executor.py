"""Unit tests for CompositeExecutorProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.models.tool import ExecutorCapabilities, ExecutorConfig, ExecutorHandle
from cognis.providers.executor.composite import CompositeExecutorProvider
from cognis.tools.executor.lsp.runtime import LSPStatusConfig, LSPStatusReport, LSPStatusTotals


def _make_mock_provider(executor_type: str = "in_process") -> MagicMock:
    """Create a mock executor sub-provider."""
    provider = MagicMock()
    provider.spawn = AsyncMock(
        return_value=ExecutorHandle(
            executor_id="test-exec",
            executor_type=executor_type,
            capabilities=ExecutorCapabilities(tools=["bash"]),
        )
    )
    provider.get_executor = AsyncMock(return_value=MagicMock())
    provider.cancel = AsyncMock()
    provider.list_active = AsyncMock(return_value=[])
    provider.cleanup = AsyncMock()
    provider.health = AsyncMock(return_value=MagicMock(status="healthy", details={}))
    return provider


@pytest.mark.asyncio
async def test_routes_in_process_spawn() -> None:
    """spawn routes to in_process provider by default."""
    ip = _make_mock_provider("in_process")
    ws = _make_mock_provider("websocket")
    sp = _make_mock_provider("subprocess")

    composite = CompositeExecutorProvider(ip, ws, sp)
    config = ExecutorConfig(executor_id="test", metadata={"executor_type": "in_process"})
    handle = await composite.spawn(config)

    ip.spawn.assert_called_once_with(config)
    ws.spawn.assert_not_called()
    sp.spawn.assert_not_called()
    assert handle.executor_type == "in_process"


@pytest.mark.asyncio
async def test_routes_websocket_spawn() -> None:
    """spawn routes to websocket provider when executor_type is websocket."""
    ip = _make_mock_provider("in_process")
    ws = _make_mock_provider("websocket")
    sp = _make_mock_provider("subprocess")

    composite = CompositeExecutorProvider(ip, ws, sp)
    config = ExecutorConfig(executor_id="test", metadata={"executor_type": "websocket"})
    await composite.spawn(config)

    ws.spawn.assert_called_once_with(config)
    ip.spawn.assert_not_called()


@pytest.mark.asyncio
async def test_routes_subprocess_spawn() -> None:
    """spawn routes to subprocess provider when executor_type is subprocess."""
    ip = _make_mock_provider("in_process")
    ws = _make_mock_provider("websocket")
    sp = _make_mock_provider("subprocess")

    composite = CompositeExecutorProvider(ip, ws, sp)
    config = ExecutorConfig(executor_id="test", metadata={"executor_type": "subprocess"})
    await composite.spawn(config)

    sp.spawn.assert_called_once_with(config)
    ip.spawn.assert_not_called()


@pytest.mark.asyncio
async def test_get_executor_routes_by_tracked_type() -> None:
    """get_executor uses the tracked type from spawn."""
    ip = _make_mock_provider("in_process")
    ws = _make_mock_provider("websocket")
    sp = _make_mock_provider("subprocess")

    composite = CompositeExecutorProvider(ip, ws, sp)
    config = ExecutorConfig(executor_id="test", metadata={"executor_type": "websocket"})
    handle = await composite.spawn(config)

    await composite.get_executor(handle)
    ws.get_executor.assert_called_once_with(handle)
    ip.get_executor.assert_not_called()


@pytest.mark.asyncio
async def test_list_active_merges_all_providers() -> None:
    """list_active returns handles from all sub-providers."""
    ip = _make_mock_provider()
    ws = _make_mock_provider()
    sp = _make_mock_provider()

    ip.list_active = AsyncMock(
        return_value=[
            ExecutorHandle(executor_id="ip-1", executor_type="in_process"),
        ]
    )
    ws.list_active = AsyncMock(
        return_value=[
            ExecutorHandle(executor_id="ws-1", executor_type="websocket"),
        ]
    )
    sp.list_active = AsyncMock(return_value=[])

    composite = CompositeExecutorProvider(ip, ws, sp)
    active = await composite.list_active()
    assert len(active) == 2
    ids = {h.executor_id for h in active}
    assert ids == {"ip-1", "ws-1"}


@pytest.mark.asyncio
async def test_cleanup_cleans_all_providers() -> None:
    """cleanup calls cleanup on all sub-providers."""
    ip = _make_mock_provider()
    ws = _make_mock_provider()
    sp = _make_mock_provider()

    composite = CompositeExecutorProvider(ip, ws, sp)
    await composite.cleanup()

    ip.cleanup.assert_called_once()
    ws.cleanup.assert_called_once()
    sp.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_health_aggregates() -> None:
    """health returns aggregated status from all sub-providers."""
    ip = _make_mock_provider()
    ws = _make_mock_provider()
    sp = _make_mock_provider()

    composite = CompositeExecutorProvider(ip, ws, sp)
    health = await composite.health()
    assert health.name == "executor"
    assert health.status == "healthy"


@pytest.mark.asyncio
async def test_get_lsp_statuses_delegates_to_in_process_when_no_session_factory() -> None:
    """get_lsp_statuses includes in-process statuses."""
    ip = _make_mock_provider()
    ws = _make_mock_provider()
    sp = _make_mock_provider()

    ip.get_lsp_statuses = AsyncMock(
        return_value=[
            LSPStatusReport(
                supported=True,
                enabled=True,
                executor_id="manager1",
                executor_type="in_process",
                state="ready",
                config=LSPStatusConfig(
                    enabled=True,
                    auto_install=False,
                    diagnostics_timeout_ms=1000,
                    idle_timeout_seconds=60,
                    max_concurrent_servers=2,
                ),
                totals=LSPStatusTotals(),
            )
        ]
    )

    composite = CompositeExecutorProvider(ip, ws, sp)
    statuses = await composite.get_lsp_statuses()
    assert [status.executor_id for status in statuses] == ["manager1"]
    ip.get_lsp_statuses.assert_called_once()


@pytest.mark.asyncio
async def test_get_lsp_statuses_passes_owner_email() -> None:
    ip = _make_mock_provider()
    ws = _make_mock_provider()
    sp = _make_mock_provider()
    ip.get_lsp_statuses = AsyncMock(return_value=[])

    composite = CompositeExecutorProvider(ip, ws, sp)
    await composite.get_lsp_statuses(owner_email="user@example.com")

    ip.get_lsp_statuses.assert_awaited_once_with(owner_email="user@example.com")


@pytest.mark.asyncio
async def test_unknown_type_falls_back_to_in_process() -> None:
    """Unknown executor_type is rejected explicitly."""
    ip = _make_mock_provider("in_process")
    ws = _make_mock_provider("websocket")
    sp = _make_mock_provider("subprocess")

    composite = CompositeExecutorProvider(ip, ws, sp)
    config = ExecutorConfig(executor_id="test", metadata={"executor_type": "unknown_type"})
    with pytest.raises(ValueError, match="Unknown executor type"):
        await composite.spawn(config)

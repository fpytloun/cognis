"""Unit tests for SubprocessExecutorProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognis.models.tool import ExecutorCapabilities, ExecutorConfig, ExecutorHandle
from cognis.providers.executor.subprocess import SubprocessExecutorProvider
from cognis.providers.executor.websocket import WebSocketExecutorProvider


def _make_auth_provider() -> MagicMock:
    """Create a mock auth provider."""
    auth = MagicMock()
    auth.sign_executor_token = MagicMock(return_value="mock-jwt-token")
    return auth


@pytest.mark.asyncio
async def test_spawn_generates_short_lived_token() -> None:
    """spawn generates a 5-minute JWT for the subprocess."""
    auth = _make_auth_provider()
    ws_provider = WebSocketExecutorProvider()
    provider = SubprocessExecutorProvider(ws_provider, auth, controller_port=8080)

    # We can't actually spawn a subprocess in unit tests, but we can
    # verify the token generation call
    config = ExecutorConfig(executor_id="sub-1")

    # Mock create_subprocess_exec to avoid actually spawning
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdin.write = MagicMock()
    mock_process.stdin.close = MagicMock()
    mock_process.returncode = None

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process

        # Also need to mock the ws_provider.spawn to not actually wait
        async def fake_ws_spawn(cfg: ExecutorConfig) -> ExecutorHandle:
            return ExecutorHandle(
                executor_id="sub-1",
                executor_type="websocket",
                capabilities=ExecutorCapabilities(tools=["bash"]),
            )

        ws_provider.spawn = fake_ws_spawn  # type: ignore[assignment]

        await provider.spawn(config)

    # Verify token was generated with 5-minute TTL
    auth.sign_executor_token.assert_called_once_with("sub-1", ttl_seconds=300)

    # Verify only the token is piped via stdin (not CLI args).
    mock_process.stdin.write.assert_called_once()
    written_data = mock_process.stdin.write.call_args[0][0].decode()
    assert written_data == "mock-jwt-token"

    # Verify the subprocess command does NOT contain the token or secrets
    call_args = mock_exec.call_args
    cli_args = call_args[0]  # positional args
    cli_str = " ".join(str(a) for a in cli_args)
    assert "mock-jwt-token" not in cli_str
    assert "--executor-id" not in cli_str
    assert "--config-json" not in cli_str


@pytest.mark.asyncio
async def test_cancel_terminates_process() -> None:
    """cancel sends SIGTERM and waits for process exit."""
    auth = _make_auth_provider()
    ws_provider = WebSocketExecutorProvider()
    provider = SubprocessExecutorProvider(ws_provider, auth)

    # Create a mock process
    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.terminate = MagicMock()
    mock_process.kill = MagicMock()
    mock_process.wait = AsyncMock(return_value=0)

    provider._processes["exec-1"] = mock_process

    handle = ExecutorHandle(executor_id="exec-1", executor_type="subprocess")
    await provider.cancel(handle)

    mock_process.terminate.assert_called_once()
    mock_process.wait.assert_called()


@pytest.mark.asyncio
async def test_cleanup_terminates_all() -> None:
    """cleanup terminates all subprocess executors."""
    auth = _make_auth_provider()
    ws_provider = WebSocketExecutorProvider()
    provider = SubprocessExecutorProvider(ws_provider, auth)

    mock_p1 = MagicMock()
    mock_p1.returncode = None
    mock_p1.terminate = MagicMock()
    mock_p1.wait = AsyncMock(return_value=0)

    mock_p2 = MagicMock()
    mock_p2.returncode = None
    mock_p2.terminate = MagicMock()
    mock_p2.wait = AsyncMock(return_value=0)

    provider._processes = {"exec-1": mock_p1, "exec-2": mock_p2}

    await provider.cleanup()

    mock_p1.terminate.assert_called_once()
    mock_p2.terminate.assert_called_once()
    assert len(provider._processes) == 0


@pytest.mark.asyncio
async def test_health_reports_alive_count() -> None:
    """health reports the number of alive processes."""
    auth = _make_auth_provider()
    ws_provider = WebSocketExecutorProvider()
    provider = SubprocessExecutorProvider(ws_provider, auth)

    mock_alive = MagicMock()
    mock_alive.returncode = None

    mock_dead = MagicMock()
    mock_dead.returncode = 1

    provider._processes = {"alive": mock_alive, "dead": mock_dead}

    health = await provider.health()
    assert health.details["active_processes"] == 1

"""Focused tests for executor WebSocket authentication and probe isolation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from cognis.api import executor_ws
from cognis.core.executor_policy import ExecutorPolicy


class FakeWebSocket:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.sent: list[dict[str, Any]] = []
        self.close_calls: list[tuple[int, str | None]] = []

    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict[str, Any]:
        return self.message

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_calls.append((code, reason))


class FakeSessionFactory:
    def __call__(self) -> FakeSessionFactory:
        return self

    async def __aenter__(self) -> FakeSessionFactory:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _providers(verify: Any) -> Any:
    return SimpleNamespace(auth=SimpleNamespace(verify_executor_token=verify))


def _row(**overrides: Any) -> Any:
    values = {
        "executor_id": "exec-1",
        "executor_type": "websocket",
        "token_version": 3,
        "status": "active",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_probe_is_authenticated_and_does_not_take_over_live_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWebSocket(
        {
            "jsonrpc": "2.0",
            "method": "executor.probe",
            "params": {"token": "executor-token"},
            "id": "probe-1",
        }
    )
    provider = Mock()
    ownership = Mock()
    channel_manager = Mock()
    local_model_runtime_manager = Mock()
    ws.app = SimpleNamespace(
        state=SimpleNamespace(
            executor_connection_ownership=ownership,
            channel_manager=channel_manager,
            local_model_runtime_manager=local_model_runtime_manager,
        )
    )
    monkeypatch.setattr(
        executor_ws,
        "load_executor_policy",
        AsyncMock(return_value=ExecutorPolicy()),
    )
    monkeypatch.setattr(executor_ws, "get_executor_row", AsyncMock(return_value=_row()))

    await executor_ws.handle_executor_websocket(
        ws,
        provider,
        _providers(
            lambda token: {
                "sub": "exec-1",
                "etv": 3,
            }
        ),
        FakeSessionFactory(),  # type: ignore[arg-type]
    )

    assert ws.sent == [
        {
            "jsonrpc": "2.0",
            "result": {"status": "authenticated", "executor_id": "exec-1"},
            "id": "probe-1",
        }
    ]
    assert ws.close_calls == [(1000, "Probe complete")]
    provider.register_connection.assert_not_called()
    provider.unregister_connection.assert_not_called()
    ownership.takeover_validated.assert_not_called()
    ownership.update_runtime_state.assert_not_called()
    ownership.release.assert_not_called()
    channel_manager.start_executor_channels.assert_not_called()
    channel_manager.stop_executor_channels.assert_not_called()
    local_model_runtime_manager.executor_connected.assert_not_called()
    local_model_runtime_manager.executor_disconnected.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verify", "row", "code", "close_code", "reason"),
    [
        (Mock(side_effect=ValueError("invalid")), _row(), -32000, 4401, "Invalid token"),
        (
            lambda _token: {"sub": "exec-1", "etv": 2},
            _row(),
            -32000,
            4401,
            "Invalid token",
        ),
        (
            lambda _token: {"sub": "exec-1", "etv": 3, "exp": 1},
            _row(),
            -32000,
            4401,
            "Invalid token",
        ),
        (
            lambda _token: {"sub": "exec-1", "etv": 3},
            None,
            -32004,
            4404,
            "Executor not found",
        ),
        (
            lambda _token: {"sub": "exec-1", "etv": 3},
            _row(status="inactive"),
            -32005,
            4403,
            "Executor inactive",
        ),
        (
            lambda _token: {"sub": "exec-1", "etv": 3},
            _row(),
            -32006,
            4403,
            "Executor type disabled",
        ),
    ],
)
async def test_probe_returns_typed_validation_errors_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    verify: Any,
    row: Any,
    code: int,
    close_code: int,
    reason: str,
) -> None:
    ws = FakeWebSocket(
        {
            "jsonrpc": "2.0",
            "method": "executor.probe",
            "params": {"token": "executor-token"},
            "id": "probe-2",
        }
    )
    provider = Mock()
    monkeypatch.setattr(
        executor_ws,
        "load_executor_policy",
        AsyncMock(return_value=ExecutorPolicy()),
    )
    monkeypatch.setattr(executor_ws, "is_executor_type_allowed", lambda *_args: code != -32006)
    monkeypatch.setattr(executor_ws, "get_executor_row", AsyncMock(return_value=row))

    await executor_ws.handle_executor_websocket(
        ws,
        provider,
        _providers(verify),
        FakeSessionFactory(),  # type: ignore[arg-type]
    )

    assert ws.sent[0]["error"]["code"] == code
    assert ws.close_calls == [(close_code, reason)]
    provider.register_connection.assert_not_called()
    provider.unregister_connection.assert_not_called()

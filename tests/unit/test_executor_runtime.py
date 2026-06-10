"""Unit tests for websocket executor runtime reconciliation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cognis.api import executor_runtime
from cognis.api.executor_ws import _resolve_executor_mcp_payload
from cognis.core.mcp_oauth import MCPOAuthError


class _RuntimeSession:
    async def commit(self) -> None:
        return None


class _RuntimeSessionFactory:
    async def __aenter__(self) -> _RuntimeSession:
        return _RuntimeSession()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _app_with_ws_connection(connection: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: _RuntimeSessionFactory(),
            providers=SimpleNamespace(
                executor=SimpleNamespace(
                    websocket=SimpleNamespace(get_connection=lambda _executor_id: connection)
                )
            ),
            tool_classification_queue=None,
        )
    )


def _executor_row(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "executor_id": "remote-1",
        "executor_type": "websocket",
        "status": "active",
        "desired_config_version": 2,
        "applied_config_version": 1,
        "runtime_state": "reconfiguring",
        "runtime_metadata": {},
        "observed_tools": [],
        "labels": {},
        "owner_email": "alice@example.com",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_background_reconcile_failure_marks_executor_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app_with_ws_connection()
    row = _executor_row()
    updates: list[dict[str, object]] = []

    async def _reconcile_executor(*_: object, **__: object) -> bool:
        raise RuntimeError("boom")

    async def _get_executor_row(*_: object, **__: object) -> SimpleNamespace:
        return row

    async def _update_executor_runtime_state(
        _session: object,
        _executor_id: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(row, key, value)
        return row

    monkeypatch.setattr(executor_runtime, "reconcile_executor", _reconcile_executor)
    monkeypatch.setattr(executor_runtime, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(
        executor_runtime,
        "update_executor_runtime_state",
        _update_executor_runtime_state,
    )

    executor_runtime.schedule_executor_reconfigure(app, "remote-1")
    task = app.state.executor_reconcile_tasks["remote-1"]
    await asyncio.wait_for(task, timeout=1)

    assert row.runtime_state == "blocked"
    assert updates[-1]["runtime_state"] == "blocked"
    assert "background reconfigure failed" in row.runtime_metadata["warnings"][0]


@pytest.mark.asyncio
async def test_reconcile_unavailable_executor_leaves_reconfiguring_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app_with_ws_connection(connection=None)
    row = _executor_row()
    updates: list[dict[str, object]] = []

    async def _get_executor_row(*_: object, **__: object) -> SimpleNamespace:
        return row

    async def _update_executor_runtime_state(
        _session: object,
        _executor_id: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(row, key, value)
        return row

    monkeypatch.setattr(executor_runtime, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(
        executor_runtime,
        "update_executor_runtime_state",
        _update_executor_runtime_state,
    )

    ok = await executor_runtime.reconcile_executor(app, "remote-1")

    assert ok is False
    assert row.runtime_state == "stale"
    assert updates[-1]["runtime_state"] == "stale"
    assert "connection is unavailable" in row.runtime_metadata["warnings"][0]


@pytest.mark.asyncio
async def test_executor_mcp_payload_skips_unresolved_oauth_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _executor_row(
        config={"mcp_server_ids": ["mcp-oauth", "mcp-stdio"]},
        owner_email="alice@example.com",
    )
    oauth_server = SimpleNamespace(
        server_id="mcp-oauth",
        name="oauth",
        transport="streamable_http",
        command=None,
        url="https://mcp.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
        status="active",
    )
    stdio_server = SimpleNamespace(
        server_id="mcp-stdio",
        name="stdio",
        transport="stdio",
        command="stdio-server",
        url=None,
        args=[],
        env={},
        headers={},
        auth_config=None,
        timeout_seconds=30,
        status="active",
    )

    async def _get_mcp_server(_session: object, server_id: str, **_: object) -> SimpleNamespace:
        return {"mcp-oauth": oauth_server, "mcp-stdio": stdio_server}[server_id]

    async def _get_setting_value(_session: object, _key: str, default: object) -> object:
        return default

    class _OAuthService:
        async def inject_authorization_header(self, **_: object) -> object:
            raise MCPOAuthError("OAuth metadata response is not valid JSON")

    monkeypatch.setattr("cognis.api.executor_ws.get_mcp_server", _get_mcp_server)
    monkeypatch.setattr("cognis.api.executor_ws.get_setting_value", _get_setting_value)
    providers = SimpleNamespace(
        _session_factory=lambda: _RuntimeSessionFactory(),
        mcp_oauth_service=_OAuthService(),
        secrets=SimpleNamespace(get_secret=None),
    )

    servers, secrets, metadata = await _resolve_executor_mcp_payload(row, providers)

    assert secrets == {}
    assert [server.server_id for server in servers] == ["mcp-stdio"]
    assert metadata["mcp_servers"][0]["server_id"] == "mcp-oauth"
    assert metadata["mcp_servers"][0]["status"] == "authorization_required"
    assert metadata["warnings"] == [
        "MCP server oauth requires OAuth authorization, but authorization metadata could not be resolved."
    ]


def test_merge_runtime_metadata_preserves_controller_and_executor_mcp_diagnostics() -> None:
    merged = executor_runtime._merge_runtime_metadata(
        {
            "mcp_servers": [
                {
                    "server_id": "mcp-oauth",
                    "name": "oauth",
                    "status": "authorization_required",
                }
            ],
            "warnings": ["MCP server oauth requires OAuth authorization."],
        },
        {
            "mcp_servers": [
                {
                    "server_id": "mcp-stdio",
                    "name": "stdio",
                    "status": "ready",
                }
            ],
            "warnings": ["MCP server other failed during initialize."],
        },
    )

    assert [item["server_id"] for item in merged["mcp_servers"]] == [
        "mcp-oauth",
        "mcp-stdio",
    ]
    assert merged["warnings"] == [
        "MCP server oauth requires OAuth authorization.",
        "MCP server other failed during initialize.",
    ]

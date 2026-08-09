"""Unit tests for websocket executor runtime reconciliation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cognis.api import executor_runtime
from cognis.api.executor_ws import _resolve_executor_mcp_payload
from cognis.core.mcp_oauth import MCPOAuthError
from cognis.models.executor_inference import (
    executor_local_inference_config_confirmed,
    resolve_executor_local_inference_config,
)


def test_fast_path_local_inference_requires_matching_generation_flags_and_endpoint() -> None:
    metadata = {
        "local_inference_enabled": True,
        "ollama_runtime": {
            "runtime_type": "ollama",
            "port": 22434,
            "endpoint": "http://127.0.0.1:22434",
            "management_enabled": True,
            "max_concurrent_pulls": 1,
            "disk_headroom_bytes": 5 * 1024**3,
        },
    }
    row = SimpleNamespace(
        config={"ollama_runtime": {"port": 22434}},
        desired_config_version=2,
        applied_config_version=2,
        runtime_state="active",
        runtime_metadata=metadata,
    )

    assert executor_runtime._fast_path_local_inference_enabled(row, metadata) is True
    row.desired_config_version = 3
    assert executor_runtime._fast_path_local_inference_enabled(row, metadata) is False
    row.applied_config_version = 3
    metadata["ollama_runtime"]["endpoint"] = "http://127.0.0.1:11434"
    metadata["ollama_runtime"]["port"] = 11434
    assert executor_runtime._fast_path_local_inference_enabled(row, metadata) is False


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
        "config": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _wait_for_call_count(calls: list[str], expected: int) -> None:
    while len(calls) < expected:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_configure_payload_derives_custom_loopback_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolve_mcp(*_: object) -> tuple[list[object], dict[str, str], dict[str, object]]:
        return [], {}, {}

    async def _resolve_web(*_: object) -> dict[str, object]:
        return {}

    async def _resolve_skills(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(skills=[])

    monkeypatch.setattr("cognis.api.executor_ws._resolve_executor_mcp_payload", _resolve_mcp)
    monkeypatch.setattr("cognis.api.runtime_support._resolve_web_config", _resolve_web)
    monkeypatch.setattr(executor_runtime, "resolve_skills_for_agent", _resolve_skills)
    row = _executor_row(
        config={"ollama_runtime": {"port": 22434}},
        enabled_tools=[],
        enabled_tool_groups=[],
    )

    payload, _ = await executor_runtime._build_configure_payload(
        _app_with_ws_connection(),
        row,
        2,
    )

    assert payload["ollama_runtime"]["port"] == 22434
    assert payload["ollama_runtime"]["endpoint"] == "http://127.0.0.1:22434"
    assert payload["config"]["ollama_runtime"]["port"] == 22434
    assert "endpoint" not in payload["config"]["ollama_runtime"]
    resolved = resolve_executor_local_inference_config(payload["config"])
    assert resolved.ollama_runtime.endpoint == "http://127.0.0.1:22434"


@pytest.mark.asyncio
async def test_legacy_generation_normalizes_confirms_and_is_restart_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_metadata = {
        "local_inference_enabled": True,
        "ollama_runtime": {
            "runtime_type": "ollama",
            "port": 11434,
            "endpoint": "http://127.0.0.1:11434",
            "management_enabled": True,
            "max_concurrent_pulls": 1,
            "disk_headroom_bytes": 5 * 1024**3,
        },
    }
    row = _executor_row(
        desired_config_version=0,
        applied_config_version=None,
        runtime_state="offline",
        runtime_metadata={},
    )
    configure_calls: list[int] = []
    normalization_calls: list[int] = []
    ready_calls: list[object] = []

    class _Connection:
        connected = True

        async def rpc_call(
            self,
            method: str,
            payload: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            assert method == "executor.configure"
            assert timeout == executor_runtime.CONFIGURE_RPC_TIMEOUT_SECONDS
            configure_calls.append(int(payload["config_version"]))
            return {
                "applied_version": 1,
                "runtime_state": "active",
                "observed_tools": [],
                "capabilities": {
                    "inference": True,
                    "local_inference": True,
                    "local_model_runtime": runtime_metadata["ollama_runtime"],
                },
                "runtime_metadata": runtime_metadata,
            }

    connection = _Connection()
    websocket = SimpleNamespace(
        get_connection=lambda _executor_id: connection,
        mark_ready=lambda *_args, **_kwargs: ready_calls.append(_args),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: _RuntimeSessionFactory(),
            providers=SimpleNamespace(
                executor=SimpleNamespace(websocket=websocket),
                mcp_oauth_service=None,
            ),
            tool_classification_queue=None,
        )
    )

    async def _get_executor_row(*_: object, **__: object) -> SimpleNamespace:
        return row

    async def _normalize(
        _session: object,
        _executor_id: str,
        *,
        minimum_version: int = 1,
    ) -> bool:
        normalization_calls.append(minimum_version)
        if row.desired_config_version >= minimum_version:
            return False
        row.desired_config_version = minimum_version
        return True

    async def _update_runtime_state(
        _session: object,
        _executor_id: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        for key, value in kwargs.items():
            setattr(row, key, value)
        return row

    async def _build_payload(
        _app: object,
        _row: object,
        desired_version: int,
    ) -> tuple[dict[str, object], dict[str, object]]:
        return {"config_version": desired_version}, dict(runtime_metadata)

    monkeypatch.setattr(executor_runtime, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(
        executor_runtime,
        "normalize_executor_desired_config_version",
        _normalize,
    )
    monkeypatch.setattr(
        executor_runtime,
        "update_executor_runtime_state",
        _update_runtime_state,
    )
    monkeypatch.setattr(executor_runtime, "_build_configure_payload", _build_payload)

    assert await executor_runtime.reconcile_executor(app, "remote-1") is True
    assert row.desired_config_version == 1
    assert row.applied_config_version == 1
    assert row.runtime_metadata["ollama_runtime"]["port"] == 11434
    assert executor_local_inference_config_confirmed(row) is True

    assert await executor_runtime.reconcile_executor(app, "remote-1") is True
    assert normalization_calls == [1]
    assert configure_calls == [1]
    assert len(ready_calls) == 2

    row.desired_config_version = 2
    row.runtime_state = "reconfiguring"
    assert executor_local_inference_config_confirmed(row) is False


@pytest.mark.asyncio
async def test_legacy_applied_generation_fast_path_persists_desired_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_metadata = {
        "local_inference_enabled": True,
        "ollama_runtime": {
            "runtime_type": "ollama",
            "port": 11434,
            "endpoint": "http://127.0.0.1:11434",
            "management_enabled": True,
            "max_concurrent_pulls": 1,
            "disk_headroom_bytes": 5 * 1024**3,
        },
    }
    row = _executor_row(
        desired_config_version=0,
        applied_config_version=1,
        runtime_state="active",
        runtime_metadata=runtime_metadata,
    )

    class _Connection:
        connected = True

        async def rpc_call(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("legacy applied generation must use the fast path")

    connection = _Connection()
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: _RuntimeSessionFactory(),
            providers=SimpleNamespace(
                executor=SimpleNamespace(
                    websocket=SimpleNamespace(
                        get_connection=lambda _executor_id: connection,
                        mark_ready=lambda *_args, **_kwargs: None,
                    )
                )
            ),
            tool_classification_queue=None,
        )
    )

    async def _get_executor_row(*_: object, **__: object) -> SimpleNamespace:
        return row

    async def _normalize(
        _session: object,
        _executor_id: str,
        *,
        minimum_version: int = 1,
    ) -> bool:
        row.desired_config_version = minimum_version
        return True

    monkeypatch.setattr(executor_runtime, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(
        executor_runtime,
        "normalize_executor_desired_config_version",
        _normalize,
    )

    assert await executor_runtime.reconcile_executor(app, "remote-1") is True
    assert row.desired_config_version == 1
    assert row.applied_config_version == 1
    assert executor_local_inference_config_confirmed(row) is True


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
async def test_schedule_executor_reconfigure_forwards_to_connection_owner() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []

    class _ForwardedConnection:
        async def rpc_call(
            self,
            method: str,
            params: dict[str, str],
            *,
            timeout: float,
        ) -> object:
            calls.append((method, params, timeout))
            return {"ok": True}

    forwarded = _ForwardedConnection()
    websocket = SimpleNamespace(
        get_connection=lambda _executor_id: forwarded,
        get_local_connection=lambda _executor_id: None,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            providers=SimpleNamespace(executor=SimpleNamespace(websocket=websocket)),
        )
    )

    executor_runtime.schedule_executor_reconfigure(app, "remote-1")
    task = app.state.executor_reconcile_tasks["remote-1"]
    await asyncio.wait_for(task, timeout=1)

    assert calls == [
        (
            "executor.reconcile",
            {"executor_id": "remote-1"},
            executor_runtime.CONFIGURE_RPC_TIMEOUT_SECONDS,
        )
    ]


@pytest.mark.asyncio
async def test_schedule_executor_reconfigure_replays_update_arriving_during_active_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app_with_ws_connection()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def _reconcile_executor(_app: object, executor_id: str) -> bool:
        calls.append(executor_id)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        return True

    monkeypatch.setattr(executor_runtime, "reconcile_executor", _reconcile_executor)

    executor_runtime.schedule_executor_reconfigure(app, "remote-1")
    first_task = app.state.executor_reconcile_tasks["remote-1"]
    await asyncio.wait_for(first_started.wait(), timeout=1)
    executor_runtime.schedule_executor_reconfigure(app, "remote-1")
    assert app.state.executor_reconcile_pending == {"remote-1"}

    release_first.set()
    await asyncio.wait_for(first_task, timeout=1)
    await asyncio.wait_for(_wait_for_call_count(calls, 2), timeout=1)

    assert calls == ["remote-1", "remote-1"]
    assert app.state.executor_reconcile_pending == set()
    assert app.state.executor_reconcile_tasks == {}


@pytest.mark.asyncio
async def test_cancelled_reconcile_clears_pending_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app_with_ws_connection()
    started = asyncio.Event()
    calls: list[str] = []

    async def _reconcile_executor(_app: object, executor_id: str) -> bool:
        calls.append(executor_id)
        started.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(executor_runtime, "reconcile_executor", _reconcile_executor)

    executor_runtime.schedule_executor_reconfigure(app, "remote-1")
    task = app.state.executor_reconcile_tasks["remote-1"]
    await asyncio.wait_for(started.wait(), timeout=1)
    executor_runtime.schedule_executor_reconfigure(app, "remote-1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert calls == ["remote-1"]
    assert app.state.executor_reconcile_pending == set()
    assert app.state.executor_reconcile_tasks == {}


@pytest.mark.asyncio
async def test_cancelled_failure_persistence_clears_pending_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app_with_ws_connection()
    persistence_started = asyncio.Event()
    calls: list[str] = []

    async def _reconcile_executor(_app: object, executor_id: str) -> bool:
        calls.append(executor_id)
        raise RuntimeError("configure failed")

    async def _mark_reconcile_failed(*_args: object) -> None:
        persistence_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(executor_runtime, "reconcile_executor", _reconcile_executor)
    monkeypatch.setattr(executor_runtime, "_mark_reconcile_failed", _mark_reconcile_failed)

    executor_runtime.schedule_executor_reconfigure(app, "remote-1")
    task = app.state.executor_reconcile_tasks["remote-1"]
    await asyncio.wait_for(persistence_started.wait(), timeout=1)
    executor_runtime.schedule_executor_reconfigure(app, "remote-1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert calls == ["remote-1"]
    assert app.state.executor_reconcile_pending == set()
    assert app.state.executor_reconcile_tasks == {}


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

    seen_server_lookups: list[dict[str, object]] = []

    async def _get_mcp_server(
        _session: object,
        server_id: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        seen_server_lookups.append(kwargs)
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
    assert seen_server_lookups
    assert all(item["owner_email"] == "alice@example.com" for item in seen_server_lookups)
    assert all(item["include_shared"] is True for item in seen_server_lookups)
    assert [server.server_id for server in servers] == ["mcp-stdio"]
    assert metadata["mcp_servers"][0]["server_id"] == "mcp-oauth"
    assert metadata["mcp_servers"][0]["status"] == "authorization_required"
    assert metadata["warnings"] == [
        "MCP server oauth requires OAuth authorization, but authorization metadata could not be resolved."
    ]


@pytest.mark.asyncio
async def test_executor_mcp_payload_propagates_runtime_authorization_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = {
        "error": "insufficient_scope",
        "scope": "tools.write tools.read",
        "resource_metadata": "https://mcp.example/.well-known/oauth-protected-resource/mcp",
    }
    row = _executor_row(
        config={"mcp_server_ids": ["mcp-oauth"]},
        owner_email="alice@example.com",
        runtime_metadata={
            "mcp_servers": [
                {
                    "server_id": "mcp-oauth",
                    "status": "authorization_required",
                    "authorization_required": True,
                },
                {
                    "server_id": "mcp-oauth",
                    "status": "failed",
                    "authorization_required": True,
                    "authorization_challenge": challenge,
                },
            ]
        },
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
    seen_challenges: list[dict[str, str] | None] = []

    async def _get_mcp_server(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return oauth_server

    async def _get_setting_value(_session: object, _key: str, default: object) -> object:
        return default

    class _OAuthService:
        async def inject_authorization_header(self, **kwargs: object) -> object:
            seen_challenges.append(kwargs.get("authorization_challenge"))
            return SimpleNamespace(
                authorization_required=True,
                reason="authorization_required",
                transaction_id="tx-1",
                authorization_url="https://issuer.example/authorize",
                flow="authorization_code",
                verification_uri=None,
                verification_uri_complete=None,
                user_code=None,
                callback_mode="controller_public",
                oauth_executor_id=None,
                oauth_executor_name=None,
                redirect_uri="https://cognis.example/callback",
                instructions=None,
                scopes=["tools.write", "tools.read"],
                resource="https://mcp.example/mcp",
            )

    monkeypatch.setattr("cognis.api.executor_ws.get_mcp_server", _get_mcp_server)
    monkeypatch.setattr("cognis.api.executor_ws.get_setting_value", _get_setting_value)
    providers = SimpleNamespace(
        _session_factory=lambda: _RuntimeSessionFactory(),
        mcp_oauth_service=_OAuthService(),
        secrets=SimpleNamespace(get_secret=None),
    )

    servers, secrets, metadata = await _resolve_executor_mcp_payload(row, providers)

    assert servers == []
    assert secrets == {}
    assert seen_challenges == [challenge]
    assert metadata["mcp_servers"][0]["scopes"] == ["tools.write", "tools.read"]
    assert metadata["mcp_servers"][0]["resource"] == "https://mcp.example/mcp"


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


def test_merge_runtime_metadata_preserves_ready_platform_when_result_omits_it() -> None:
    merged = executor_runtime._merge_runtime_metadata(
        {
            "platform": {"os": "darwin", "arch": "arm64", "python": "3.12.10"},
            "mcp_servers": [],
            "warnings": [],
        },
        {"runtime_state": "active"},
    )

    assert merged["platform"] == {
        "os": "darwin",
        "arch": "arm64",
        "python": "3.12.10",
    }


def test_reported_runtime_metadata_is_allowlisted_and_bounded() -> None:
    reported = {
        "platform": {
            "os": "darwin",
            "arch": "arm64",
            "python": "3.12.10",
            "private": "discard",
        },
        "environment": {
            "home": "/Users/alice",
            "hostname": "mac",
            "token": "discard",
        },
        "warnings": ["warning"],
        "resource_snapshot": {
            "observed_at": datetime.now(UTC).isoformat(),
            "private_path": "/Users/alice/.ollama",
        },
        "resource_snapshot_received_at": "2099-01-01T00:00:00Z",
        "private": {"secret": "discard"},
    }
    sanitized = executor_runtime._sanitize_reported_runtime_metadata(reported)

    assert sanitized == {
        "platform": {
            "os": "darwin",
            "arch": "arm64",
            "python": "3.12.10",
        },
        "environment": {
            "home": "/Users/alice",
            "hostname": "mac",
        },
        "warnings": ["warning"],
    }
    received_at = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    configured = executor_runtime._sanitize_configure_result_runtime_metadata(
        reported,
        received_at=received_at,
    )
    assert configured["resource_snapshot_received_at"] == received_at.isoformat()
    assert "private_path" not in configured["resource_snapshot"]


def test_configure_sanitization_preserves_lsp_and_oauth_runtime_contracts() -> None:
    configured = executor_runtime._sanitize_configure_result_runtime_metadata(
        {
            "configure_capabilities": ["mcp_runtime_status_v1", "lsp_status_v1"],
            "mcp_servers": [
                {
                    "server_id": "oauth-server",
                    "name": "oauth",
                    "status": "failed",
                    "phase": "initialize",
                    "authorization_required": True,
                    "auth_error": "authorization_required",
                    "status_code": 401,
                    "www_authenticate": "Bearer realm=example",
                    "authorization_challenge": {
                        "realm": "example",
                        "resource_metadata": "https://mcp.example/resource",
                    },
                    "private": "discard",
                }
            ],
        },
        received_at=datetime(2026, 7, 13, 10, 0, tzinfo=UTC),
    )
    merged = executor_runtime._merge_runtime_metadata(
        {"mcp_servers": [], "warnings": []},
        configured,
    )

    assert "lsp_status_v1" in merged["configure_capabilities"]
    assert executor_runtime._authorization_failed_mcp_server_ids(merged) == ["oauth-server"]
    status = merged["mcp_servers"][0]
    assert status["authorization_required"] is True
    assert status["authorization_challenge"]["realm"] == "example"
    assert "private" not in status


@pytest.mark.asyncio
async def test_resource_snapshot_persistence_ignores_duplicates_stale_and_rapid_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime.now(UTC)
    row = _executor_row(
        runtime_metadata={
            "resource_snapshot": {
                "observed_at": observed_at.isoformat(),
                "os": "linux",
            },
            "resource_snapshot_received_at": observed_at.isoformat(),
        }
    )
    connection = object()
    app = _app_with_ws_connection(connection=connection)
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

    duplicate = await executor_runtime.persist_executor_resource_snapshot(
        app,
        "remote-1",
        {"observed_at": observed_at.isoformat(), "os": "linux"},
        connection=connection,
    )
    older = await executor_runtime.persist_executor_resource_snapshot(
        app,
        "remote-1",
        {
            "observed_at": (observed_at - timedelta(seconds=30)).isoformat(),
            "os": "linux",
        },
        connection=connection,
    )
    row.runtime_metadata["resource_snapshot_received_at"] = (
        observed_at - timedelta(seconds=31)
    ).isoformat()
    accepted_after_interval = await executor_runtime.persist_executor_resource_snapshot(
        app,
        "remote-1",
        {
            "observed_at": (observed_at + timedelta(seconds=30)).isoformat(),
            "os": "linux",
            "runtime": {"active_calls": 1},
        },
        connection=connection,
    )

    assert duplicate is False
    assert older is False
    assert accepted_after_interval is True
    assert len(updates) == 1
    assert updates[0]["runtime_metadata"]["resource_snapshot"]["runtime"]["active_calls"] == 1
    assert updates[0]["last_observed_at"] > observed_at


def test_authorization_failed_mcp_server_ids_filters_runtime_statuses() -> None:
    assert executor_runtime._authorization_failed_mcp_server_ids(
        {
            "mcp_servers": [
                {
                    "server_id": "rohlik",
                    "status": "failed",
                    "authorization_required": True,
                    "status_code": 401,
                },
                {
                    "server_id": "rohlik",
                    "status": "failed",
                    "authorization_required": True,
                    "status_code": 401,
                },
                {
                    "server_id": "transient",
                    "status": "failed",
                    "authorization_required": False,
                },
                {
                    "server_id": "pending",
                    "status": "authorization_required",
                    "authorization_required": True,
                },
                {
                    "server_id": "application-forbidden",
                    "status": "failed",
                    "authorization_required": True,
                    "status_code": 403,
                },
            ]
        }
    ) == ["rohlik"]


def test_authorization_failed_mcp_server_ids_ignores_controller_skipped_oauth() -> None:
    assert (
        executor_runtime._authorization_failed_mcp_server_ids(
            {
                "mcp_servers": [
                    {
                        "server_id": "rohlik",
                        "status": "authorization_required",
                        "phase": "authorization",
                        "authorization_required": True,
                        "reason": "authorization_required",
                    }
                ]
            }
        )
        == []
    )


@pytest.mark.asyncio
async def test_runtime_auth_failure_forces_refresh_and_requests_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _executor_row(desired_config_version=3, applied_config_version=3)
    app = _app_with_ws_connection(connection=object())
    refreshed: list[tuple[str, str, bool, str]] = []

    class _OAuthService:
        async def refresh_token_for_server_id(
            self,
            *,
            user_email: str,
            server_id: str,
            force: bool,
            reason: str,
        ) -> bool:
            refreshed.append((user_email, server_id, force, reason))
            row.desired_config_version += 1
            row.runtime_state = "reconfiguring"
            return True

    app.state.providers.mcp_oauth_service = _OAuthService()

    await executor_runtime._invalidate_mcp_oauth_tokens_for_runtime_failures(
        app,
        row,
        runtime_metadata={
            "mcp_servers": [
                {
                    "server_id": "rohlik",
                    "status": "failed",
                    "authorization_required": True,
                    "status_code": 401,
                }
            ]
        },
    )

    assert refreshed == [("alice@example.com", "rohlik", True, "mcp_resource_authorization_failed")]
    assert row.desired_config_version == 4
    assert row.runtime_state == "reconfiguring"


@pytest.mark.asyncio
async def test_runtime_skipped_oauth_status_does_not_request_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _executor_row(desired_config_version=3, applied_config_version=3)
    app = _app_with_ws_connection(connection=object())
    calls: list[str] = []

    class _OAuthService:
        async def refresh_token_for_server_id(
            self,
            *,
            user_email: str,
            server_id: str,
            force: bool,
            reason: str,
        ) -> bool:
            calls.append(server_id)
            return True

    app.state.providers.mcp_oauth_service = _OAuthService()

    await executor_runtime._invalidate_mcp_oauth_tokens_for_runtime_failures(
        app,
        row,
        runtime_metadata={
            "mcp_servers": [
                {
                    "server_id": "rohlik",
                    "status": "authorization_required",
                    "phase": "authorization",
                    "authorization_required": True,
                }
            ]
        },
    )

    assert calls == []
    assert row.desired_config_version == 3


@pytest.mark.asyncio
async def test_runtime_auth_recovery_preserves_newer_callback_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _executor_row(desired_config_version=3, applied_config_version=3)
    app = _app_with_ws_connection(connection=object())

    class _OAuthService:
        async def refresh_token_for_server_id(
            self,
            *,
            user_email: str,
            server_id: str,
            force: bool,
            reason: str,
        ) -> bool:
            row.desired_config_version = 11
            row.runtime_state = "reconfiguring"
            return True

    app.state.providers.mcp_oauth_service = _OAuthService()

    await executor_runtime._invalidate_mcp_oauth_tokens_for_runtime_failures(
        app,
        row,
        runtime_metadata={
            "mcp_servers": [
                {
                    "server_id": "rohlik",
                    "status": "failed",
                    "authorization_required": True,
                    "status_code": 401,
                }
            ]
        },
    )

    assert row.desired_config_version == 11
    assert row.runtime_state == "reconfiguring"

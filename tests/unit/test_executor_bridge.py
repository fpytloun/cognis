from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from jose import JWTError

from cognis.api.controller_ws import (
    BRIDGE_MAX_FRAME_BYTES,
    _bounded_frame,
    handle_controller_executor_websocket,
)
from cognis.bootstrap import ensure_data_dir, ensure_jwt_keypair
from cognis.config import load_config
from cognis.core.executor_connection_ownership import ExecutorConnectionOwner
from cognis.models.tool import ExecutorCapabilities, ExecutorHandle, ToolCall, ToolResult
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.executor.forwarding import (
    ForwardedDeliveryError,
    ForwardedExecutorConnection,
    _PendingCall,
)
from cognis.providers.executor.websocket import WebSocketExecutorProvider
from cognis.store.coordination import Lease


class _Directory:
    async def get_reachable(self, owner_id: str) -> Any:
        return SimpleNamespace(
            owner_id=owner_id,
            internal_url="http://peer:8000",
            lifecycle_state="draining",
        )


class _Ownership:
    current = True

    async def is_current(self, _owner: Any) -> bool:
        return self.current


class _LocalConnection:
    def __init__(self, owner: ExecutorConnectionOwner) -> None:
        self.connection_owner = owner
        self.cancelled: list[str] = []
        self.received: list[tuple[str, Any]] = []
        self.replay_metadata: list[tuple[str | None, bool]] = []

    async def rpc_call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
        *,
        on_sent: Any = None,
        stable_call_id: str | None = None,
        replay_safe: bool = False,
    ) -> dict[str, Any]:
        self.received.append((method, params))
        self.replay_metadata.append((stable_call_id, replay_safe))
        assert timeout is not None
        await on_sent()
        if method == "accepted_unknown":
            from cognis.providers.executor.websocket import ExecutorDeliveryError

            raise ExecutorDeliveryError("physical timeout")
        return {"method": method, "params": params}

    async def tool_execute(
        self,
        tool_call: Any,
        timeout_seconds: int | None = None,
        output_chunk_callback: Any = None,
        *,
        on_sent: Any = None,
    ) -> ToolResult:
        del timeout_seconds
        await on_sent()
        await output_chunk_callback(f"{tool_call.call_id}:out", "stdout")
        return ToolResult(output=tool_call.name, is_error=False)

    async def llm_complete_stream(
        self,
        request_id: str,
        messages: list[dict[str, Any]],
        model: str,
        *,
        on_sent: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, model, kwargs
        await on_sent()
        yield {"request_id": request_id, "content": "a"}
        yield {"request_id": request_id, "content": "b", "done": True}

    async def cancel_call(self, call_id: str) -> None:
        self.cancelled.append(call_id)

    async def wait_tool_progress(self, _call_id: str) -> bool:
        return True


def _owner(epoch: int = 7) -> ExecutorConnectionOwner:
    return ExecutorConnectionOwner(
        executor_id="exec-1",
        lease=Lease(
            resource_key="executor_connection:exec-1",
            owner_id="controller-b:boot-b",
            fencing_token=epoch,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )


def _bridge_app(auth: Any, *, epoch: int = 7) -> tuple[FastAPI, _LocalConnection, _Ownership]:
    app = FastAPI()
    owner = _owner(epoch)
    connection = _LocalConnection(owner)
    ownership = _Ownership()
    provider = SimpleNamespace(get_local_connection=lambda executor_id: connection)
    app.state.providers = SimpleNamespace(
        auth=auth,
        executor=SimpleNamespace(websocket=provider),
    )
    app.state.controller_runtime = SimpleNamespace(owner_id=owner.owner_id)
    app.state.controller_directory = _Directory()
    app.state.executor_connection_ownership = ownership

    @app.websocket("/api/internal/executor-bridge")
    async def bridge(ws: WebSocket) -> None:
        await handle_controller_executor_websocket(ws)

    return app, connection, ownership


def _open(
    ws: Any,
    *,
    token: str = "controller-token",
    epoch: int = 7,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    ws.send_json(
        {
            "type": "open",
            "token": token,
            "requester_owner_id": "controller-a:boot-a",
            "executor_id": "exec-1",
            "target_owner_id": "controller-b:boot-b",
            "target_epoch": epoch,
            "protocol_version": 1,
            **({"capabilities": capabilities} if capabilities is not None else {}),
        }
    )
    return ws.receive_json()


def test_bridge_remote_unary_progress_inference_and_callback_isolation() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda token: {
            "typ": "controller",
            "aud": ["cognis-controller"],
            "sub": "controller-a:boot-a",
            "jti": "jti-1",
            "exp": 9999999999,
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"
        ws.send_json(
            {
                "type": "call",
                "call_id": "rpc-1",
                "operation": "rpc",
                "payload": {"method": "browser.status", "params": {"session": "s1"}},
                "timeout_seconds": 2,
            }
        )
        assert ws.receive_json() == {"type": "accepted", "call_id": "rpc-1"}
        assert ws.receive_json()["result"]["method"] == "browser.status"

        for call_id in ("tool-a", "tool-b"):
            ws.send_json(
                {
                    "type": "call",
                    "call_id": call_id,
                    "operation": "tool",
                    "payload": {
                        "tool_call": {
                            "call_id": call_id,
                            "name": "bash",
                            "arguments": {"command": "true"},
                        }
                    },
                }
            )
        seen: dict[str, list[str]] = {"tool-a": [], "tool-b": []}
        completed: set[str] = set()
        while len(completed) < 2:
            frame = ws.receive_json()
            call_id = frame["call_id"]
            if frame["type"] == "event":
                seen[call_id].append(frame["payload"]["chunk"])
            elif frame["type"] == "result":
                completed.add(call_id)
        assert seen == {"tool-a": ["tool-a:out"], "tool-b": ["tool-b:out"]}

        ws.send_json(
            {
                "type": "call",
                "call_id": "llm-1",
                "operation": "inference",
                "payload": {
                    "request_id": "request-1",
                    "messages": [],
                    "model": "model",
                    "kwargs": {},
                },
            }
        )
        chunks: list[str] = []
        while True:
            frame = ws.receive_json()
            if frame["type"] == "event":
                chunks.append(frame["payload"]["content"])
            elif frame["type"] == "result":
                break
        assert chunks == ["a", "b"]
        assert all("executor-token" not in json.dumps(item) for item in connection.received)


def test_bridge_negotiates_and_chunks_oversized_result() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    large_result = {
        "tools": [{"name": f"tool-{index}", "description": "x" * 5000} for index in range(273)]
    }

    async def rpc_call(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        await kwargs["on_sent"]()
        return large_result

    connection.rpc_call = rpc_call  # type: ignore[method-assign]
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        opened = _open(ws, capabilities=["result_chunks_v1"])
        assert opened["protocol_version"] == 1
        assert opened["negotiated_capabilities"] == ["result_chunks_v1"]
        ws.send_json(
            {
                "type": "call",
                "call_id": "large",
                "operation": "rpc",
                "payload": {"method": "tool.list", "params": {}},
            }
        )
        assert ws.receive_json()["type"] == "accepted"
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "result_end":
                break
    start = frames[0]
    chunks = [frame for frame in frames if frame["type"] == "result_chunk"]
    raw = b"".join(base64.b64decode(frame["payload"]) for frame in chunks)
    assert start["chunk_count"] == len(chunks)
    assert start["byte_length"] == len(raw)
    assert start["sha256"] == hashlib.sha256(raw).hexdigest()
    assert json.loads(raw) == large_result
    assert all(
        len(json.dumps(frame, separators=(",", ":")).encode()) < 256 * 1024 for frame in chunks
    )


def test_bridge_oversized_legacy_result_is_bounded_protocol_error() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {"typ": "controller", "sub": "controller-a:boot-a"}
    )
    app, connection, _ownership = _bridge_app(auth)
    large_result = {
        "tools": [{"name": f"tool-{index}", "description": "x" * 5000} for index in range(273)]
    }

    async def rpc_call(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        await kwargs["on_sent"]()
        return large_result

    connection.rpc_call = rpc_call  # type: ignore[method-assign]
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        _open(ws)
        ws.send_json(
            {
                "type": "call",
                "call_id": "large-legacy",
                "operation": "rpc",
                "payload": {"method": "tool.list", "params": {}},
            }
        )
        assert ws.receive_json()["type"] == "accepted"
        error = ws.receive_json()
        assert error["code"] == "protocol_error"
        assert error["delivery_state"] == "terminal"


def test_bridge_old_requester_oversized_error_keeps_socket_usable() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    large_result = {
        "tools": [{"name": f"tool-{index}", "description": "x" * 5000} for index in range(273)]
    }

    async def rpc_call(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        await kwargs["on_sent"]()
        return large_result if method == "custom.large_status" else {"ok": True}

    connection.rpc_call = rpc_call  # type: ignore[method-assign]
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        ws.send_json(
            {
                "type": "open",
                "token": "controller-token",
                "requester_owner_id": "controller-a:boot-a",
                "executor_id": "exec-1",
                "target_owner_id": "controller-b:boot-b",
                "target_epoch": 7,
            }
        )
        assert ws.receive_json()["negotiated_capabilities"] == []
        for call_id, method in (("large", "custom.large_status"), ("small", "tool.list")):
            ws.send_json(
                {
                    "type": "call",
                    "call_id": call_id,
                    "operation": "rpc",
                    "payload": {"method": method, "params": {}},
                }
            )
            assert ws.receive_json()["type"] == "accepted"
            frame = ws.receive_json()
            if call_id == "large":
                assert frame["code"] == "protocol_error"
                assert frame["delivery_state"] == "terminal"
            else:
                assert frame == {"type": "result", "call_id": "small", "result": {"ok": True}}


def test_bridge_small_result_uses_legacy_frame_at_exact_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.api.controller_ws as controller_ws

    result = {"value": "x" * 100}
    frame_size = len(
        json.dumps(
            {"type": "result", "call_id": "exact", "result": result}, separators=(",", ":")
        ).encode()
    )
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {"typ": "controller", "sub": "controller-a:boot-a"}
    )
    app, connection, _ownership = _bridge_app(auth)

    async def rpc_call(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        await kwargs["on_sent"]()
        return result

    connection.rpc_call = rpc_call  # type: ignore[method-assign]
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        _open(ws)
        monkeypatch.setattr(controller_ws, "BRIDGE_MAX_FRAME_BYTES", frame_size)
        ws.send_json(
            {
                "type": "call",
                "call_id": "exact",
                "operation": "rpc",
                "payload": {"method": "tool.list", "params": {}},
            }
        )
        assert ws.receive_json()["type"] == "accepted"
        assert ws.receive_json()["type"] == "result"


def test_bridge_forwards_replay_metadata_without_decorating_rpc_params() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda token: {
            "typ": "controller",
            "aud": ["cognis-controller"],
            "sub": "controller-a:boot-a",
            "jti": "jti-replay",
            "exp": 9999999999,
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"
        ws.send_json(
            {
                "type": "call",
                "call_id": "bridge-transport",
                "operation": "rpc",
                "payload": {
                    "method": "tool.list",
                    "params": {},
                    "stable_call_id": "logical-call",
                    "replay_safe": True,
                },
                "timeout_seconds": 2,
            }
        )
        assert ws.receive_json() == {"type": "accepted", "call_id": "bridge-transport"}
        assert ws.receive_json()["type"] == "result"

    assert connection.received[-1] == ("tool.list", {})
    assert connection.replay_metadata[-1] == ("logical-call", True)


@pytest.mark.parametrize(
    ("claims", "epoch"),
    [
        ({"typ": "service", "sub": "controller-a:boot-a"}, 7),
        ({"typ": "controller", "sub": "other-controller"}, 7),
        (
            {
                "typ": "controller",
                "sub": "controller-a:boot-a",
                "jti": "jti-1",
                "exp": 9999999999,
            },
            8,
        ),
    ],
)
def test_bridge_rejects_auth_and_wrong_owner_epoch(claims: dict[str, Any], epoch: int) -> None:
    def verify(_token: str) -> dict[str, Any]:
        if claims.get("typ") != "controller":
            raise ValueError("invalid controller token type")
        return claims

    auth = SimpleNamespace(verify_controller_jwt=verify)
    app, _connection, _ownership = _bridge_app(auth)
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        frame = _open(ws, epoch=epoch)
        assert frame["type"] == "error"
        assert frame["delivery_state"] == "not_sent"


def test_bridge_bounds_and_owner_change_before_send() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
            "jti": "jti-1",
            "exp": 9999999999,
        }
    )
    app, _connection, ownership = _bridge_app(auth)
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"
        ownership.current = False
        ws.send_json(
            {
                "type": "call",
                "call_id": "stale-1",
                "operation": "rpc",
                "payload": {"method": "tool.list", "params": {}},
            }
        )
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["delivery_state"] == "not_sent"

    with pytest.raises(ValueError, match="maximum size"):
        _bounded_frame({"payload": "x" * (BRIDGE_MAX_FRAME_BYTES + 1)})


def test_bridge_first_frame_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import cognis.api.controller_ws as controller_ws

    monkeypatch.setattr(controller_ws, "BRIDGE_FIRST_FRAME_TIMEOUT_SECONDS", 0.01)
    auth = SimpleNamespace(verify_controller_jwt=lambda _token: {})
    app, _connection, _ownership = _bridge_app(auth)
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        frame = ws.receive_json()
        assert frame["delivery_state"] == "not_sent"
        assert frame["message"] == ""


def test_controller_jwt_exact_contract(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    config = load_config()
    ensure_data_dir(config)
    ensure_jwt_keypair(config)
    provider = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    token = provider.sign_controller_jwt("controller-a:boot-a", 30)
    claims = provider.verify_controller_jwt(token)
    assert claims["typ"] == "controller"
    assert claims["aud"] == ["cognis-controller"]
    assert claims["sub"] == "controller-a:boot-a"
    assert claims["jti"]
    assert claims["exp"] > claims["iat"]
    for audience in (
        "cognis-controller",
        ["cognis-controller", "cognis"],
        ["cognis", "cognis-controller"],
    ):
        audience_value = audience
        monkeypatch.setattr(
            provider,
            "verify_jwt",
            lambda _token, audience, value=audience_value: {
                **claims,
                "aud": value,
            },
        )
        with pytest.raises(JWTError, match="audience"):
            provider.verify_controller_jwt(token)


class _FakeClientBridge:
    def __init__(self, *, old_controller: bool = False) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.old_controller = old_controller

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame["type"] == "open":
            opened = {
                "type": "opened",
                "owner_id": frame["target_owner_id"],
                "epoch": frame["target_epoch"],
            }
            if not self.old_controller:
                opened.update(
                    {
                        "protocol_version": 1,
                        "capabilities": ["result_chunks_v1"],
                        "negotiated_capabilities": ["result_chunks_v1"],
                    }
                )
            self.incoming.put_nowait(json.dumps(opened))
        elif frame["type"] == "call":
            call_id = frame["call_id"]
            if frame["payload"].get("method") == "preaccept_disconnect":
                self.incoming.put_nowait(None)
                return
            self.incoming.put_nowait(json.dumps({"type": "accepted", "call_id": call_id}))
            if frame["payload"].get("method") == "delivery_error":
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "error",
                            "call_id": call_id,
                            "message": "executor restarted",
                            "code": "executor_disconnected",
                            "delivery_state": "accepted_unknown",
                            "executor_id": "exec-1",
                            "generation": 4,
                            "epoch": 9,
                            "same_executor_only": True,
                            "retry_after": 0.25,
                        }
                    )
                )
                return
            if frame["payload"].get("method") == "chunked":
                result_bytes = b'{"value":"chunked"}'
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "result_start",
                            "call_id": call_id,
                            "sequence": 0,
                            "chunk_count": 1,
                            "byte_length": len(result_bytes),
                            "sha256": hashlib.sha256(result_bytes).hexdigest(),
                            "encoding": "base64",
                            "serialization": "canonical-json-v1",
                        }
                    )
                )
                for sequence, payload in enumerate((result_bytes,)):
                    self.incoming.put_nowait(
                        json.dumps(
                            {
                                "type": "result_chunk",
                                "call_id": call_id,
                                "sequence": sequence,
                                "payload": base64.b64encode(payload).decode(),
                            }
                        )
                    )
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "result_end",
                            "call_id": call_id,
                            "chunk_count": 1,
                            "byte_length": len(result_bytes),
                            "sha256": hashlib.sha256(result_bytes).hexdigest(),
                        }
                    )
                )
                return
            if frame["payload"].get("method") == "catalog":
                result = {
                    "tools": [
                        {
                            "name": f"tool-{index}",
                            "description": "x" * 5000,
                        }
                        for index in range(273)
                    ]
                }
                result_bytes = json.dumps(
                    result, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
                chunk_bytes = 180 * 1024
                chunk_count = math.ceil(len(result_bytes) / chunk_bytes)
                digest = hashlib.sha256(result_bytes).hexdigest()
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "result_start",
                            "call_id": call_id,
                            "sequence": 0,
                            "chunk_count": chunk_count,
                            "byte_length": len(result_bytes),
                            "sha256": digest,
                            "encoding": "base64",
                            "serialization": "canonical-json-v1",
                        }
                    )
                )
                for sequence in range(chunk_count):
                    payload = base64.b64encode(
                        result_bytes[sequence * chunk_bytes : (sequence + 1) * chunk_bytes]
                    ).decode()
                    self.incoming.put_nowait(
                        json.dumps(
                            {
                                "type": "result_chunk",
                                "call_id": call_id,
                                "sequence": sequence,
                                "payload": payload,
                            }
                        )
                    )
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "result_end",
                            "call_id": call_id,
                            "chunk_count": chunk_count,
                            "byte_length": len(result_bytes),
                            "sha256": digest,
                        }
                    )
                )
                return
            if frame["payload"].get("method") == "slow" or (
                frame["operation"] == "tool" and frame["payload"]["tool_call"].get("name") == "slow"
            ):
                return
            if frame["operation"] == "tool":
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "event",
                            "call_id": call_id,
                            "event": "tool.progress",
                            "payload": {"chunk": "progress", "stream": "stdout"},
                        }
                    )
                )
                result: dict[str, Any] = {"output": "ok", "is_error": False}
            elif frame["operation"] == "inference":
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "event",
                            "call_id": call_id,
                            "event": "llm.chunk",
                            "payload": {"content": "partial"},
                        }
                    )
                )
                result = {}
            elif frame["payload"].get("method") == "llm.discover_models":
                result = {"models": [{"id": "remote-model"}]}
            else:
                result = {"ok": True}
            self.incoming.put_nowait(
                json.dumps({"type": "result", "call_id": call_id, "result": result})
            )

    async def recv(self) -> str:
        value = await self.incoming.get()
        assert value is not None
        return value

    def __aiter__(self) -> _FakeClientBridge:
        return self

    async def __anext__(self) -> str:
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)


async def _append_chunk(chunks: list[str], chunk: str) -> None:
    chunks.append(chunk)


def _assembly_connection() -> tuple[ForwardedExecutorConnection, _PendingCall]:
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    connection._negotiated_capabilities = {"result_chunks_v1"}
    pending = _PendingCall(asyncio.get_running_loop().create_future(), submitted=True)
    connection._pending["bad"] = pending
    return connection, pending


def _assembly_start(
    raw: bytes,
    *,
    digest: str | None = None,
    byte_length: int | None = None,
    chunk_count: int | None = None,
) -> dict[str, Any]:
    import cognis.providers.executor.forwarding as forwarding

    length = len(raw) if byte_length is None else byte_length
    return {
        "type": "result_start",
        "call_id": "bad",
        "sequence": 0,
        "chunk_count": chunk_count
        if chunk_count is not None
        else math.ceil(length / forwarding.BRIDGE_RESULT_CHUNK_BYTES),
        "byte_length": length,
        "sha256": digest or hashlib.sha256(raw).hexdigest(),
        "encoding": "base64",
        "serialization": "canonical-json-v1",
    }


async def _assert_terminal_protocol_failure(
    connection: ForwardedExecutorConnection,
    pending: _PendingCall,
) -> None:
    await asyncio.sleep(0)
    with pytest.raises(ForwardedDeliveryError):
        pending.future.result()
    assert pending.future.done()
    assert pending.assembly is None
    assert connection._assembly_bytes == 0
    assert connection._active_assemblies == 0
    await asyncio.gather(*tuple(connection._cancel_tasks), return_exceptions=True)
    assert connection._cancel_tasks == set()


@pytest.mark.asyncio
async def test_forwarded_connection_surfaces_and_delivery_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _FakeClientBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _FakeClientBridge:
        return bridge

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", fake_connect)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(inference=True),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda owner_id, ttl: "controller-jwt"),
    )
    assert await connection.rpc_call("tool.list", {}, timeout=1) == {"ok": True}
    with pytest.raises(ForwardedDeliveryError) as delivery_exc:
        await connection.rpc_call("delivery_error", {}, timeout=1)
    assert delivery_exc.value.code == "executor_disconnected"
    assert delivery_exc.value.delivery_state == "accepted_unknown"
    assert delivery_exc.value.executor_id == "exec-1"
    assert delivery_exc.value.generation == 4
    assert delivery_exc.value.epoch == 9
    assert delivery_exc.value.retry_after == 0.25
    assert await connection.rpc_call("chunked", {}, timeout=1) == {"value": "chunked"}
    catalog = await connection.rpc_call("catalog", {}, timeout=2)
    assert len(catalog["tools"]) == 273
    assert await connection.llm_discover_models(
        preset="openai",
        base_url="http://model-api",
    ) == [{"id": "remote-model"}]
    chunks: list[str] = []
    result = await connection.tool_execute(
        ToolCall(call_id="tool-1", name="bash", arguments={}),
        timeout_seconds=1,
        output_chunk_callback=lambda chunk, stream: _append_chunk(chunks, chunk),
    )
    assert result.output == "ok"
    assert chunks == ["progress"]
    inference = [chunk async for chunk in connection.llm_complete_stream("request-1", [], "model")]
    assert inference == [{"content": "partial"}]

    with pytest.raises(ForwardedDeliveryError) as exc:
        await connection.rpc_call("slow", {}, timeout=0.01)
    assert exc.value.delivery_state == "accepted_unknown"
    assert any(frame["type"] == "cancel" for frame in bridge.sent)
    assert all("executor-token" not in json.dumps(frame) for frame in bridge.sent)

    slow_tool = asyncio.create_task(
        connection.tool_execute(
            ToolCall(call_id="slow-tool", name="slow", arguments={}),
            timeout_seconds=30,
        )
    )
    while not any(
        frame.get("type") == "call"
        and frame.get("payload", {}).get("tool_call", {}).get("call_id") == "slow-tool"
        for frame in bridge.sent
    ):
        await asyncio.sleep(0)
    slow_tool.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow_tool
    assert any(frame.get("type") == "cancel" for frame in bridge.sent)

    await connection.close()
    assert bridge.closed is True
    assert connection.connected is False


@pytest.mark.asyncio
async def test_forwarded_new_requester_old_controller_preserves_small_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _FakeClientBridge(old_controller=True)

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _FakeClientBridge:
        return bridge

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", fake_connect)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    assert await connection.rpc_call("tool.list", {}, timeout=1) == {"ok": True}
    assert connection._negotiated_capabilities == set()
    await connection.close()


@pytest.mark.asyncio
async def test_forwarded_old_controller_unnegotiated_oversized_chunks_are_call_local() -> None:
    bridge = _FakeClientBridge(old_controller=True)

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _FakeClientBridge:
        return bridge

    # The fake legacy controller intentionally still emits the catalog chunk frames.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", fake_connect)
    try:
        connection = ForwardedExecutorConnection(
            executor_id="exec-1",
            capabilities=ExecutorCapabilities(),
            owner_id="controller-b:boot-b",
            epoch=7,
            owner_internal_url="http://controller-b:8000",
            requester_owner_id="controller-a:boot-a",
            auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
        )
        with pytest.raises(ForwardedDeliveryError) as exc:
            await connection.rpc_call("catalog", {}, timeout=2)
        assert exc.value.delivery_state == "terminal"
        assert await connection.rpc_call("tool.list", {}, timeout=1) == {"ok": True}
        assert bridge.closed is False
        await connection.close()
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize(
    ("protocol_version", "capabilities", "expected"),
    [
        (1, ["result_chunks_v1"], ["result_chunks_v1"]),
        (1, [], []),
        (0, ["result_chunks_v1"], []),
        (None, None, []),
    ],
)
def test_bridge_negotiation_matrix(
    protocol_version: int | None,
    capabilities: list[str] | None,
    expected: list[str],
) -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, _connection, _ownership = _bridge_app(auth)
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        ws.send_json(
            {
                "type": "open",
                "token": "controller-token",
                "requester_owner_id": "controller-a:boot-a",
                "executor_id": "exec-1",
                "target_owner_id": "controller-b:boot-b",
                "target_epoch": 7,
                **({"protocol_version": protocol_version} if protocol_version is not None else {}),
                **({"capabilities": capabilities} if capabilities is not None else {}),
            }
        )
        opened = ws.receive_json()
        assert opened["negotiated_capabilities"] == expected


@pytest.mark.asyncio
async def test_forwarded_disconnect_after_submission_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _FakeClientBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _FakeClientBridge:
        return bridge

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", fake_connect)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    with pytest.raises(ForwardedDeliveryError) as exc:
        await connection.rpc_call("preaccept_disconnect", {}, timeout=1)
    assert exc.value.delivery_state == "accepted_unknown"


@pytest.mark.asyncio
async def test_forwarded_oversized_outbound_call_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.providers.executor.forwarding as forwarding

    bridge = _FakeClientBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _FakeClientBridge:
        return bridge

    monkeypatch.setattr(forwarding, "connect", fake_connect)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_FRAME_BYTES", 256)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )

    with pytest.raises(ForwardedDeliveryError) as exc:
        await connection.rpc_call("custom.large_request", {"value": "x" * 1024}, timeout=1)

    assert exc.value.delivery_state == "not_sent"
    assert not any(frame.get("type") == "call" for frame in bridge.sent)
    await connection.close()


@pytest.mark.asyncio
async def test_forwarded_close_before_submission_is_not_sent() -> None:
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    pending = _PendingCall(asyncio.get_running_loop().create_future())
    connection._pending["opening"] = pending

    await connection.close()

    with pytest.raises(ForwardedDeliveryError) as exc:
        pending.future.result()
    assert exc.value.delivery_state == "not_sent"


@pytest.mark.asyncio
async def test_forwarded_chunk_state_machine_rejects_out_of_order_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.providers.executor.forwarding as forwarding

    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_MAX_BYTES", 8)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_AGGREGATE_MAX_BYTES", 8)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_CHUNK_BYTES", 2)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_RESULT_CHUNKS", 2)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    pending = _PendingCall(asyncio.get_running_loop().create_future())
    connection._pending["call-1"] = pending
    connection._cancel = lambda _call_id: asyncio.sleep(0)  # type: ignore[method-assign]
    connection._accept_result_start(
        "call-1",
        {
            "sequence": 0,
            "chunk_count": 2,
            "byte_length": 4,
            "sha256": hashlib.sha256(b"{}{}").hexdigest(),
            "encoding": "base64",
            "serialization": "canonical-json-v1",
        },
    )
    connection._accept_result_chunk(
        "call-1",
        {"sequence": 1, "payload": base64.b64encode(b"{}").decode()},
    )
    await asyncio.sleep(0)
    with pytest.raises(ForwardedDeliveryError, match="out-of-order"):
        pending.future.result()
    assert pending.assembly is None
    assert connection._assembly_bytes == 0


@pytest.mark.asyncio
async def test_forwarded_assembly_limits_reserve_before_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.providers.executor.forwarding as forwarding

    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_MAX_BYTES", 16)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_AGGREGATE_MAX_BYTES", 8)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_CHUNK_BYTES", 4)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_RESULT_CHUNKS", 4)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_ACTIVE_ASSEMBLIES", 1)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    first = _PendingCall(asyncio.get_running_loop().create_future())
    second = _PendingCall(asyncio.get_running_loop().create_future())
    connection._pending.update({"first": first, "second": second})
    digest = hashlib.sha256(b"{}").hexdigest()
    connection._accept_result_start(
        "first",
        {
            "sequence": 0,
            "chunk_count": 1,
            "byte_length": 2,
            "sha256": digest,
            "encoding": "base64",
            "serialization": "canonical-json-v1",
        },
    )
    assert not first.future.done()
    assert connection._assembly_bytes == 2
    assert connection._active_assemblies == 1
    connection._accept_result_start(
        "second",
        {
            "sequence": 0,
            "chunk_count": 1,
            "byte_length": 2,
            "sha256": digest,
            "encoding": "base64",
            "serialization": "canonical-json-v1",
        },
    )
    assert second.future.done()
    with pytest.raises(ForwardedDeliveryError):
        second.future.result()
    assert connection._assembly_bytes == 2
    assert connection._active_assemblies == 1
    connection._clear_assembly(first)
    assert connection._assembly_bytes == 0
    assert connection._active_assemblies == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "duplicate_start",
        "duplicate_chunk",
        "out_of_order",
        "out_of_range",
        "chunk_before_start",
        "oversized_encoded",
        "invalid_base64",
        "wrong_raw_length",
        "missing_chunk",
        "end_metadata",
        "bad_digest",
        "invalid_json",
        "noncanonical",
        "non_object",
        "surrogate",
    ],
)
async def test_forwarded_chunk_protocol_failures_are_call_local(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import cognis.providers.executor.forwarding as forwarding

    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_CHUNK_BYTES", 4)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_MAX_BYTES", 16)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_AGGREGATE_MAX_BYTES", 16)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_RESULT_CHUNKS", 4)
    connection, pending = _assembly_connection()
    raw = b'{"a":1}'

    if failure == "chunk_before_start":
        connection._accept_result_chunk(
            "bad", {"sequence": 0, "payload": base64.b64encode(b"{}").decode()}
        )
    else:
        start_raw = raw
        start = _assembly_start(start_raw)
        if failure in {"out_of_order", "missing_chunk"}:
            start = _assembly_start(b"abcdefgh", chunk_count=2)
        if failure == "oversized_encoded":
            connection._accept_result_start("bad", start)
            connection._accept_result_chunk(
                "bad",
                {
                    "sequence": 0,
                    "payload": "A" * (4 * math.ceil(4 / 3) + 1),
                },
            )
        elif failure == "invalid_base64":
            connection._accept_result_start("bad", start)
            connection._accept_result_chunk("bad", {"sequence": 0, "payload": "!!!"})
        elif failure == "wrong_raw_length":
            connection._accept_result_start("bad", start)
            connection._accept_result_chunk(
                "bad", {"sequence": 0, "payload": base64.b64encode(b"abc").decode()}
            )
        elif failure == "duplicate_start":
            connection._accept_result_start("bad", start)
            connection._accept_result_start("bad", start)
        elif failure in {"duplicate_chunk", "out_of_order", "out_of_range"}:
            connection._accept_result_start("bad", start)
            sequence = {"duplicate_chunk": 0, "out_of_order": 1, "out_of_range": 2}[failure]
            connection._accept_result_chunk(
                "bad", {"sequence": sequence, "payload": base64.b64encode(b"abcd").decode()}
            )
            if failure == "duplicate_chunk":
                connection._accept_result_chunk(
                    "bad", {"sequence": 0, "payload": base64.b64encode(b"abcd").decode()}
                )
        elif failure == "missing_chunk":
            connection._accept_result_start("bad", start)
            connection._accept_result_chunk(
                "bad", {"sequence": 0, "payload": base64.b64encode(b"abcd").decode()}
            )
            connection._accept_result_end(
                "bad",
                {
                    "chunk_count": 2,
                    "byte_length": 8,
                    "sha256": start["sha256"],
                },
            )
        else:
            if failure == "end_metadata":
                start = _assembly_start(raw)
            elif failure == "bad_digest":
                start = _assembly_start(raw, digest="0" * 64)
            elif failure == "invalid_json":
                start = _assembly_start(b"not-json")
            elif failure == "noncanonical":
                start = _assembly_start(b'{"b":1,"a":2}')
            elif failure == "non_object":
                start = _assembly_start(b"[]")
            elif failure == "surrogate":
                start = _assembly_start(b'{"x":"\\ud800"}')
            connection._accept_result_start("bad", start)
            payload = start_raw
            if failure == "invalid_json":
                payload = b"not-json"
            elif failure == "noncanonical":
                payload = b'{"b":1,"a":2}'
            elif failure == "non_object":
                payload = b"[]"
            elif failure == "surrogate":
                payload = b'{"x":"\\ud800"}'
            connection._accept_result_chunk(
                "bad",
                {"sequence": 0, "payload": base64.b64encode(payload).decode()},
            )
            connection._accept_result_end(
                "bad",
                {
                    "chunk_count": start["chunk_count"],
                    "byte_length": start["byte_length"] + (1 if failure == "end_metadata" else 0),
                    "sha256": start["sha256"],
                },
            )
    await _assert_terminal_protocol_failure(connection, pending)
    other = _PendingCall(asyncio.get_running_loop().create_future())
    connection._pending["other"] = other
    other.future.set_result({"usable": True})
    assert await other.future == {"usable": True}


@pytest.mark.asyncio
async def test_forwarded_interleaved_assemblies_release_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.providers.executor.forwarding as forwarding

    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_CHUNK_BYTES", 4)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_MAX_BYTES", 16)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_AGGREGATE_MAX_BYTES", 16)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_RESULT_CHUNKS", 4)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="o",
        epoch=1,
        owner_internal_url="http://peer",
        requester_owner_id="r",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    connection._negotiated_capabilities = {"result_chunks_v1"}
    pending_a = _PendingCall(asyncio.get_running_loop().create_future())
    pending_b = _PendingCall(asyncio.get_running_loop().create_future())
    connection._pending.update({"a": pending_a, "b": pending_b})
    raw_a, raw_b = b'{"a":1}', b'{"b":2}'
    for call_id, raw in (("a", raw_a), ("b", raw_b)):
        start = _assembly_start(raw)
        start["call_id"] = call_id
        connection._accept_result_start(call_id, start)
    for call_id, raw in (("a", raw_a), ("b", raw_b)):
        connection._accept_result_chunk(
            call_id, {"sequence": 0, "payload": base64.b64encode(raw[:4]).decode()}
        )
    for call_id, raw in (("a", raw_a), ("b", raw_b)):
        connection._accept_result_chunk(
            call_id, {"sequence": 1, "payload": base64.b64encode(raw[4:]).decode()}
        )
        assembly = connection._pending[call_id].assembly
        assert assembly is not None
        connection._accept_result_end(
            call_id,
            {
                "chunk_count": 2,
                "byte_length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
    assert pending_a.future.result() == {"a": 1}
    assert pending_b.future.result() == {"b": 2}
    assert connection._assembly_bytes == 0
    assert connection._active_assemblies == 0


@pytest.mark.asyncio
async def test_forwarded_unpaired_surrogate_encoding_failure_is_call_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.providers.executor.forwarding as forwarding

    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_CHUNK_BYTES", 64)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_MAX_BYTES", 64)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_AGGREGATE_MAX_BYTES", 64)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_RESULT_CHUNKS", 1)
    connection, pending = _assembly_connection()
    raw = b'{"x":"\\ud800"}'
    connection._accept_result_start("bad", _assembly_start(raw))
    connection._accept_result_chunk(
        "bad", {"sequence": 0, "payload": base64.b64encode(raw).decode()}
    )
    connection._accept_result_end(
        "bad",
        {
            "chunk_count": 1,
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    await _assert_terminal_protocol_failure(connection, pending)


def test_forwarded_raw_chunk_frame_is_below_frame_ceiling() -> None:
    import cognis.providers.executor.forwarding as forwarding

    payload = base64.b64encode(b"x" * forwarding.BRIDGE_RESULT_CHUNK_BYTES).decode()
    frame = json.dumps(
        {"type": "result_chunk", "call_id": "call", "sequence": 0, "payload": payload},
        separators=(",", ":"),
    ).encode()
    assert len(frame) < 256 * 1024
    assert forwarding.BRIDGE_RESULT_CHUNK_BYTES == 180 * 1024


@pytest.mark.asyncio
async def test_forwarded_result_limits_reject_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.providers.executor.forwarding as forwarding

    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_CHUNK_BYTES", 4)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_MAX_BYTES", 8)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_AGGREGATE_MAX_BYTES", 8)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_RESULT_CHUNKS", 2)
    monkeypatch.setattr(forwarding, "BRIDGE_MAX_ACTIVE_ASSEMBLIES", 1)
    connection, first = _assembly_connection()
    connection._accept_result_start(
        "bad", _assembly_start(b"12345678", byte_length=8, chunk_count=2)
    )
    assert first.assembly is not None
    assert connection._assembly_bytes == 8
    second = _PendingCall(asyncio.get_running_loop().create_future())
    connection._pending["second"] = second
    connection._accept_result_start(
        "second", _assembly_start(b"12345678", byte_length=8, chunk_count=2)
    )
    await asyncio.sleep(0)
    with pytest.raises(ForwardedDeliveryError):
        second.future.result()
    assert second.assembly is None
    assert connection._assembly_bytes == 8
    assert connection._active_assemblies == 1
    connection._clear_assembly(first)
    assert connection._assembly_bytes == 0
    assert connection._active_assemblies == 0
    third = _PendingCall(asyncio.get_running_loop().create_future())
    connection._pending["third"] = third
    connection._accept_result_start(
        "third", _assembly_start(b"123456789", byte_length=9, chunk_count=3)
    )
    await asyncio.sleep(0)
    with pytest.raises(ForwardedDeliveryError):
        third.future.result()
    assert third.assembly is None
    assert connection._assembly_bytes == 0
    assert connection._active_assemblies == 0
    await asyncio.gather(*tuple(connection._cancel_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_forwarded_assembly_timeout_and_disconnect_clear_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognis.providers.executor.forwarding as forwarding

    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_CHUNK_BYTES", 4)
    monkeypatch.setattr(forwarding, "BRIDGE_RESULT_ASSEMBLY_TIMEOUT_SECONDS", 0.01)
    connection, pending = _assembly_connection()
    connection._accept_result_start("bad", _assembly_start(b"12345678", chunk_count=2))
    await asyncio.sleep(0.03)
    await _assert_terminal_protocol_failure(connection, pending)

    bridge = _FakeClientBridge()
    connection._ws = bridge
    connection._connected = True
    pending_a = _PendingCall(asyncio.get_running_loop().create_future(), submitted=True)
    pending_b = _PendingCall(asyncio.get_running_loop().create_future(), submitted=True)
    connection._pending.update({"a": pending_a, "b": pending_b})
    connection._accept_result_start("a", _assembly_start(b"12345678", chunk_count=2))
    connection._accept_result_start("b", _assembly_start(b"12345678", chunk_count=2))
    receiver = asyncio.create_task(connection._receive_loop())
    bridge.incoming.put_nowait(None)
    await receiver
    for pending_item in (pending_a, pending_b):
        with pytest.raises(ForwardedDeliveryError) as exc:
            pending_item.future.result()
        assert exc.value.delivery_state == "accepted_unknown"
    assert connection._assembly_bytes == 0
    assert connection._active_assemblies == 0


@pytest.mark.asyncio
async def test_forwarded_caller_timeout_during_active_assembly_cleans_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AssemblyBridge(_FakeClientBridge):
        async def send(self, raw: str) -> None:
            frame = json.loads(raw)
            self.sent.append(frame)
            if frame["type"] == "open":
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "opened",
                            "owner_id": frame["target_owner_id"],
                            "epoch": frame["target_epoch"],
                            "protocol_version": 1,
                            "capabilities": ["result_chunks_v1"],
                            "negotiated_capabilities": ["result_chunks_v1"],
                        }
                    )
                )
            elif frame["type"] == "call":
                self.incoming.put_nowait(
                    json.dumps({"type": "accepted", "call_id": frame["call_id"]})
                )
                raw_result = b"{}"
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "result_start",
                            "call_id": frame["call_id"],
                            "sequence": 0,
                            "chunk_count": 1,
                            "byte_length": 2,
                            "sha256": hashlib.sha256(raw_result).hexdigest(),
                            "encoding": "base64",
                            "serialization": "canonical-json-v1",
                        }
                    )
                )

    bridge = _AssemblyBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _AssemblyBridge:
        return bridge

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", fake_connect)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    with pytest.raises(ForwardedDeliveryError, match="timed out"):
        await connection.rpc_call("slow", {}, timeout=0.01)
    assert connection._pending == {}
    assert connection._assembly_bytes == 0
    assert connection._active_assemblies == 0
    await connection.close()


@pytest.mark.asyncio
async def test_forwarded_close_clears_pending_assemblies_and_cancel_tasks() -> None:
    connection, pending = _assembly_connection()
    connection._tool_bridge_ids["tool"] = "bad"
    connection._accept_result_start("bad", _assembly_start(b"12345678", chunk_count=2))
    connection._schedule_cancel("bad")
    await connection.close()
    assert pending.assembly is None
    assert connection._pending == {}
    assert connection._assembly_bytes == 0
    assert connection._active_assemblies == 0
    assert connection._tool_bridge_ids == {}
    with pytest.raises(ForwardedDeliveryError):
        pending.future.result()


@pytest.mark.asyncio
async def test_forwarded_close_cannot_reopen_for_scheduled_protocol_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_calls = 0

    async def unexpected_connect(*_args: Any, **_kwargs: Any) -> _FakeClientBridge:
        nonlocal connect_calls
        connect_calls += 1
        return _FakeClientBridge()

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", unexpected_connect)
    connection, pending = _assembly_connection()
    bridge = _FakeClientBridge()
    connection._ws = bridge  # type: ignore[assignment]
    connection._connected = True
    connection._protocol_failure("bad", "bad chunk")

    await connection.close()

    assert connect_calls == 0
    assert connection._ws is None
    assert connection.connected is False
    assert connection._receiver is None or connection._receiver.done()
    assert connection._cancel_tasks == set()
    assert bridge.closed is True
    with pytest.raises(ForwardedDeliveryError, match="bad chunk"):
        pending.future.result()


@pytest.mark.asyncio
async def test_forwarded_close_during_open_handshake_cannot_publish_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recv_started = asyncio.Event()
    release_recv = asyncio.Event()

    class BlockedOpenBridge(_FakeClientBridge):
        async def recv(self) -> str:
            recv_started.set()
            await release_recv.wait()
            return json.dumps(
                {
                    "type": "opened",
                    "owner_id": "controller-b:boot-b",
                    "epoch": 7,
                    "protocol_version": 1,
                    "capabilities": ["result_chunks_v1"],
                    "negotiated_capabilities": ["result_chunks_v1"],
                }
            )

    bridge = BlockedOpenBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> BlockedOpenBridge:
        return bridge

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", fake_connect)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    opening = asyncio.create_task(connection._ensure_open())
    await asyncio.wait_for(recv_started.wait(), timeout=1)

    await connection.close()
    release_recv.set()

    with pytest.raises(ForwardedDeliveryError, match="closing"):
        await opening
    assert bridge.closed is True
    assert connection._opening_ws is None
    assert connection._ws is None
    assert connection.connected is False
    assert connection._receiver is None


@pytest.mark.asyncio
async def test_forwarded_open_rejection_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )

    async def reject_open() -> None:
        raise ForwardedDeliveryError("stale owner epoch", "not_sent")

    monkeypatch.setattr(connection, "_ensure_open", reject_open)
    with pytest.raises(ForwardedDeliveryError) as exc:
        await connection.rpc_call("tool.list", {}, timeout=1)
    assert exc.value.delivery_state == "not_sent"


def test_bridge_forwarded_owner_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, _connection, _ownership = _bridge_app(auth)

    async def reconcile(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("cognis.api.executor_runtime.reconcile_executor", reconcile)
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"
        ws.send_json(
            {
                "type": "call",
                "call_id": "reconcile-1",
                "operation": "rpc",
                "payload": {
                    "method": "executor.reconcile",
                    "params": {"executor_id": "exec-1"},
                },
            }
        )
        assert ws.receive_json() == {
            "type": "result",
            "call_id": "reconcile-1",
            "result": {"reconciled": True},
        }


def test_bridge_server_propagates_accepted_unknown() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, _connection, _ownership = _bridge_app(auth)
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"
        ws.send_json(
            {
                "type": "call",
                "call_id": "accepted-unknown-1",
                "operation": "rpc",
                "payload": {"method": "accepted_unknown", "params": {}},
            }
        )
        assert ws.receive_json() == {"type": "accepted", "call_id": "accepted-unknown-1"}
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["call_id"] == "accepted-unknown-1"
        assert frame["delivery_state"] == "accepted_unknown"


@pytest.mark.asyncio
async def test_proxy_refresh_updates_forwarded_capabilities() -> None:
    provider = WebSocketExecutorProvider()
    row = SimpleNamespace(
        status="active",
        runtime_metadata={"capabilities": {"tools": ["read"]}},
        labels={},
        owner_email="owner@example.com",
    )
    lease = SimpleNamespace(
        resource_key="executor_connection:exec-1",
        owner_id="controller-b:boot-b",
        fencing_token=7,
    )

    class _Session:
        bind = None

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalars(self, _query: Any) -> Any:
            return SimpleNamespace(all=lambda: [lease])

        async def get(self, _model: Any, _executor_id: str) -> Any:
            return row

    async def _controller(_owner_id: str) -> SimpleNamespace:
        return SimpleNamespace(internal_url="http://peer:8000")

    provider._cluster_enabled = True
    provider._cluster_session_factory = lambda: _Session()
    provider._cluster_directory = SimpleNamespace(get_ready=_controller)
    provider._cluster_runtime = SimpleNamespace(owner_id="controller-a:boot-a")
    provider._cluster_auth = SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt")

    await provider.refresh_cluster_directory()
    proxy = provider.get_connection("exec-1")
    assert proxy is not None
    assert proxy.capabilities.tools == ["read"]

    row.runtime_metadata = {"capabilities": {"tools": ["read", "write"]}}
    await provider.refresh_cluster_directory()
    assert provider.get_connection("exec-1") is proxy
    assert proxy.capabilities.tools == ["read", "write"]


@pytest.mark.asyncio
async def test_cluster_directory_refresh_is_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = WebSocketExecutorProvider()
    active = 0
    max_active = 0

    async def controlled_refresh() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    monkeypatch.setattr(provider, "_refresh_cluster_directory_once", controlled_refresh)
    await asyncio.gather(
        provider.refresh_cluster_directory(),
        provider.refresh_cluster_directory(),
    )
    assert max_active == 1


@pytest.mark.asyncio
async def test_forwarded_refresh_invalidation_race_never_republishes_closed_proxy() -> None:
    provider = WebSocketExecutorProvider()
    row = SimpleNamespace(
        status="active",
        runtime_metadata={"capabilities": {"tools": ["read"]}},
        labels={},
        owner_email="owner@example.com",
    )
    lease = SimpleNamespace(
        resource_key="executor_connection:exec-1",
        owner_id="controller-b:boot-b",
        fencing_token=7,
    )

    class _Session:
        bind = None

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalars(self, _query: Any) -> Any:
            return SimpleNamespace(all=lambda: [lease])

        async def get(self, _model: Any, _executor_id: str) -> Any:
            return row

    async def ready_controller(_owner_id: str) -> SimpleNamespace:
        return SimpleNamespace(internal_url="http://peer:8000")

    provider._cluster_enabled = True
    provider._cluster_session_factory = lambda: _Session()
    provider._cluster_directory = SimpleNamespace(get_ready=ready_controller)
    provider._cluster_runtime = SimpleNamespace(owner_id="controller-a:boot-a")
    provider._cluster_auth = SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt")
    await provider.refresh_cluster_directory()
    failed = provider.get_connection("exec-1")
    assert isinstance(failed, ForwardedExecutorConnection)

    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    ready_calls = 0

    async def delayed_ready_controller(_owner_id: str) -> SimpleNamespace:
        nonlocal ready_calls
        ready_calls += 1
        if ready_calls == 1:
            refresh_entered.set()
            await release_refresh.wait()
        return SimpleNamespace(internal_url="http://peer:8000")

    provider._cluster_directory = SimpleNamespace(get_ready=delayed_ready_controller)
    refresh = asyncio.create_task(provider.refresh_cluster_directory())
    await refresh_entered.wait()
    invalidation = asyncio.create_task(provider.invalidate_forwarded_connection("exec-1", failed))
    await asyncio.sleep(0)
    assert not invalidation.done()
    release_refresh.set()
    await asyncio.gather(refresh, invalidation)

    assert provider.get_connection("exec-1") is None
    assert failed.closing
    assert failed not in provider._forwarded_connections.values()
    await provider.refresh_cluster_directory()
    replacement = provider.get_connection("exec-1")
    assert isinstance(replacement, ForwardedExecutorConnection)
    assert replacement is not failed
    assert not replacement.closing
    await provider.invalidate_forwarded_connection("exec-1", failed)
    assert provider.get_connection("exec-1") is replacement
    await replacement.close()


@pytest.mark.asyncio
async def test_wait_for_connection_discovers_new_remote_epoch_before_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = WebSocketExecutorProvider()
    old = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://peer:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    new = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=8,
        owner_internal_url="http://peer:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    same_epoch = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id=old.owner_id,
        epoch=old.epoch,
        owner_internal_url="http://peer:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    provider._cluster_enabled = True
    provider._forwarded_by_executor["exec-1"] = old
    provider._forwarded_connections[("exec-1", old.owner_id, old.epoch)] = old
    provider._handles["exec-1"] = ExecutorHandle(
        executor_id="exec-1",
        executor_type="websocket",
        capabilities=ExecutorCapabilities(),
        status="ready",
        metadata={
            "forwarded": True,
            "owner_id": old.owner_id,
            "connection_epoch": old.epoch,
        },
    )
    refresh_calls = 0

    async def publish_delayed_epoch() -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            provider._forwarded_connections = {
                ("exec-1", same_epoch.owner_id, same_epoch.epoch): same_epoch
            }
            provider._forwarded_by_executor["exec-1"] = same_epoch
            return
        provider._forwarded_connections = {("exec-1", new.owner_id, new.epoch): new}
        provider._forwarded_by_executor["exec-1"] = new
        provider._handles["exec-1"].metadata = {
            "forwarded": True,
            "owner_id": new.owner_id,
            "connection_epoch": new.epoch,
        }

    monkeypatch.setattr(provider, "refresh_cluster_directory", publish_delayed_epoch)

    replacement = await provider.wait_for_connection(
        "exec-1",
        timeout=1.0,
        failed_connection=old,
        delivery_state="accepted_unknown",
        failed_owner_id=old.owner_id,
        failed_epoch=old.epoch,
    )

    assert replacement is new
    assert refresh_calls >= 2
    await old.close()
    await same_epoch.close()
    await new.close()


@pytest.mark.asyncio
async def test_wait_for_connection_bounds_stalled_authoritative_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = WebSocketExecutorProvider()
    provider._cluster_enabled = True
    refresh_cancelled = asyncio.Event()

    async def stalled_refresh() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            refresh_cancelled.set()
            raise

    monkeypatch.setattr(provider, "refresh_cluster_directory", stalled_refresh)
    started = asyncio.get_running_loop().time()

    replacement = await provider.wait_for_connection(
        "exec-1",
        timeout=0.02,
        delivery_state="accepted_unknown",
        failed_owner_id="controller-b:boot-b",
        failed_epoch=7,
    )

    assert replacement is None
    assert asyncio.get_running_loop().time() - started < 0.2
    assert refresh_cancelled.is_set()

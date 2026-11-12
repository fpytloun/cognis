from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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
from cognis.models.program import ProgramRuntimeBinding, ProgramToolCall
from cognis.models.tool import ExecutorCapabilities, ExecutorHandle, ToolCall, ToolResult
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.executor.forwarding import (
    ForwardedDeliveryError,
    ForwardedExecutorConnection,
    _PendingCall,
)
from cognis.providers.executor.websocket import (
    WebSocketExecutorConnection,
    WebSocketExecutorProvider,
)
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
        self.executor_instance_id = "executor-instance-a"
        self.cancelled: list[str] = []
        self.received: list[tuple[str, Any]] = []
        self.replay_metadata: list[tuple[str | None, bool]] = []
        self.tool_timeouts: list[int | None] = []

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
        if method == "secret_failure":
            raise RuntimeError("SECRET_SENTINEL_MUST_NOT_BE_LOGGED")
        return {"method": method, "params": params}

    async def list_tools(
        self, *, timeout=None, on_sent=None, stable_call_id=None
    ) -> list[dict[str, Any]]:
        result = await self.rpc_call(
            "tool.list",
            {},
            timeout=timeout,
            on_sent=on_sent,
            stable_call_id=stable_call_id,
            replay_safe=True,
        )
        return result.get("tools", [])

    async def tool_execute(
        self,
        tool_call: Any,
        timeout_seconds: int | None = None,
        output_chunk_callback: Any = None,
        *,
        on_sent: Any = None,
    ) -> ToolResult:
        self.tool_timeouts.append(timeout_seconds)
        await on_sent(self.connection_owner.executor_id, self.executor_instance_id)
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
    detached_tasks: set[asyncio.Task[Any]] = set()

    def track_detached_tool_call(task: asyncio.Task[Any]) -> None:
        detached_tasks.add(task)
        task.add_done_callback(detached_tasks.discard)

    provider = SimpleNamespace(
        get_local_connection=lambda executor_id: connection,
        track_detached_tool_call=track_detached_tool_call,
        detached_tasks=detached_tasks,
    )
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
    protocol_version: int = 1,
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
            "protocol_version": protocol_version,
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


def test_bridge_tool_acceptance_uses_physical_callback_and_preserves_timeout() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, connection, _ownership = _bridge_app(auth)

    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"
        ws.send_json(
            {
                "type": "call",
                "call_id": "long-tool",
                "operation": "tool",
                "payload": {
                    "tool_call": {
                        "call_id": "long-tool",
                        "name": "bash",
                        "arguments": {"command": "true"},
                    }
                },
                "timeout_seconds": 3605,
            }
        )

        assert ws.receive_json() == {"type": "accepted", "call_id": "long-tool"}
        assert ws.receive_json()["type"] == "event"
        assert ws.receive_json()["result"]["is_error"] is False

    assert connection.tool_timeouts == [3605]


def test_bridge_real_physical_connection_acknowledges_and_returns_once() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, stub, _ownership = _bridge_app(auth)

    class PhysicalSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, request: dict[str, Any]) -> None:
            self.sent.append(request)
            physical._pending[request["id"]].set_result(
                {"output": "physical success", "is_error": False}
            )

    socket = PhysicalSocket()
    physical = WebSocketExecutorConnection(
        socket,  # type: ignore[arg-type]
        stub.connection_owner.executor_id,
        ExecutorCapabilities(),
        connection_owner=stub.connection_owner,
    )
    physical.executor_instance_id = stub.executor_instance_id
    app.state.providers.executor.websocket.get_local_connection = lambda _executor_id: physical

    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"
        ws.send_json(
            {
                "type": "call",
                "call_id": "physical-tool",
                "operation": "tool",
                "payload": {
                    "tool_call": {
                        "call_id": "physical-tool",
                        "name": "bash",
                        "arguments": {"command": "true"},
                    }
                },
                "timeout_seconds": 2,
            }
        )
        assert ws.receive_json() == {"type": "accepted", "call_id": "physical-tool"}
        result = ws.receive_json()

    assert result["result"]["output"] == "physical success"
    assert len(socket.sent) == 1


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("mutation", ["executor.configure", "executor.mcp.retry"])
def test_bridge_catalog_uses_owner_cache_and_invalidates_before_mutation(notifications, mutation):
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {"typ": "controller", "sub": "controller-a:boot-a"}
    )
    app, stub, ownership = _bridge_app(auth)

    class PhysicalSocket:
        def __init__(self):
            self.calls = []
            self.catalog = [{"name": "read"}]

        async def send_json(self, request):
            self.calls.append(request["method"])
            physical._pending[request["id"]].set_result(
                {
                    "tools": self.catalog,
                    "catalog_notifications": notifications,
                }
            )

    socket = PhysicalSocket()
    physical = WebSocketExecutorConnection(
        socket,
        stub.connection_owner.executor_id,
        ExecutorCapabilities(),
        connection_owner=stub.connection_owner,
    )
    physical.executor_instance_id = stub.executor_instance_id
    app.state.providers.executor.websocket.get_local_connection = lambda _executor_id: physical

    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"

        def request(method, call_id):
            ws.send_json(
                {
                    "type": "call",
                    "call_id": call_id,
                    "operation": "rpc",
                    "payload": {
                        "method": method,
                        "params": {},
                        "stable_call_id": call_id,
                        "replay_safe": method == "tool.list",
                    },
                    "timeout_seconds": 2,
                }
            )
            frame = ws.receive_json()
            accepted = frame["type"] == "accepted"
            if accepted:
                frame = ws.receive_json()
            return frame, accepted

        first, accepted = request("tool.list", "first-catalog")
        assert accepted and first["result"]["tools"] == [{"name": "read"}]
        second, accepted = request("tool.list", "cached-catalog")
        assert accepted is (not notifications)
        assert second["result"]["tools"] == [{"name": "read"}]
        assert socket.calls == ["tool.list"] * (1 if notifications else 2)

        changed, _ = request(mutation, "configure-catalog")
        assert changed["type"] == "result"
        socket.catalog = [{"name": "write"}]
        refreshed, accepted = request("tool.list", "refreshed-catalog")
        assert accepted
        assert refreshed["result"]["tools"] == [{"name": "write"}]
        assert socket.calls[-2:] == [mutation, "tool.list"]

        # A catalog hit must not bypass the bridge's current ownership gate.
        ownership.current = False
        refused, _ = request("tool.list", "revoked-catalog")
        assert refused["type"] == "error"
        assert socket.calls[-1] == "tool.list"


def test_bridge_reverse_program_rpc_and_status_use_the_live_binding() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, stub, _ownership = _bridge_app(auth)
    physical = WebSocketExecutorConnection(
        SimpleNamespace(),
        stub.connection_owner.executor_id,
        ExecutorCapabilities(),
        connection_owner=stub.connection_owner,
    )
    physical.executor_instance_id = stub.executor_instance_id
    physical._send_rpc_message = AsyncMock()
    physical.send_program_status = AsyncMock(return_value=True)
    app.state.providers.executor.websocket.get_local_connection = lambda _executor_id: physical
    binding = ProgramRuntimeBinding(
        program_id="program-call-1",
        execution_id="program-execution-1",
        scope_id="program-scope-1",
        description="Write an artifact.",
        tool_manifest_digest="program-plan-v1",
        tools=[],
        executor_id=physical.executor_id,
        executor_instance_id=physical.executor_instance_id,
    )
    child = ProgramToolCall(
        program_id=binding.program_id,
        execution_id=binding.execution_id,
        kernel_generation=1,
        sequence=1,
        tool_id="builtin:artifact_write",
        schema_hash="artifact-schema-v1",
        arguments={"filename": "review.txt", "content": "approved"},
    )
    child_params = {
        **child.model_dump(mode="json"),
        "executor_id": binding.executor_id,
        "executor_instance_id": binding.executor_instance_id,
        "scope_id": binding.scope_id,
    }

    async def execute_program(
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: Any = None,
        *,
        on_sent: Any = None,
    ) -> ToolResult:
        del timeout_seconds, output_chunk_callback
        assert tool_call.call_id == binding.program_id
        await on_sent(physical.executor_id, physical.executor_instance_id)
        await physical._answer_program_tool_call("child-call", child_params)
        await asyncio.sleep(0.05)
        return ToolResult(output="program finished")

    physical.tool_execute = execute_program
    status = {
        "program_id": binding.program_id,
        "execution_id": binding.execution_id,
        "scope_id": binding.scope_id,
        "kernel_generation": 1,
        "sequence": 1,
        "status": "approval_waiting",
    }
    result_value = {
        "text": "Artifact saved.",
        "data": {"saved": True},
        "format": "structured",
        "is_error": False,
        "outcome_certainty": "known",
    }
    binding_key = [
        binding.executor_id,
        binding.executor_instance_id,
        binding.scope_id,
        binding.execution_id,
        binding.program_id,
    ]
    with (
        TestClient(app) as client,
        client.websocket_connect("/api/internal/executor-bridge") as ws,
    ):
        opened = _open(
            ws,
            protocol_version=2,
            capabilities=["result_chunks_v1", "program_rpc_v1"],
        )
        assert "program_rpc_v1" in opened["negotiated_capabilities"]
        ws.send_json(
            {
                "type": "call",
                "call_id": "bridge-program-call-1",
                "operation": "tool",
                "payload": {
                    "tool_call": {
                        "call_id": binding.program_id,
                        "name": "python_execute",
                        "arguments": {"description": binding.description, "code": "pass"},
                        "runtime_metadata": {"program_runtime": binding.model_dump(mode="json")},
                    }
                },
                "timeout_seconds": 60,
            }
        )
        assert ws.receive_json() == {
            "type": "accepted",
            "call_id": "bridge-program-call-1",
        }
        request = ws.receive_json()
        assert request["type"] == "program_tool_call"
        assert request["params"] == child_params
        ws.send_json(
            {
                "type": "program_status",
                "call_id": "bridge-program-call-1",
                "params": status,
            }
        )
        ws.send_json(
            {
                "type": "program_tool_call_result",
                "call_id": "bridge-program-call-1",
                "request_id": request["request_id"],
                "binding_key": binding_key,
                "sequence": 1,
                "result": result_value,
            }
        )
        result = ws.receive_json()

    physical.send_program_status.assert_awaited_once_with(status)
    assert result["type"] == "result"
    assert result["result"]["output"] == "program finished"
    sent_value = physical._send_rpc_message.await_args.args[0]["result"]
    assert all(sent_value[key] == value for key, value in result_value.items())


def test_bridge_without_program_rpc_capability_never_dispatches_python() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    binding = ProgramRuntimeBinding(
        program_id="unsupported-program-call",
        execution_id="unsupported-execution",
        scope_id="unsupported-scope",
        description="Run a program.",
        tool_manifest_digest="program-plan-v1",
        tools=[],
        executor_id=connection.connection_owner.executor_id,
        executor_instance_id=connection.executor_instance_id,
    )
    with (
        TestClient(app) as client,
        client.websocket_connect("/api/internal/executor-bridge") as ws,
    ):
        opened = _open(ws, capabilities=["program_rpc_v1"])
        assert "program_rpc_v1" not in opened["negotiated_capabilities"]
        ws.send_json(
            {
                "type": "call",
                "call_id": "bridge-unsupported-program",
                "operation": "tool",
                "payload": {
                    "tool_call": {
                        "call_id": binding.program_id,
                        "name": "python_execute",
                        "arguments": {"description": "Run a program.", "code": "pass"},
                        "runtime_metadata": {"program_runtime": binding.model_dump(mode="json")},
                    }
                },
            }
        )
        response = ws.receive_json()

    assert response["type"] == "error"
    assert response["delivery_state"] == "not_sent"
    assert connection.tool_timeouts == []


@pytest.mark.asyncio
async def test_forwarded_reverse_program_callbacks_are_scoped_and_cancelable() -> None:
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://peer:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(),
    )
    connection.executor_instance_id = "executor-instance-a"
    connection._negotiated_capabilities.add("program_rpc_v1")
    sent: list[dict[str, Any]] = []

    class BridgeSocket:
        async def send(self, raw: str) -> None:
            sent.append(json.loads(raw))

    ws = BridgeSocket()
    connection._ws = ws  # type: ignore[assignment]
    connection._connected = True

    def make_binding(program_id: str) -> ProgramRuntimeBinding:
        return ProgramRuntimeBinding(
            program_id=program_id,
            execution_id=f"execution-{program_id}",
            scope_id=f"scope-{program_id}",
            description="A scoped program.",
            tool_manifest_digest=f"plan-{program_id}",
            tools=[],
            executor_id=connection.executor_id,
            executor_instance_id=connection.executor_instance_id,
        )

    binding_a = make_binding("program-a")
    binding_b = make_binding("program-b")
    result_a = {"text": "A", "is_error": False, "outcome_certainty": "known"}
    result_b = {"text": "B", "is_error": False, "outcome_certainty": "known"}
    connection.register_program_tool_call_handler(binding_a, AsyncMock(return_value=result_a))
    connection.register_program_tool_call_handler(binding_b, AsyncMock(return_value=result_b))

    def params(binding: ProgramRuntimeBinding, sequence: int) -> dict[str, Any]:
        return {
            "program_id": binding.program_id,
            "execution_id": binding.execution_id,
            "scope_id": binding.scope_id,
            "executor_id": binding.executor_id,
            "executor_instance_id": binding.executor_instance_id,
            "kernel_generation": 1,
            "sequence": sequence,
            "arguments": {},
        }

    await connection._answer_program_tool_call(
        ws,
        "bridge-a",
        "request-a",
        params(binding_a, 1),  # type: ignore[arg-type]
    )
    await connection._answer_program_tool_call(
        ws,
        "bridge-b",
        "request-b",
        params(binding_b, 1),  # type: ignore[arg-type]
    )
    assert [frame["result"] for frame in sent] == [result_a, result_b]
    assert sent[0]["binding_key"] != sent[1]["binding_key"]

    unbound = make_binding("program-unbound")
    await connection._answer_program_tool_call(
        ws,
        "bridge-unbound",
        "request-unbound",
        params(unbound, 1),  # type: ignore[arg-type]
    )
    assert sent[-1]["result"]["outcome_certainty"] == "unknown"

    approval_started = asyncio.Event()

    async def waiting_for_approval(_params: dict[str, Any]) -> dict[str, Any]:
        approval_started.set()
        await asyncio.Event().wait()
        return result_a

    binding_pending = make_binding("program-pending")
    connection.register_program_tool_call_handler(binding_pending, waiting_for_approval)
    pending_task = asyncio.create_task(
        connection._answer_program_tool_call(
            ws,
            "bridge-pending",
            "request-pending",
            params(binding_pending, 1),  # type: ignore[arg-type]
        )
    )
    await approval_started.wait()
    pending_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_task
    assert sent[-1] == {
        "type": "program_tool_call_cancel",
        "call_id": "bridge-pending",
        "request_id": "request-pending",
    }


def test_bridge_disconnect_detaches_accepted_tool_without_cancelling() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    release = threading.Event()
    completed = threading.Event()

    async def tool_execute(
        tool_call: Any,
        timeout_seconds: int | None = None,
        output_chunk_callback: Any = None,
        *,
        on_sent: Any = None,
    ) -> ToolResult:
        del tool_call, timeout_seconds, output_chunk_callback
        await on_sent(connection.connection_owner.executor_id, connection.executor_instance_id)
        await asyncio.to_thread(release.wait)
        completed.set()
        return ToolResult(output="finished", is_error=False)

    connection.tool_execute = tool_execute  # type: ignore[method-assign]
    provider = app.state.providers.executor.websocket
    with TestClient(app) as client:
        with client.websocket_connect("/api/internal/executor-bridge") as ws:
            assert _open(ws)["type"] == "opened"
            ws.send_json(
                {
                    "type": "call",
                    "call_id": "detached-tool",
                    "operation": "tool",
                    "payload": {
                        "tool_call": {
                            "call_id": "executor-call",
                            "name": "bash",
                            "arguments": {"command": "true"},
                        }
                    },
                    "timeout_seconds": 2,
                }
            )
            assert ws.receive_json() == {"type": "accepted", "call_id": "detached-tool"}

        for _ in range(100):
            if provider.detached_tasks:
                break
            time.sleep(0.01)
        assert provider.detached_tasks
        assert connection.cancelled == []
        release.set()
        assert completed.wait(timeout=1)
        for _ in range(100):
            if not provider.detached_tasks:
                break
            time.sleep(0.01)
        assert provider.detached_tasks == set()


def test_replacement_bridge_cancels_detached_physical_tool() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda _token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, stub, _ownership = _bridge_app(auth)

    class PhysicalSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.execute_request_id: str | None = None

        async def send_json(self, request: dict[str, Any]) -> None:
            self.sent.append(request)
            if request["method"] == "tool.execute":
                self.execute_request_id = request["id"]
                return
            if request["method"] == "tool.cancel":
                physical._pending[request["id"]].set_result({"cancelled": True})
                assert self.execute_request_id is not None
                physical._pending[self.execute_request_id].set_result(
                    {
                        "output": "Tool execution cancelled.",
                        "is_error": True,
                        "metadata": {"code": "tool_cancelled"},
                    }
                )

    socket = PhysicalSocket()
    physical = WebSocketExecutorConnection(
        socket,  # type: ignore[arg-type]
        stub.connection_owner.executor_id,
        ExecutorCapabilities(),
        connection_owner=stub.connection_owner,
    )
    physical.executor_instance_id = stub.executor_instance_id
    app.state.providers.executor.websocket.get_local_connection = lambda _executor_id: physical
    provider = app.state.providers.executor.websocket

    with TestClient(app) as client:
        with client.websocket_connect("/api/internal/executor-bridge") as original:
            assert _open(original)["type"] == "opened"
            original.send_json(
                {
                    "type": "call",
                    "call_id": "original-bridge-call",
                    "operation": "tool",
                    "payload": {
                        "tool_call": {
                            "call_id": "stable-executor-call",
                            "name": "bash",
                            "arguments": {"command": "sleep 1"},
                        }
                    },
                    "timeout_seconds": 30,
                }
            )
            assert original.receive_json() == {
                "type": "accepted",
                "call_id": "original-bridge-call",
            }

        for _ in range(100):
            if provider.detached_tasks:
                break
            time.sleep(0.01)
        assert provider.detached_tasks

        with client.websocket_connect("/api/internal/executor-bridge") as replacement:
            assert _open(replacement)["type"] == "opened"
            replacement.send_json(
                {
                    "type": "call",
                    "call_id": "replacement-cancel",
                    "operation": "rpc",
                    "payload": {
                        "method": "tool.cancel",
                        "params": {"call_id": "stable-executor-call"},
                    },
                    "timeout_seconds": 2,
                }
            )
            assert replacement.receive_json() == {
                "type": "accepted",
                "call_id": "replacement-cancel",
            }
            assert replacement.receive_json()["result"] == {"cancelled": True}

        for _ in range(100):
            if not provider.detached_tasks:
                break
            time.sleep(0.01)
        assert provider.detached_tasks == set()

    assert [request["method"] for request in socket.sent] == ["tool.execute", "tool.cancel"]


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
        assert opened["protocol_version"] == 2
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
        for call_id, method in (("large", "custom.large_status"), ("small", "custom.small_status")):
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
                "payload": {"method": "custom.exact_status", "params": {}},
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


def test_bridge_bounds_and_owner_change_before_send(caplog: pytest.LogCaptureFixture) -> None:
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

        ownership.current = True
        ws.send_json(
            {
                "type": "call",
                "call_id": "secret-1",
                "operation": "rpc",
                "payload": {"method": "secret_failure", "params": {}},
            }
        )
        frame = ws.receive_json()
        assert frame["type"] == "accepted"
        frame = ws.receive_json()
        assert frame["type"] == "error"

    assert "SECRET_SENTINEL_MUST_NOT_BE_LOGGED" not in caplog.text

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


def test_bridge_capacity_rejection_has_stable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import cognis.api.controller_ws as controller_ws

    monkeypatch.setattr(controller_ws, "BRIDGE_MAX_CALLS", 0)
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
                "call_id": "capacity-1",
                "operation": "tool",
                "payload": {"tool_call": {}},
            }
        )
        frame = ws.receive_json()
        assert frame["delivery_state"] == "not_sent"
        assert frame["code"] == "executor_bridge_capacity"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rpc", "tool", "inference"])
async def test_bridge_cancel_before_task_start_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    import cognis.api.controller_ws as controller_ws

    monkeypatch.setattr(controller_ws, "BRIDGE_MAX_CALLS", 2)
    auth = SimpleNamespace(
        verify_controller_jwt=lambda token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    frames: list[dict[str, Any]] = [
        {
            "type": "open",
            "token": "controller-token",
            "requester_owner_id": "controller-a:boot-a",
            "executor_id": "exec-1",
            "target_owner_id": "controller-b:boot-b",
            "target_epoch": 7,
        }
    ]
    for index in range(2):
        payload: dict[str, Any] = {"method": "browser.status", "params": {}}
        if operation == "tool":
            payload = {
                "tool_call": ToolCall(
                    call_id=f"tool-{index}", name="read", arguments={"file_path": "/tmp/input.txt"}
                ).model_dump()
            }
        elif operation == "inference":
            payload = {"request_id": f"inference-{index}", "messages": [], "model": "remote-model"}
        frames.extend(
            [
                {
                    "type": "call",
                    "call_id": f"cancel-before-start-{index}",
                    "operation": operation,
                    "payload": payload,
                },
                {"type": "cancel", "call_id": f"cancel-before-start-{index}"},
            ]
        )
    messages = [{"type": "websocket.connect"}] + [
        {"type": "websocket.receive", "text": json.dumps(frame)} for frame in frames
    ]
    completed = asyncio.Event()
    received: list[dict[str, Any]] = []
    follow_up_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal follow_up_sent
        if messages:
            # ASGI may already have a burst buffered, so receive need not yield.
            return messages.pop(0)
        if not follow_up_sent:
            follow_up_sent = True
            # Let cancelled tasks and their completion callbacks run first.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "call",
                        "call_id": "after-cancelled-burst",
                        "operation": "rpc",
                        "payload": {"method": "browser.status", "params": {}},
                    }
                ),
            }
        await asyncio.wait_for(completed.wait(), timeout=1)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "websocket.send":
            frame = json.loads(message["text"])
            received.append(frame)
            if frame.get("call_id") == "after-cancelled-burst" and frame["type"] in {
                "result",
                "error",
            }:
                completed.set()

    websocket = WebSocket({"type": "websocket", "app": app}, receive, send)
    await handle_controller_executor_websocket(websocket)

    assert not [frame for frame in received if frame.get("code") == "executor_bridge_capacity"]
    assert connection.received == [("browser.status", {})]
    assert received[-1]["type"] == "result"


@pytest.mark.asyncio
async def test_bridge_finished_task_callback_preserves_reused_call_id() -> None:
    auth = SimpleNamespace(
        verify_controller_jwt=lambda token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    call_frame = {
        "type": "call",
        "call_id": "reused-call",
        "operation": "rpc",
        "payload": {"method": "browser.status", "params": {}},
    }
    frames = [
        {
            "type": "open",
            "token": "controller-token",
            "requester_owner_id": "controller-a:boot-a",
            "executor_id": "exec-1",
            "target_owner_id": "controller-b:boot-b",
            "target_epoch": 7,
        },
        call_frame,
    ]
    messages = [{"type": "websocket.connect"}] + [
        {"type": "websocket.receive", "text": json.dumps(frame)} for frame in frames
    ]
    outcomes: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    received: list[dict[str, Any]] = []
    reused = False

    async def receive() -> dict[str, Any]:
        nonlocal reused
        if messages:
            return messages.pop(0)
        # The send wakes this reader before scheduling the old task's done
        # callback, allowing the same ID to belong to a new task meanwhile.
        await asyncio.wait_for(outcomes.get(), timeout=1)
        if not reused:
            reused = True
            return {"type": "websocket.receive", "text": json.dumps(call_frame)}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "websocket.send":
            frame = json.loads(message["text"])
            received.append(frame)
            if frame["type"] in {"result", "error"}:
                outcomes.put_nowait(frame)

    websocket = WebSocket({"type": "websocket", "app": app}, receive, send)
    await handle_controller_executor_websocket(websocket)

    assert not [frame for frame in received if frame["type"] == "error"]
    assert [frame["type"] for frame in received] == [
        "opened",
        "accepted",
        "result",
        "accepted",
        "result",
    ]
    assert connection.received == [("browser.status", {}), ("browser.status", {})]


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
                        "executor_instance_id": "executor-instance-a",
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
                if frame["payload"]["tool_call"].get("name") == "circuit":
                    result = {
                        "output": "Executor 'exec-1' circuit breaker is open.",
                        "is_error": True,
                        "metadata": {
                            "code": "executor_circuit_open",
                            "delivery_state": "not_sent",
                            "executor_id": "exec-1",
                            "retryable": True,
                            "circuit": {
                                "name": "executor.websocket:exec-1",
                                "state": "open",
                                "retry_after_seconds": 12.5,
                                "operation": "tool.execute",
                                "last_error_type": "ConnectionError",
                                "last_failure_at": "2026-08-30T16:03:21+00:00",
                            },
                        },
                    }
                    self.incoming.put_nowait(
                        json.dumps({"type": "result", "call_id": call_id, "result": result})
                    )
                    return
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


async def _append_identity(
    identities: list[tuple[str, str | None]],
    executor_id: str,
    instance_id: str | None,
) -> None:
    identities.append((executor_id, instance_id))


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
    assert delivery_exc.value.executor_instance_id == "executor-instance-a"
    assert connection.executor_instance_id == "executor-instance-a"
    assert await connection.rpc_call("chunked", {}, timeout=1) == {"value": "chunked"}
    catalog = await connection.rpc_call("catalog", {}, timeout=2)
    assert len(catalog["tools"]) == 273
    assert await connection.llm_discover_models(
        preset="openai",
        base_url="http://model-api",
    ) == [{"id": "remote-model"}]
    chunks: list[str] = []
    dispatch_identities: list[tuple[str, str | None]] = []
    result = await connection.tool_execute(
        ToolCall(call_id="tool-1", name="bash", arguments={}),
        timeout_seconds=1,
        output_chunk_callback=lambda chunk, stream: _append_chunk(chunks, chunk),
        before_send=lambda executor_id, instance_id: _append_identity(
            dispatch_identities, executor_id, instance_id
        ),
        on_sent=lambda executor_id, instance_id: _append_identity(
            dispatch_identities, executor_id, instance_id
        ),
    )
    assert result.output == "ok"
    assert chunks == ["progress"]
    assert dispatch_identities == [
        ("exec-1", "executor-instance-a"),
        ("exec-1", "executor-instance-a"),
    ]
    circuit_result = await connection.tool_execute(
        ToolCall(call_id="tool-circuit", name="circuit", arguments={}),
        timeout_seconds=1,
    )
    assert circuit_result.is_error is True
    assert circuit_result.metadata is not None
    assert circuit_result.metadata["code"] == "executor_circuit_open"
    assert circuit_result.metadata["delivery_state"] == "not_sent"
    assert circuit_result.metadata["circuit"]["name"] == "executor.websocket:exec-1"
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


@pytest.mark.asyncio
async def test_forwarded_event_overflow_isolated_to_one_call() -> None:
    """One slow progress consumer must not abort sibling sessions."""

    bridge = _FakeClientBridge()
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    connection._ws = bridge
    connection._connected = True
    connection.executor_instance_id = "executor-instance-a"
    overflow = _PendingCall(asyncio.get_running_loop().create_future(), submitted=True)
    sibling = _PendingCall(asyncio.get_running_loop().create_future(), submitted=True)
    for index in range(overflow.events.maxsize):
        overflow.events.put_nowait({"sequence": index})
    connection._pending = {"overflow": overflow, "sibling": sibling}
    bridge.incoming.put_nowait(json.dumps({"type": "event", "call_id": "overflow", "payload": {}}))
    bridge.incoming.put_nowait(
        json.dumps(
            {
                "type": "result",
                "call_id": "sibling",
                "result": {"output": "ok", "is_error": False},
            }
        )
    )
    bridge.incoming.put_nowait(None)

    await connection._receive_loop()

    with pytest.raises(ForwardedDeliveryError) as exc:
        overflow.future.result()
    assert exc.value.executor_instance_id == "executor-instance-a"
    assert sibling.future.result()["output"] == "ok"
    await asyncio.gather(*tuple(connection._cancel_tasks), return_exceptions=True)
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
    assert connection.executor_instance_id is None
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
async def test_replacement_forwarded_bridge_cancels_by_executor_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _FakeClientBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _FakeClientBridge:
        return bridge

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", fake_connect)
    replacement = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )

    await replacement.cancel_call("stable-executor-call")

    cancel_calls = [
        frame
        for frame in bridge.sent
        if frame.get("type") == "call" and frame.get("payload", {}).get("method") == "tool.cancel"
    ]
    assert cancel_calls[0]["payload"]["params"] == {"call_id": "stable-executor-call"}
    await replacement.close()


@pytest.mark.asyncio
async def test_forwarded_call_send_rejection_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RejectingBridge(_FakeClientBridge):
        async def send(self, raw: str) -> None:
            if json.loads(raw).get("type") == "call":
                raise RuntimeError('Cannot call "send" once a close message has been sent.')
            await super().send(raw)

    bridge = _RejectingBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _RejectingBridge:
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
        await connection.rpc_call("rejected_call", {}, timeout=1)

    assert exc.value.delivery_state == "not_sent"
    await connection.close()


@pytest.mark.asyncio
async def test_forwarded_uncertain_submission_does_not_claim_physical_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UncertainBridge(_FakeClientBridge):
        async def send(self, raw: str) -> None:
            if json.loads(raw).get("type") == "call":
                raise OSError("transport write failed")
            await super().send(raw)

    bridge = _UncertainBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _UncertainBridge:
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
        await connection.rpc_call("uncertain_call", {}, timeout=1)

    assert exc.value.delivery_state == "accepted_unknown"
    assert exc.value.executor_instance_id is None
    await connection.close()


@pytest.mark.asyncio
async def test_forwarded_cancelled_connect_does_not_exhaust_call_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.providers.executor.bridge_limits import BRIDGE_MAX_CALLS

    connecting = asyncio.Event()

    async def blocked_connect(*_args: Any, **_kwargs: Any) -> None:
        connecting.set()
        await asyncio.Future()

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", blocked_connect)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    try:
        for _ in range(BRIDGE_MAX_CALLS + 1):
            connecting.clear()
            task = asyncio.create_task(connection.list_tools())
            await asyncio.wait_for(connecting.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert connection._pending == {}
    finally:
        await connection.close()


class _AdmissionClientBridge(_FakeClientBridge):
    def __init__(self) -> None:
        super().__init__()
        self.calls: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        await super().send(raw)
        frame = json.loads(raw)
        if frame["type"] == "call":
            self.calls.put_nowait(frame)

    def complete(self, frame: dict[str, Any]) -> None:
        self.incoming.put_nowait(
            json.dumps({"type": "result", "call_id": frame["call_id"], "result": {"ok": True}})
        )


@pytest.fixture
async def admission_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[ForwardedExecutorConnection, _AdmissionClientBridge]]:
    bridge = _AdmissionClientBridge()

    async def fake_connect(*_args: Any, **_kwargs: Any) -> _AdmissionClientBridge:
        return bridge

    monkeypatch.setattr("cognis.providers.executor.forwarding.connect", fake_connect)
    monkeypatch.setattr("cognis.providers.executor.forwarding.BRIDGE_MAX_CALLS", 1)
    monkeypatch.setattr("cognis.providers.executor.forwarding.BRIDGE_MAX_QUEUED_CALLS", 2)
    connection = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://controller-b:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    try:
        yield connection, bridge
    finally:
        await connection.close()


async def _queued_operation(connection: ForwardedExecutorConnection, operation: str) -> Any:
    if operation == "tool":
        return await connection.tool_execute(
            ToolCall(call_id="queued-tool", name="slow", arguments={})
        )
    if operation == "inference":
        return [
            chunk
            async for chunk in connection.llm_complete_stream(
                "queued-inference", [], "remote-model"
            )
        ]
    return await connection.rpc_call("slow", {})


def test_bridge_admits_nine_parallel_eight_call_sessions() -> None:
    from cognis.providers.executor.bridge_limits import BRIDGE_MAX_CALLS

    assert BRIDGE_MAX_CALLS == 128
    auth = SimpleNamespace(
        verify_controller_jwt=lambda token: {
            "typ": "controller",
            "sub": "controller-a:boot-a",
        }
    )
    app, connection, _ownership = _bridge_app(auth)
    release = asyncio.Event()

    async def blocked_rpc(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        connection.received.append((method, params))
        await kwargs["on_sent"]()
        await release.wait()
        return {"ok": True}

    connection.rpc_call = blocked_rpc  # type: ignore[method-assign]
    count = 9 * 8
    with TestClient(app) as client, client.websocket_connect("/api/internal/executor-bridge") as ws:
        assert _open(ws)["type"] == "opened"
        for index in range(count):
            ws.send_json(
                {
                    "type": "call",
                    "call_id": f"parallel-{index}",
                    "operation": "rpc",
                    "payload": {"method": "browser.status", "params": {}},
                }
            )
            assert ws.receive_json() == {"type": "accepted", "call_id": f"parallel-{index}"}
        assert len(connection.received) == count
        assert client.portal is not None
        client.portal.call(release.set)
        assert {ws.receive_json()["call_id"] for _ in range(count)} == {
            f"parallel-{index}" for index in range(count)
        }


@pytest.mark.asyncio
async def test_forwarded_admission_bounds_queue_and_resumes_in_fifo_order(
    admission_bridge: tuple[ForwardedExecutorConnection, _AdmissionClientBridge],
) -> None:
    connection, bridge = admission_bridge
    active = asyncio.create_task(connection.rpc_call("slow", {"index": 0}))
    frame = await asyncio.wait_for(bridge.calls.get(), timeout=1)
    queued = []
    for index in (1, 2):
        queued.append(asyncio.create_task(connection.rpc_call("slow", {"index": index})))
        await asyncio.sleep(0)
    assert len(connection._admission_waiters) == 2
    assert bridge.calls.empty()

    with pytest.raises(ForwardedDeliveryError) as exc:
        await connection.rpc_call("slow", {"index": 3})
    assert exc.value.code == "executor_bridge_capacity"
    assert exc.value.delivery_state == "not_sent"
    assert connection.pending_call_count == 1
    assert len(connection._admission_waiters) == 2

    bridge.complete(frame)
    assert await active == {"ok": True}
    for index, task in enumerate(queued, start=1):
        frame = await asyncio.wait_for(bridge.calls.get(), timeout=1)
        assert frame["payload"]["params"]["index"] == index
        assert connection.pending_call_count == 1
        bridge.complete(frame)
        assert await task == {"ok": True}
    assert connection.pending_call_count == 0
    assert not connection._admission_waiters
    assert not any(frame["type"] == "cancel" for frame in bridge.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rpc", "tool", "inference"])
async def test_forwarded_queued_timeout_is_not_sent(
    admission_bridge: tuple[ForwardedExecutorConnection, _AdmissionClientBridge],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    connection, bridge = admission_bridge
    active = asyncio.create_task(connection.rpc_call("slow", {}))
    frame = await asyncio.wait_for(bridge.calls.get(), timeout=1)
    monkeypatch.setattr("cognis.providers.executor.forwarding.BRIDGE_QUEUE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(ForwardedDeliveryError) as exc:
        await _queued_operation(connection, operation)
    assert exc.value.code == "executor_bridge_capacity"
    assert exc.value.delivery_state == "not_sent"
    assert exc.value.executor_id == "exec-1"
    assert exc.value.retry_after is not None
    assert not connection._admission_waiters
    assert connection.pending_call_count == 1
    assert bridge.calls.empty()
    assert not any(frame["type"] == "cancel" for frame in bridge.sent)
    bridge.complete(frame)
    assert await active == {"ok": True}
    assert await connection.rpc_call("browser.status", {}) == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rpc", "tool", "inference"])
async def test_forwarded_queued_cancellation_frees_slot_without_sending(
    admission_bridge: tuple[ForwardedExecutorConnection, _AdmissionClientBridge],
    operation: str,
) -> None:
    connection, bridge = admission_bridge
    active = asyncio.create_task(connection.rpc_call("slow", {}))
    frame = await asyncio.wait_for(bridge.calls.get(), timeout=1)
    cancelled = asyncio.create_task(_queued_operation(connection, operation))
    await asyncio.sleep(0)
    next_call = asyncio.create_task(connection.rpc_call("browser.status", {}))
    await asyncio.sleep(0)
    assert len(connection._admission_waiters) == 2
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert len(connection._admission_waiters) == 1
    assert not any(frame["type"] == "cancel" for frame in bridge.sent)
    assert bridge.calls.empty()
    bridge.complete(frame)
    assert await active == {"ok": True}
    assert await asyncio.wait_for(next_call, timeout=1) == {"ok": True}
    assert not connection._admission_waiters
    assert connection.pending_call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["close", "retire"])
async def test_forwarded_stopping_proxy_wakes_unsent_waiters(
    admission_bridge: tuple[ForwardedExecutorConnection, _AdmissionClientBridge],
    stop: str,
) -> None:
    connection, bridge = admission_bridge
    active = asyncio.create_task(connection.rpc_call("slow", {}))
    frame = await asyncio.wait_for(bridge.calls.get(), timeout=1)
    queued = [asyncio.create_task(connection.rpc_call("slow", {})) for _ in range(2)]
    await asyncio.sleep(0)
    assert len(connection._admission_waiters) == 2
    await getattr(connection, stop)()
    for task in queued:
        with pytest.raises(ForwardedDeliveryError) as exc:
            await asyncio.wait_for(task, timeout=1)
        assert exc.value.delivery_state == "not_sent"
    assert not connection._admission_waiters
    assert bridge.calls.empty()
    if stop == "retire":
        bridge.complete(frame)
        assert await active == {"ok": True}
    else:
        with pytest.raises(ForwardedDeliveryError) as exc:
            await active
        assert exc.value.delivery_state == "accepted_unknown"


@pytest.mark.asyncio
async def test_forwarded_queued_tool_rechecks_send_fence_after_admission(
    admission_bridge: tuple[ForwardedExecutorConnection, _AdmissionClientBridge],
) -> None:
    connection, bridge = admission_bridge
    active = asyncio.create_task(connection.rpc_call("slow", {}))
    frame = await asyncio.wait_for(bridge.calls.get(), timeout=1)
    fence_identities = []
    accepted_identities: list[tuple[str, str | None]] = []
    allowed = True

    async def fence(executor_id: str, instance_id: str | None) -> None:
        fence_identities.append((executor_id, instance_id))
        if not allowed:
            raise PermissionError("Dispatch revoked while queued")

    queued = asyncio.create_task(
        connection.tool_execute(
            ToolCall(call_id="queued-tool", name="slow", arguments={}),
            before_send=fence,
            on_sent=lambda executor_id, instance_id: _append_identity(
                accepted_identities, executor_id, instance_id
            ),
        )
    )
    await asyncio.sleep(0)
    assert len(connection._admission_waiters) == 1
    assert fence_identities == []
    allowed = False
    bridge.complete(frame)
    assert await active == {"ok": True}
    with pytest.raises(PermissionError, match="Dispatch revoked while queued"):
        await queued
    assert fence_identities == [("exec-1", "executor-instance-a")]
    assert accepted_identities == []
    assert bridge.calls.empty()
    assert connection.pending_call_count == 0
    assert not connection._admission_waiters
    assert not any(frame["type"] == "cancel" for frame in bridge.sent)
    assert await connection.rpc_call("browser.status", {}) == {"ok": True}


@pytest.mark.asyncio
async def test_forwarded_cancellation_while_awaiting_acceptance_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _FakeClientBridge()

    async def send_without_accepting(raw: str) -> None:
        frame = json.loads(raw)
        bridge.sent.append(frame)
        if frame["type"] == "open":
            bridge.incoming.put_nowait(
                json.dumps(
                    {
                        "type": "opened",
                        "owner_id": frame["target_owner_id"],
                        "epoch": frame["target_epoch"],
                        "executor_instance_id": "executor-instance-a",
                    }
                )
            )

    bridge.send = send_without_accepting  # type: ignore[method-assign]

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
    task = asyncio.create_task(
        connection.tool_execute(
            ToolCall(call_id="tool-cancel", name="bash", arguments={}),
            timeout_seconds=30,
            on_sent=lambda *_args: asyncio.sleep(0),
        )
    )
    for _ in range(100):
        if any(frame.get("type") == "call" for frame in bridge.sent):
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any(frame.get("type") == "cancel" for frame in bridge.sent)
    assert connection._pending == {}
    assert connection._tool_bridge_ids == {}
    await connection.close()


@pytest.mark.asyncio
async def test_forwarded_acceptance_timeout_is_accepted_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _FakeClientBridge()

    async def send_without_accepting(raw: str) -> None:
        frame = json.loads(raw)
        bridge.sent.append(frame)
        if frame["type"] == "open":
            bridge.incoming.put_nowait(
                json.dumps(
                    {
                        "type": "opened",
                        "owner_id": frame["target_owner_id"],
                        "epoch": frame["target_epoch"],
                        "executor_instance_id": "executor-instance-a",
                    }
                )
            )

    bridge.send = send_without_accepting  # type: ignore[method-assign]

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
        await connection.tool_execute(
            ToolCall(call_id="tool-timeout", name="bash", arguments={}),
            timeout_seconds=0.01,  # type: ignore[arg-type]
            on_sent=lambda *_args: asyncio.sleep(0),
        )

    assert exc.value.delivery_state == "accepted_unknown"
    assert exc.value.executor_instance_id is None
    assert connection._pending == {}
    assert connection._tool_bridge_ids == {}
    await connection.close()


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
        executor_id="exec-1",
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
            if _query.column_descriptions[0]["entity"].__name__ == "ExecutorRow":
                return [row]
            return SimpleNamespace(all=lambda: [lease])

    async def _controller(owner_ids: set[str]) -> dict[str, Any]:
        return {owner: SimpleNamespace(internal_url="http://peer:8000") for owner in owner_ids}

    provider._cluster_enabled = True
    provider._cluster_session_factory = lambda: _Session()
    provider._cluster_directory = SimpleNamespace(get_ready_many=_controller)
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
async def test_cluster_refresh_releases_stale_self_owned_lease_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = WebSocketExecutorProvider()
    lease = SimpleNamespace(
        resource_key="executor_connection:exec-1",
        owner_id="controller-a:boot-a",
        fencing_token=7,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    class _Session:
        bind = None

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalars(self, _query: Any) -> Any:
            return SimpleNamespace(all=lambda: [lease])

    released: list[Lease] = []

    async def release(current: Lease) -> bool:
        released.append(current)
        return True

    provider._cluster_enabled = True
    provider._cluster_session_factory = lambda: _Session()
    provider._cluster_directory = SimpleNamespace()
    provider._cluster_runtime = SimpleNamespace(owner_id="controller-a:boot-a")
    provider._cluster_auth = SimpleNamespace()
    provider._cluster_lease_store = SimpleNamespace(release=release)
    observed_times = iter((100.0, 104.9, 105.0))
    monkeypatch.setattr(
        "cognis.providers.executor.websocket.perf_counter",
        lambda: next(observed_times),
    )

    await provider.refresh_cluster_directory()
    assert released == []
    assert provider._missing_self_owned_connections == {"exec-1": 100.0}

    await provider.refresh_cluster_directory()
    assert released == []
    assert provider._missing_self_owned_connections == {"exec-1": 100.0}

    await provider.refresh_cluster_directory()
    assert released == [
        Lease(
            resource_key=lease.resource_key,
            owner_id=lease.owner_id,
            fencing_token=lease.fencing_token,
            lease_expires_at=lease.lease_expires_at,
        )
    ]
    assert provider._missing_self_owned_connections == {}


@pytest.mark.asyncio
async def test_cluster_refresh_does_not_release_self_owned_lease_with_local_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = WebSocketExecutorProvider()
    lease = SimpleNamespace(
        resource_key="executor_connection:exec-1",
        owner_id="controller-a:boot-a",
        fencing_token=7,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    class _Session:
        bind = None

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalars(self, _query: Any) -> Any:
            return SimpleNamespace(all=lambda: [lease])

    async def release(_current: Lease) -> bool:
        raise AssertionError("live local ownership must not be released")

    provider._connections["exec-1"] = SimpleNamespace(connected=True)
    provider._cluster_enabled = True
    provider._cluster_session_factory = lambda: _Session()
    provider._cluster_directory = SimpleNamespace()
    provider._cluster_runtime = SimpleNamespace(owner_id="controller-a:boot-a")
    provider._cluster_auth = SimpleNamespace()
    provider._cluster_lease_store = SimpleNamespace(release=release)
    monkeypatch.setattr(
        "cognis.providers.executor.websocket.SELF_OWNED_MISSING_CONNECTION_GRACE_SECONDS",
        0.0,
    )

    await provider.refresh_cluster_directory()

    assert provider._missing_self_owned_connections == {}


@pytest.mark.asyncio
async def test_cluster_refresh_releases_self_owned_lease_for_disconnected_local_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = WebSocketExecutorProvider()
    lease = SimpleNamespace(
        resource_key="executor_connection:exec-1",
        owner_id="controller-a:boot-a",
        fencing_token=7,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    class _Session:
        bind = None

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalars(self, _query: Any) -> Any:
            return SimpleNamespace(all=lambda: [lease])

    released: list[Lease] = []

    async def release(current: Lease) -> bool:
        released.append(current)
        return True

    provider._connections["exec-1"] = SimpleNamespace(connected=False)
    provider._cluster_enabled = True
    provider._cluster_session_factory = lambda: _Session()
    provider._cluster_directory = SimpleNamespace()
    provider._cluster_runtime = SimpleNamespace(owner_id="controller-a:boot-a")
    provider._cluster_auth = SimpleNamespace()
    provider._cluster_lease_store = SimpleNamespace(release=release)
    monkeypatch.setattr(
        "cognis.providers.executor.websocket.SELF_OWNED_MISSING_CONNECTION_GRACE_SECONDS",
        0.0,
    )

    await provider.refresh_cluster_directory()

    assert len(released) == 1
    assert released[0].fencing_token == 7


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
        executor_id="exec-1",
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
            if _query.column_descriptions[0]["entity"].__name__ == "ExecutorRow":
                return [row]
            return SimpleNamespace(all=lambda: [lease])

    async def ready_controller(owner_ids: set[str]) -> dict[str, Any]:
        return {owner: SimpleNamespace(internal_url="http://peer:8000") for owner in owner_ids}

    provider._cluster_enabled = True
    provider._cluster_session_factory = lambda: _Session()
    provider._cluster_directory = SimpleNamespace(get_ready_many=ready_controller)
    provider._cluster_runtime = SimpleNamespace(owner_id="controller-a:boot-a")
    provider._cluster_auth = SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt")
    await provider.refresh_cluster_directory()
    failed = provider.get_connection("exec-1")
    assert isinstance(failed, ForwardedExecutorConnection)

    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    ready_calls = 0

    async def delayed_ready_controller(owner_ids: set[str]) -> dict[str, Any]:
        nonlocal ready_calls
        ready_calls += 1
        if ready_calls == 1:
            refresh_entered.set()
            await release_refresh.wait()
        return await ready_controller(owner_ids)

    provider._cluster_directory = SimpleNamespace(get_ready_many=delayed_ready_controller)
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
async def test_wait_for_connection_accepts_distinct_replay_safe_same_route(
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
    replacement = ForwardedExecutorConnection(
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

    async def publish_replacement() -> None:
        provider._forwarded_connections = {
            ("exec-1", replacement.owner_id, replacement.epoch): replacement
        }
        provider._forwarded_by_executor["exec-1"] = replacement

    monkeypatch.setattr(provider, "refresh_cluster_directory", publish_replacement)
    await provider.invalidate_forwarded_connection("exec-1", old)

    resolved = await provider.wait_for_connection(
        "exec-1",
        timeout=1.0,
        failed_connection=old,
        delivery_state="accepted_unknown",
        accepted_unknown_replay_safe=True,
        failed_owner_id=old.owner_id,
        failed_epoch=old.epoch,
    )

    assert resolved is replacement
    assert old.closing
    assert provider._handles["exec-1"].metadata.get("generation") is None
    await replacement.close()


@pytest.mark.asyncio
async def test_wait_for_connection_never_reuses_failed_physical_connection() -> None:
    provider = WebSocketExecutorProvider()
    failed = ForwardedExecutorConnection(
        executor_id="exec-1",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-b:boot-b",
        epoch=7,
        owner_internal_url="http://peer:8000",
        requester_owner_id="controller-a:boot-a",
        auth_provider=SimpleNamespace(sign_controller_jwt=lambda *_args: "jwt"),
    )
    provider._forwarded_by_executor["exec-1"] = failed
    provider._forwarded_connections[("exec-1", failed.owner_id, failed.epoch)] = failed

    resolved = await provider.wait_for_connection(
        "exec-1",
        timeout=0.01,
        failed_connection=failed,
        delivery_state="accepted_unknown",
        accepted_unknown_replay_safe=True,
        failed_owner_id=failed.owner_id,
        failed_epoch=failed.epoch,
    )

    assert resolved is None
    await failed.close()


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

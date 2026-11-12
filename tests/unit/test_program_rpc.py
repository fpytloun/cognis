from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from cognis.core.program_plan import build_effective_program_plan
from cognis.executor import runner as runner_module
from cognis.executor.runner import ExecutorRunner
from cognis.models.program import (
    ProgramExecutionRequest,
    ProgramLimits,
    ProgramRuntimeBinding,
    ProgramToolDescriptor,
    ProgramToolValue,
)
from cognis.models.tool import ExecutorCapabilities, ExecutorConfig, ToolCall
from cognis.providers.executor.websocket import WebSocketExecutorConnection
from cognis.tools.executor.definitions import WEB_SEARCH_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _connection() -> WebSocketExecutorConnection:
    connection = WebSocketExecutorConnection(
        ws=AsyncMock(),
        executor_id=f"executor-{uuid.uuid4().hex}",
        capabilities=ExecutorCapabilities(),
    )
    connection.executor_instance_id = uuid.uuid4().hex
    connection._ensure_current_ownership = AsyncMock(return_value=True)
    connection._send_rpc_message = AsyncMock()
    return connection


def _web_search_descriptor() -> ProgramToolDescriptor:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(definition=WEB_SEARCH_TOOL.model_copy(update={"output_schema": None}))
    )
    return build_effective_program_plan(registry).tools[0]


def _binding(
    connection: WebSocketExecutorConnection,
    *,
    program_id: str,
    execution_id: str | None = None,
    scope_id: str | None = None,
) -> ProgramRuntimeBinding:
    return ProgramRuntimeBinding(
        program_id=program_id,
        execution_id=execution_id or uuid.uuid4().hex,
        scope_id=scope_id or uuid.uuid4().hex,
        description="Test a scoped reverse callback.",
        tool_manifest_digest="test-plan",
        tools=[],
        executor_id=connection.executor_id,
        executor_instance_id=connection.executor_instance_id,
    )


@pytest.mark.asyncio
async def test_python_executor_rpc_disables_transport_watchdog() -> None:
    connection = _connection()
    call_id = uuid.uuid4().hex
    connection.rpc_call = AsyncMock(
        return_value={"call_id": call_id, "output": "completed", "is_error": False}
    )

    result = await connection.tool_execute(
        ToolCall(
            call_id=call_id,
            name="python_execute",
            arguments={"description": "Run a bounded Python cell.", "code": "pass"},
        ),
        timeout_seconds=0,
    )

    assert result.output == "completed"
    _method, params = connection.rpc_call.await_args.args
    assert params["timeout_seconds"] == 300
    assert connection.rpc_call.await_args.kwargs["timeout"] is None


@pytest.mark.asyncio
async def test_python_runner_uses_program_runtime_not_fixed_executor_watchdog() -> None:
    async def delayed_result() -> str:
        await asyncio.sleep(0.03)
        return "completed"

    assert (
        await runner_module._invoke_with_timeout("python_execute", 0.001, delayed_result)
        == "completed"
    )
    with pytest.raises(TimeoutError):
        await runner_module._invoke_with_timeout("web_search", 0.001, delayed_result)


@pytest.mark.asyncio
async def test_reverse_rpc_is_answered_without_blocking_the_receive_owner() -> None:
    connection = _connection()
    binding = _binding(connection, program_id=uuid.uuid4().hex)
    payload = {
        "program_id": binding.program_id,
        "execution_id": binding.execution_id,
        "scope_id": binding.scope_id,
        "executor_id": binding.executor_id,
        "executor_instance_id": binding.executor_instance_id,
    }
    handler = AsyncMock(return_value={"data": False, "format": "structured"})
    connection.register_program_tool_call_handler(binding, handler)

    await connection._answer_program_tool_call("rpc-request", payload)

    handler.assert_awaited_once_with(payload)
    connection._send_rpc_message.assert_awaited_once_with(
        {
            "jsonrpc": "2.0",
            "id": "rpc-request",
            "result": {"data": False, "format": "structured"},
        }
    )


@pytest.mark.asyncio
async def test_reverse_rpc_cancellation_reaches_nested_dispatch() -> None:
    connection = _connection()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_for_governance() -> dict[str, bool]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"completed": True}

    binding = _binding(connection, program_id=uuid.uuid4().hex)
    payload = {
        "program_id": binding.program_id,
        "execution_id": binding.execution_id,
        "scope_id": binding.scope_id,
        "executor_id": binding.executor_id,
        "executor_instance_id": binding.executor_instance_id,
    }
    connection.register_program_tool_call_handler(binding, lambda _payload: wait_for_governance())
    dispatch = asyncio.create_task(connection._answer_program_tool_call("rpc-request", payload))
    await started.wait()

    connection._program_tool_call_tasks["rpc-request"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    assert cancelled.is_set()
    connection._send_rpc_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_websocket_approval_pause_resumes_same_kernel_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    monkeypatch.setattr(runner_module, "_PROGRAM_RPC_ACTIVE_TIMEOUT_SECONDS", 0.5)
    executor_id = uuid.uuid4().hex
    runner = ExecutorRunner(
        ExecutorConfig(
            executor_id=executor_id,
            controller_token=uuid.uuid4().hex,
        )
    )
    runtime = runner._program_runtime
    scope_id = uuid.uuid4().hex
    program_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    messages: list[dict[str, Any]] = []
    status_updates: list[dict[str, Any]] = []
    response_ids: list[str] = []
    child_call_id = uuid.uuid4().hex
    child_call_count = 0

    async def controller_peer(ws: Any) -> None:
        nonlocal child_call_count
        request = json.loads(await ws.recv())
        assert request["method"] == "program.tool_call"
        params = request["params"]
        child_call_count += 1
        messages.append(request)
        binding = {
            "program_id": params["program_id"],
            "execution_id": params["execution_id"],
            "kernel_generation": params["kernel_generation"],
            "scope_id": params["scope_id"],
            "sequence": params["sequence"],
        }
        awaiting_approval = {
            **binding,
            "status": "awaiting_approval",
            "approval_timeout_seconds": 10.0,
        }
        status_updates.append(awaiting_approval)
        await ws.send(
            json.dumps({"jsonrpc": "2.0", "method": "program.status", "params": awaiting_approval})
        )
        # Longer than both the program's active wall budget and this RPC's
        # active-time budget; only the normal approval deadline remains live.
        await asyncio.sleep(1.2)
        resumed = {**binding, "status": "running"}
        status_updates.append(resumed)
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "program.status", "params": resumed}))
        response_ids.append(request["id"])
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "data": False,
                        "text": "false",
                        "format": "structured",
                        "call_id": child_call_id,
                    },
                }
            )
        )
        await ws.wait_closed()

    async with serve(controller_peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await connect(f"ws://127.0.0.1:{port}")
        runner._current_ws = ws
        message_task = asyncio.create_task(runner._message_loop(ws))
        descriptor = _web_search_descriptor()
        request = ProgramExecutionRequest(
            program_id=program_id,
            execution_id=execution_id,
            scope_id=scope_id,
            description="Perform one approval-gated read.",
            code=(
                "attempts = 0\n"
                "attempts += 1\n"
                f"descriptor = (await cognis.tools.search({json.dumps(descriptor.name)}))[0]\n"
                "tool = cognis.tools.load(descriptor)\n"
                "value = await tool(query='approval-gated query')\n"
                "await cognis.emit({'attempts': attempts, 'value': value.data})"
            ),
            tools=[descriptor],
            output_schema={"type": "object"},
            limits=ProgramLimits(wall_seconds=1),
        )

        async def call_controller(child_call: Any) -> ProgramToolValue:
            return ProgramToolValue.model_validate(
                await runner._call_controller_program_tool(
                    {
                        **child_call.model_dump(mode="json"),
                        "scope_id": scope_id,
                        "executor_id": executor_id,
                        "executor_instance_id": runner.executor_instance_id,
                    },
                )
            )

        try:
            result = await asyncio.wait_for(
                runtime.execute(request, tool_call_handler=call_controller),
                timeout=10,
            )
            assert result.status == "completed", (
                result.error,
                result.stdout_preview,
                result.model_dump(),
            )
            assert result.result == {"attempts": 1, "value": False}
            child_request = messages[0]
            child_params = child_request["params"]
            generation = child_params["kernel_generation"]
            assert isinstance(generation, int) and not isinstance(generation, bool)
            assert 0 < generation <= 2**53 - 1
            assert child_params["program_id"] == program_id
            assert child_params["execution_id"] == execution_id
            assert child_params["scope_id"] == scope_id
            assert child_params["sequence"] == 1
            assert [status["status"] for status in status_updates] == [
                "awaiting_approval",
                "running",
            ]
            assert all(status["kernel_generation"] == generation for status in status_updates)
            assert all(status["program_id"] == program_id for status in status_updates)
            assert all(status["execution_id"] == execution_id for status in status_updates)
            assert all(status["scope_id"] == scope_id for status in status_updates)
            assert all(status["sequence"] == child_params["sequence"] for status in status_updates)
            assert response_ids == [child_request["id"]]
            assert result.kernel_generation == generation
            assert result.child_call_references == [child_call_id]
            assert result.nested_admitted == result.nested_completed == 1
            assert child_call_count == len(messages) == 1
        finally:
            runner._fail_program_rpcs_for_connection(ws)
            await ws.close()
            await asyncio.gather(message_task, return_exceptions=True)
            await runtime.close()


@pytest.mark.asyncio
async def test_real_websocket_disconnect_fails_pending_program_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    executor_id = uuid.uuid4().hex
    runner = ExecutorRunner(
        ExecutorConfig(executor_id=executor_id, controller_token=uuid.uuid4().hex)
    )
    runtime = runner._program_runtime
    scope_id = uuid.uuid4().hex
    program_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    approval_started = asyncio.Event()

    async def controller_peer(ws: Any) -> None:
        request = json.loads(await ws.recv())
        params = request["params"]
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "program.status",
                    "params": {
                        "program_id": params["program_id"],
                        "execution_id": params["execution_id"],
                        "kernel_generation": params["kernel_generation"],
                        "scope_id": params["scope_id"],
                        "sequence": params["sequence"],
                        "status": "awaiting_approval",
                        "approval_timeout_seconds": 30.0,
                    },
                }
            )
        )
        approval_started.set()
        await ws.wait_closed()

    async with serve(controller_peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await connect(f"ws://127.0.0.1:{port}")
        runner._current_ws = ws

        async def receive_until_disconnect() -> None:
            try:
                await runner._message_loop(ws)
            finally:
                runner._fail_program_rpcs_for_connection(ws)

        message_task = asyncio.create_task(receive_until_disconnect())
        descriptor = _web_search_descriptor()
        request = ProgramExecutionRequest(
            program_id=program_id,
            execution_id=execution_id,
            scope_id=scope_id,
            description="Handle a disconnected approval transport.",
            code=(
                f"descriptor = (await cognis.tools.search({json.dumps(descriptor.name)}))[0]\n"
                "tool = cognis.tools.load(descriptor)\n"
                "value = await tool(query='disconnect review')\n"
                "await cognis.emit(value.text)"
            ),
            tools=[descriptor],
            limits=ProgramLimits(wall_seconds=10),
        )

        async def call_controller(child_call: Any) -> ProgramToolValue:
            return ProgramToolValue.model_validate(
                await runner._call_controller_program_tool(
                    {
                        **child_call.model_dump(mode="json"),
                        "scope_id": scope_id,
                        "executor_id": executor_id,
                        "executor_instance_id": runner.executor_instance_id,
                    },
                )
            )

        try:
            execution = asyncio.create_task(
                runtime.execute(request, tool_call_handler=call_controller)
            )
            await asyncio.wait_for(approval_started.wait(), timeout=5)
            await asyncio.sleep(0.05)
            await ws.close()
            result = await asyncio.wait_for(execution, timeout=5)
            await asyncio.gather(message_task, return_exceptions=True)
            assert result.status == "uncertain"
            assert result.error is not None
            assert result.error.outcome_certainty == "unknown"
        finally:
            await ws.close()
            await asyncio.gather(message_task, return_exceptions=True)
            await runtime.close()


@pytest.mark.asyncio
async def test_real_websocket_disconnect_does_not_retry_accepted_nested_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    executor_id = uuid.uuid4().hex
    runner = ExecutorRunner(
        ExecutorConfig(executor_id=executor_id, controller_token=uuid.uuid4().hex)
    )
    runtime = runner._program_runtime
    scope_id = uuid.uuid4().hex
    program_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    mutation_accepted = asyncio.Event()
    accepted_mutations: list[dict[str, Any]] = []
    descriptor = ProgramToolDescriptor(
        tool_id=uuid.uuid4().hex,
        name="record_mutation",
        alias="record_mutation",
        schema_hash=uuid.uuid4().hex,
        description="Record one controller-side mutation.",
        input_schema={
            "type": "object",
            "properties": {"record": {"type": "string"}},
            "required": ["record"],
            "additionalProperties": False,
        },
        read_only=False,
    )

    async def controller_peer(ws: Any) -> None:
        request = json.loads(await ws.recv())
        assert request["method"] == "program.tool_call"
        params = request["params"]
        assert params["tool_id"] == descriptor.tool_id
        assert params["sequence"] == 1
        assert params["arguments"] == {"record": "once"}
        accepted_mutations.append(params)
        mutation_accepted.set()
        # The controller accepted the mutation, but its result cannot reach the
        # executor because this real WebSocket closes immediately afterward.
        await ws.close(code=1011, reason="response lost after acceptance")

    async with serve(controller_peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await connect(f"ws://127.0.0.1:{port}")
        runner._current_ws = ws

        async def receive_until_disconnect() -> None:
            try:
                await runner._message_loop(ws)
            finally:
                runner._fail_program_rpcs_for_connection(ws)

        message_task = asyncio.create_task(receive_until_disconnect())
        request = ProgramExecutionRequest(
            program_id=program_id,
            execution_id=execution_id,
            scope_id=scope_id,
            description="Do not repeat a mutation whose accepted result was lost.",
            code=(
                "try:\n"
                "    await cognis.tools.record_mutation(record='once')\n"
                "except ToolExecutionError:\n"
                "    try:\n"
                "        await cognis.tools.record_mutation(record='once')\n"
                "    except ToolExecutionError:\n"
                "        print('retry blocked')\n"
                "        await cognis.emit({'retry_blocked': True})\n"
                "    else:\n"
                "        await cognis.emit({'retry_blocked': False})\n"
                "else:\n"
                "    await cognis.emit({'retry_blocked': False})"
            ),
            tools=[descriptor],
            output_schema={"type": "object"},
            limits=ProgramLimits(wall_seconds=10),
        )

        async def call_controller(child_call: Any) -> ProgramToolValue:
            return ProgramToolValue.model_validate(
                await runner._call_controller_program_tool(
                    {
                        **child_call.model_dump(mode="json"),
                        "scope_id": scope_id,
                        "executor_id": executor_id,
                        "executor_instance_id": runner.executor_instance_id,
                    },
                )
            )

        try:
            result = await asyncio.wait_for(
                runtime.execute(request, tool_call_handler=call_controller),
                timeout=10,
            )
            await asyncio.wait_for(mutation_accepted.wait(), timeout=5)
            await asyncio.gather(message_task, return_exceptions=True)

            assert result.status == "uncertain"
            assert result.result is None
            assert result.error is not None
            assert result.error.code == "nested_outcome_uncertain"
            assert result.error.outcome_certainty == "unknown"
            assert result.nested_admitted == 1
            assert result.nested_failed == 1
            assert "retry blocked" in result.stdout_preview
            assert len(accepted_mutations) == 1
        finally:
            await ws.close()
            await asyncio.gather(message_task, return_exceptions=True)
            await runtime.close()


@pytest.mark.asyncio
async def test_real_websocket_approval_deadline_cancels_pending_child_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    monkeypatch.setattr(runner_module, "_PROGRAM_RPC_ACTIVE_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(runner_module, "PROGRAM_APPROVAL_RESULT_GRACE_SECONDS", 0.0)
    executor_id = uuid.uuid4().hex
    runner = ExecutorRunner(
        ExecutorConfig(
            executor_id=executor_id,
            controller_token=uuid.uuid4().hex,
        )
    )
    runtime = runner._program_runtime
    scope_id = uuid.uuid4().hex
    program_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    cancellation: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    async def controller_peer(ws: Any) -> None:
        request = json.loads(await ws.recv())
        params = request["params"]
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "program.status",
                    "params": {
                        "program_id": params["program_id"],
                        "execution_id": params["execution_id"],
                        "kernel_generation": params["kernel_generation"],
                        "scope_id": params["scope_id"],
                        "sequence": params["sequence"],
                        "status": "awaiting_approval",
                        "approval_timeout_seconds": 0.1,
                    },
                }
            )
        )
        message = json.loads(await ws.recv())
        if not cancellation.done():
            cancellation.set_result(message)
        await ws.wait_closed()

    async with serve(controller_peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await connect(f"ws://127.0.0.1:{port}")
        runner._current_ws = ws

        async def receive_until_disconnect() -> None:
            try:
                await runner._message_loop(ws)
            finally:
                runner._fail_program_rpcs_for_connection(ws)

        message_task = asyncio.create_task(receive_until_disconnect())
        descriptor = _web_search_descriptor()
        request = ProgramExecutionRequest(
            program_id=program_id,
            execution_id=execution_id,
            scope_id=scope_id,
            description="Expire at the independent approval deadline.",
            code=(
                f"descriptor = (await cognis.tools.search({json.dumps(descriptor.name)}))[0]\n"
                "tool = cognis.tools.load(descriptor)\n"
                "value = await tool(query='approval deadline')\n"
                "await cognis.emit(value.text)"
            ),
            tools=[descriptor],
            limits=ProgramLimits(wall_seconds=10),
        )

        async def call_controller(child_call: Any) -> ProgramToolValue:
            return ProgramToolValue.model_validate(
                await runner._call_controller_program_tool(
                    {
                        **child_call.model_dump(mode="json"),
                        "scope_id": scope_id,
                        "executor_id": executor_id,
                        "executor_instance_id": runner.executor_instance_id,
                    },
                )
            )

        try:
            execution = asyncio.create_task(
                runtime.execute(request, tool_call_handler=call_controller)
            )
            cancellation_message = await asyncio.wait_for(cancellation, timeout=5)
            assert cancellation_message["method"] == "program.tool_cancel"
            assert cancellation_message["params"]["program_id"] == program_id
            assert cancellation_message["params"]["execution_id"] == execution_id
            assert cancellation_message["params"]["sequence"] == 1
            result = await asyncio.wait_for(execution, timeout=5)
            assert result.status == "uncertain"
            assert result.error is not None
            assert result.error.outcome_certainty == "unknown"
        finally:
            await ws.close()
            await asyncio.gather(message_task, return_exceptions=True)
            await runtime.close()

"""Skill runtime readiness through deterministic dispatch and executor RPC."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.agent_loop import AgentLoop
from cognis.core.executor_pool import ExecutorPool
from cognis.core.tool_router import PermissionDecision, ToolRouter
from cognis.models.workflow import StepDefinition, Workflow
from cognis.providers.executor.delivery import DeliveryState, ExecutorDeliveryError
from cognis.tools.builtin.skill_management import SKILL_LOAD_TOOL, handle_skill_management_tool
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.unit.test_target_executor_routing import _target
from tests.unit.test_workflow_deterministic_execution import (
    _engine_runtime,
    _mock_tool_runtime,
)


async def _runtime(tmp_path, monkeypatch, *, require_ready=True, delivery="not_sent"):
    db_engine, factory, engine, task, _ = await _engine_runtime(tmp_path)
    store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
    created = await handle_skill_management_tool(
        "skill_write",
        {"name": "Runtime readiness", "instructions": "Use the bundled helper."},
        factory,
        task.created_by,
        artifact_store=store,
    )
    skill_id = json.loads(created.output)["skill_id"]
    written = await handle_skill_management_tool(
        "skill_asset_write",
        {
            "skill_id": skill_id,
            "filename": "helper.py",
            "content": "print('isolated')\n",
            "content_type": "text/x-python",
        },
        factory,
        task.created_by,
        artifact_store=store,
    )
    assert not written.is_error, written.output
    calls = []
    fail = True

    async def rpc(method, params, timeout=None, before_send=None):
        nonlocal fail
        if before_send is not None:
            await before_send("exec-active", "instance-before")
        calls.append((method, params))
        assert method == "skill.assets.prepare"
        if fail:
            fail = False
            raise ExecutorDeliveryError(
                "Executor interrupted during prepare",
                DeliveryState(delivery),
                executor_id="exec-active",
                executor_instance_id="instance-before",
            )
        return {
            "skill_id": params["skill_id"],
            "version_id": params["version_id"],
            "asset_manifest": [
                {
                    **{k: v for k, v in asset.items() if k != "content_b64"},
                    "version_id": params["version_id"],
                    "local_path": "/isolated/helper.py",
                }
                for asset in params["assets"]
            ],
            "python_environment": {"ready": True, "python_path": "/isolated/bin/python"},
        }

    connection = SimpleNamespace(
        executor_id="exec-active",
        executor_type="websocket",
        executor_instance_id="instance-before",
        rpc_call=rpc,
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=SKILL_LOAD_TOOL))
    _mock_tool_runtime(engine, monkeypatch, registry)
    runtime = engine._resolve_step_runtime.return_value
    runtime.executor_connection = connection
    runtime.active_executor_id = connection.executor_id
    runtime.executor_pool = ExecutorPool(primary=[_target(connection.executor_id)])
    async with factory() as db:
        from cognis.store.queries import create_conversation, get_task

        conversation = await create_conversation(db, task.created_by, task.agent_id, "workflow")
        conversation.active_executor_id = connection.executor_id
        row = await get_task(db, task.task_id)
        row.active_executor_id = connection.executor_id
        await db.commit()
        context_conversation, context_session, _ = engine._reuse_or_create_step_session.return_value
        context_conversation.conversation_id = conversation.conversation_id
        context_session.conversation_id = conversation.conversation_id
    loop = _controller_loop(engine, factory, store, connection)
    workflow = Workflow(
        workflow_id="wf-skill-runtime",
        name="Isolated runtime preparation",
        steps=[
            StepDefinition(
                name="transport_assets",
                type="tool_call",
                tool_call={
                    "tool": "skill_load",
                    "args": {"skill_id": skill_id, "require_assets_ready": require_ready},
                    "outputs": {
                        "python": "{{ result.value.python_environment.python_path }}",
                    }
                    if require_ready
                    else {},
                },
            ),
            StepDefinition(name="done", type="complete", complete={"summary": "Prepared"}),
        ],
    )
    return db_engine, factory, engine, task, workflow, loop, calls, connection


def _controller_loop(engine, factory, store, connection):
    loop = AgentLoop.__new__(AgentLoop)
    loop.providers = engine._providers
    loop.providers._session_factory = factory
    loop.providers.executor = SimpleNamespace(
        websocket=SimpleNamespace(
            wait_for_connection=AsyncMock(return_value=connection),
            get_ready_connection=lambda _: connection,
        )
    )
    loop.tool_router = ToolRouter(
        guardrails=engine._providers.guardrails, session_factory=factory, artifact_store=store
    )
    loop.tool_router.evaluate_tool_call = AsyncMock(
        return_value=PermissionDecision(
            call_id="isolated-evaluation",
            decision="approve",
            reasoning="Isolated test",
            conflict_code=None,
            source="intaris",
            risk="low",
            path="evaluation",
            latency_ms=0,
        )
    )
    loop._stale_session_step_output = AsyncMock(return_value=None)
    loop._sync_guardrails_terminal_result = AsyncMock()
    loop._get_tool_registry = lambda ctx: ctx.tool_registry
    loop._get_classified_tool_registry = lambda ctx, registry: registry
    loop._get_executor = lambda ctx: ctx.executor_connection
    loop._tool_runtime_metadata_for_call = lambda ctx, call: call.runtime_metadata
    loop._bind_tool_dispatch_fence = AsyncMock()
    loop.session_manager = SimpleNamespace(session_factory=factory)
    loop.session_cache = MagicMock()
    engine._agent_loop.execute_controller_tool = loop.execute_controller_tool
    engine._agent_loop.artifact_store = store
    return loop


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["not_sent", "accepted_unknown"])
async def test_deterministic_required_assets_recover_before_render(tmp_path, monkeypatch, delivery):
    db, factory, engine, task, workflow, loop, calls, _ = await _runtime(
        tmp_path, monkeypatch, delivery=delivery
    )
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status.value == "completed", json.dumps(result.workflow_state.step_outputs)
        assert result.workflow_state.step_outputs["transport_assets"]["outputs"]["python"] == (
            "/isolated/bin/python"
        )
        assert len(calls) == 2
        assert calls[0] == calls[1]
        loop.providers.executor.websocket.wait_for_connection.assert_awaited_once()
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_instruction_only_load_keeps_partial_success(tmp_path, monkeypatch):
    db, _, engine, task, workflow, loop, calls, _ = await _runtime(
        tmp_path, monkeypatch, require_ready=False
    )
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status.value == "completed", json.dumps(result.workflow_state.step_outputs)
        assert len(calls) == 1
        loop.providers.executor.websocket.wait_for_connection.assert_not_awaited()
    finally:
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment", [None, {"ready": False, "python_path": "", "error": "No runtime"}]
)
async def test_required_environment_failure_stops_before_output_render(
    tmp_path, monkeypatch, environment
):
    db, _, engine, task, workflow, loop, calls, connection = await _runtime(tmp_path, monkeypatch)
    original = connection.rpc_call

    async def incomplete(method, params, timeout=None, **kwargs):
        try:
            response = await original(method, params, timeout, **kwargs)
        except ExecutorDeliveryError:
            response = await original(method, params, timeout, **kwargs)
        response["python_environment"] = environment
        return response

    connection.rpc_call = incomplete
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status.value == "failed"
        output = result.workflow_state.step_outputs["transport_assets"]
        assert output["metadata"]["dependency_error"]["code"] == "skill_assets_not_ready"
        assert "unsafe value" not in output["error"]
        assert "done" not in result.workflow_state.step_outputs
        loop.providers.executor.websocket.wait_for_connection.assert_not_awaited()
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_controller_recovery_preserves_window_and_does_not_reuse_null(tmp_path, monkeypatch):
    from cognis.core.executor_recovery import begin_executor_recovery
    from cognis.models.workflow import pin_effective_workflow
    from cognis.store.queries import get_task

    db, factory, engine, task, workflow, loop, calls, connection = await _runtime(
        tmp_path, monkeypatch
    )
    pin_effective_workflow(task.workflow_state, workflow)
    async with factory() as session:
        row = await get_task(session, task.task_id)
        row.workflow_state = task.workflow_state.model_dump(mode="json")
        await session.commit()
    waiter = loop.providers.executor.websocket.wait_for_connection
    waiter.side_effect = asyncio.CancelledError
    original_rpc = connection.rpc_call
    try:
        with pytest.raises(asyncio.CancelledError):
            await engine.execute_workflow(task, workflow)
        async with factory() as session:
            row = await get_task(session, task.task_id)
            original_since = row.active_executor_unavailable_since
        assert original_since is not None
        assert "transport_assets" not in task.workflow_state.step_outputs
        resumed_window = await begin_executor_recovery(
            factory, conversation_id=None, task_id=task.task_id, executor_id="exec-active"
        )
        assert resumed_window.unavailable_since.replace(tzinfo=None) == original_since.replace(
            tzinfo=None
        )

        async def interrupted_again(method, params, timeout=None, **kwargs):
            raise ExecutorDeliveryError(
                "Still reconnecting", DeliveryState.NOT_SENT, executor_id="exec-active"
            )

        connection.rpc_call = interrupted_again
        with pytest.raises(asyncio.CancelledError):
            await engine.execute_workflow(task, workflow)
        async with factory() as session:
            row = await get_task(session, task.task_id)
            assert row.active_executor_unavailable_since == original_since
        # Reconstruct all execution owners and the task from durable state.
        from cognis.core.workflow_engine import WorkflowEngine
        from cognis.models.task import TaskModel
        from tests.unit.test_workflow_deterministic_execution import _DeterministicAgentLoop

        async with factory() as session:
            row = await get_task(session, task.task_id)
            recovered_task = TaskModel.model_validate(
                {
                    name: getattr(row, name)
                    for name in TaskModel.model_fields
                    if getattr(row, name, None) is not None
                }
            )
        conversation_id = engine._reuse_or_create_step_session.return_value[0].conversation_id
        store = engine._agent_loop.artifact_store
        recovered_engine = WorkflowEngine(
            session_factory=factory,
            providers=SimpleNamespace(
                llm=AsyncMock(), guardrails=SimpleNamespace(record_events=AsyncMock())
            ),
            agent_loop=_DeterministicAgentLoop(),
            step_evaluator=SimpleNamespace(),
            workflow_registry=SimpleNamespace(),
            event_bus=engine._event_bus,
            session_manager=SimpleNamespace(
                mark_completed=AsyncMock(), refresh_intaris_session_policy=AsyncMock()
            ),
            pause_waiter=SimpleNamespace(),
        )
        registry = ToolRegistry()
        registry.register(RegisteredTool(definition=SKILL_LOAD_TOOL))
        _mock_tool_runtime(recovered_engine, monkeypatch, registry)
        runtime = recovered_engine._resolve_step_runtime.return_value
        runtime.active_executor_id = "exec-active"
        runtime.executor_connection = connection
        runtime.executor_pool = ExecutorPool(primary=[_target("exec-active")])
        conv, session, _ = recovered_engine._reuse_or_create_step_session.return_value
        conv.conversation_id = session.conversation_id = conversation_id
        fresh_loop = _controller_loop(recovered_engine, factory, store, connection)
        first_recovered_attempt = True

        async def resumed_rpc(method, params, timeout=None, **kwargs):
            nonlocal first_recovered_attempt
            if first_recovered_attempt:
                first_recovered_attempt = False
                raise ExecutorDeliveryError(
                    "Recovery still in progress", DeliveryState.NOT_SENT, executor_id="exec-active"
                )
            return await original_rpc(method, params, timeout, **kwargs)

        async def resumed_reconnect(*args, **kwargs):
            async with factory() as session:
                row = await get_task(session, task.task_id)
                assert row.active_executor_unavailable_since == original_since
            return connection

        fresh_loop.providers.executor.websocket.wait_for_connection.side_effect = resumed_reconnect
        connection.rpc_call = resumed_rpc
        result = await recovered_engine.execute_workflow(recovered_task, workflow)
        assert result.status.value == "completed"
        assert result.workflow_state.step_outputs["transport_assets"]["outputs"]["python"]
        async with factory() as session:
            row = await get_task(session, task.task_id)
            assert row.active_executor_unavailable_since is None
        before_recovery = len(calls)
        # A completed step must not repeat even if the controller finalizes again.
        await recovered_engine._execute_deterministic_step(
            recovered_task,
            workflow.steps[0],
            recovered_task.workflow_state,
            workflow,
            cancel_event=None,
        )
        assert len(calls) == before_recovery
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_expired_durable_window_returns_structured_timeout(tmp_path, monkeypatch):
    from cognis.store.queries import get_task

    db, factory, engine, task, workflow, loop, calls, _ = await _runtime(tmp_path, monkeypatch)
    try:
        async with factory() as session:
            row = await get_task(session, task.task_id)
            row.active_executor_unavailable_since = datetime.now(UTC) - timedelta(seconds=901)
            await session.commit()
        result = await engine.execute_workflow(task, workflow)
        assert result.status.value == "failed"
        assert result.result_data["code"] == "executor_recovery_timeout"
        assert result.result_data["phase"] == "skill_assets_prepare"
        assert len(calls) == 1
        loop.providers.executor.websocket.wait_for_connection.assert_not_awaited()
    finally:
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "fence", "eligibility"])
async def test_reconnect_does_not_replay_after_cancellation_or_revocation(
    tmp_path, monkeypatch, stop
):
    from cognis.core.task_execution import StaleTaskExecutionOwner

    db, _, engine, task, workflow, loop, calls, connection = await _runtime(tmp_path, monkeypatch)
    cancel = asyncio.Event()
    fence = SimpleNamespace(assert_current=AsyncMock())
    contexts = []

    async def dispatch(ctx, call):
        contexts.append(ctx)
        ctx.execution_fence = fence
        return await loop.execute_controller_tool(ctx, call)

    async def reconnect(*args, **kwargs):
        if stop == "cancel":
            cancel.set()
        elif stop == "fence":
            fence.assert_current.side_effect = StaleTaskExecutionOwner("Owner changed")
        else:
            loop._refresh_executor_eligibility = AsyncMock(return_value=False)
        return connection

    engine._agent_loop.execute_controller_tool = dispatch
    loop.providers.executor.websocket.wait_for_connection.side_effect = reconnect
    try:
        if stop == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await engine.execute_workflow(task, workflow, cancel_event=cancel)
        elif stop == "fence":
            with pytest.raises(StaleTaskExecutionOwner):
                await engine.execute_workflow(task, workflow)
        else:
            result = await engine.execute_workflow(task, workflow)
            assert result.status.value == "failed"
        assert len(calls) == 1
    finally:
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["true", 1, None])
async def test_readiness_argument_uses_canonical_schema_validation(tmp_path, monkeypatch, argument):
    db, _, engine, task, workflow, loop, calls, _ = await _runtime(tmp_path, monkeypatch)
    workflow.steps[0].tool_call.args["require_assets_ready"] = argument
    workflow.steps[0].tool_call.outputs = {}
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status.value == "failed"
        assert not calls
        loop.tool_router.evaluate_tool_call.assert_not_awaited()
    finally:
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_process", [False, True])
@pytest.mark.parametrize("delivery", ["not_sent", "accepted_unknown", "response_timeout"])
async def test_real_rpc_reconnect_prepares_same_executor_cache(
    tmp_path, monkeypatch, restart_process, delivery
):
    from pathlib import Path

    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorCapabilities, ExecutorConfig
    from cognis.providers.executor.websocket import WebSocketExecutorConnection

    db, _, engine, task, workflow, loop, _, connection = await _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "executor-cache"))
    monkeypatch.setattr(
        "cognis.executor.skill_python.prepare_python_environment",
        AsyncMock(return_value={"ready": True, "python_path": "/isolated/bin/python"}),
    )
    requests = asyncio.Queue()
    responses = asyncio.Queue()
    fail_send = delivery == "accepted_unknown"
    drop_response = delivery == "response_timeout"
    prepared = asyncio.Event()

    class ControllerSocket:
        async def send_json(self, message):
            nonlocal fail_send
            await requests.put(json.dumps(message))
            if fail_send:
                fail_send = False
                # The runner accepted the message; the response can be lost.
                await prepared.wait()
                raise OSError("Isolated transport loss after send")

        async def receive_json(self):
            return await responses.get()

        async def close(self, **kwargs):
            pass

    class RunnerSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return await requests.get()

        async def send(self, message):
            nonlocal drop_response
            prepared.set()
            if drop_response:
                drop_response = False
                return
            await responses.put(json.loads(message))

    runner = ExecutorRunner(ExecutorConfig(executor_id="exec-active", controller_token="isolated"))
    socket = ControllerSocket()
    old = WebSocketExecutorConnection(socket, "exec-active", ExecutorCapabilities())
    old.start_receiver()
    if delivery == "not_sent":
        old._connected = False
    runner_task = asyncio.create_task(runner._message_loop(RunnerSocket()))
    fresh = None

    async def physical_rpc(method, params, timeout=None, **kwargs):
        return await old.rpc_call(
            method, params, timeout=0.05 if delivery == "response_timeout" else timeout, **kwargs
        )

    connection.rpc_call = physical_rpc
    fetch = AsyncMock(side_effect=AssertionError("Preparation is not a tool.execute call"))

    async def reconnect(executor_id, **kwargs):
        nonlocal runner, runner_task, fresh
        assert executor_id == "exec-active"
        assert kwargs["delivery_state"] == (
            "accepted_unknown" if delivery == "response_timeout" else delivery
        )
        assert kwargs["accepted_unknown_replay_safe"] is True
        await old.close()
        if restart_process:
            runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner_task
            await runner._cancel_connection_handler_tasks()
            previous = runner.executor_instance_id
            runner = ExecutorRunner(
                ExecutorConfig(executor_id="exec-active", controller_token="isolated")
            )
            assert previous != runner.executor_instance_id
            runner_task = asyncio.create_task(runner._message_loop(RunnerSocket()))
        fresh = WebSocketExecutorConnection(socket, "exec-active", ExecutorCapabilities())
        fresh.fetch_tool_result = fetch
        fresh.start_receiver()
        return fresh

    loop.providers.executor.websocket.wait_for_connection.side_effect = reconnect
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status.value == "completed", json.dumps(result.workflow_state.step_outputs)
        assert list((tmp_path / "executor-cache").rglob("helper.py"))
        helper = next((tmp_path / "executor-cache").rglob("helper.py"))
        assert Path(helper).read_text() == "print('isolated')\n"
        loop.providers.executor.websocket.wait_for_connection.assert_awaited_once()
        fetch.assert_not_awaited()
    finally:
        runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        await runner._cancel_connection_handler_tasks()
        await old.close()
        if fresh:
            await fresh.close()
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [None, "cancel", "fence"])
@pytest.mark.parametrize("wait_for_send_lock", [False, True])
async def test_required_preparation_uses_forwarded_rpc_send_fence(
    tmp_path, monkeypatch, stop, wait_for_send_lock
):
    from cognis.core.task_execution import StaleTaskExecutionOwner
    from cognis.executor.skill_assets import prepare_skill_assets
    from cognis.models.tool import ExecutorCapabilities
    from cognis.providers.executor.forwarding import ForwardedExecutorConnection

    db, _, engine, task, workflow, loop, _, connection = await _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "executor-cache"))
    monkeypatch.setattr(
        "cognis.executor.skill_python.prepare_python_environment",
        AsyncMock(return_value={"ready": True, "python_path": "/isolated/bin/python"}),
    )
    forwarded = ForwardedExecutorConnection(
        executor_id="exec-active",
        capabilities=ExecutorCapabilities(),
        owner_id="controller-owner",
        epoch=1,
        owner_internal_url="http://peer:8000",
        requester_owner_id="controller-requester",
        auth_provider=None,
    )
    frames = []

    class BridgeSocket:
        async def send(self, encoded):
            frame = json.loads(encoded)
            frames.append(frame)
            assert frame["type"] == "call"
            assert frame["operation"] == "rpc"
            assert frame["payload"]["method"] == "skill.assets.prepare"
            response = await prepare_skill_assets(frame["payload"]["params"])
            forwarded._pending[frame["call_id"]].future.set_result(response)

        async def close(self):
            pass

    forwarded._connected = True
    forwarded._ws = BridgeSocket()
    connection.rpc_call = forwarded.rpc_call
    blocked = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        async def acquire(self):
            if self.locked():
                blocked.set()
            return await super().acquire()

    forwarded._send_lock = ObservedLock()
    held_by_test = wait_for_send_lock
    if held_by_test:
        await forwarded._send_lock.acquire()
    if stop is not None and not wait_for_send_lock:
        loop._bind_tool_dispatch_fence.side_effect = (
            asyncio.CancelledError if stop == "cancel" else StaleTaskExecutionOwner("Owner changed")
        )
    work = asyncio.create_task(engine.execute_workflow(task, workflow))
    try:
        if wait_for_send_lock:
            await asyncio.wait_for(blocked.wait(), timeout=5)
            if stop is not None:
                loop._bind_tool_dispatch_fence.side_effect = (
                    asyncio.CancelledError
                    if stop == "cancel"
                    else StaleTaskExecutionOwner("Owner changed while send was queued")
                )
            held_by_test = False
            forwarded._send_lock.release()
        if stop is not None:
            with pytest.raises(
                asyncio.CancelledError if stop == "cancel" else StaleTaskExecutionOwner
            ):
                await work
            assert not frames
        else:
            result = await work
            assert result.status.value == "completed", json.dumps(
                result.workflow_state.step_outputs
            )
            assert len(frames) == 1
        loop._bind_tool_dispatch_fence.assert_awaited_once()
        loop.providers.executor.websocket.wait_for_connection.assert_not_awaited()
        assert not forwarded._pending
    finally:
        if held_by_test:
            forwarded._send_lock.release()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        await forwarded.close()
        await db.dispose()


@pytest.mark.asyncio
async def test_reprepare_keeps_authorized_bundle_when_latest_version_changes(tmp_path, monkeypatch):
    db, factory, engine, task, workflow, loop, calls, connection = await _runtime(
        tmp_path, monkeypatch
    )
    skill_id = workflow.steps[0].tool_call.args["skill_id"]

    async def reconnect(*args, **kwargs):
        result = await handle_skill_management_tool(
            "skill_asset_write",
            {
                "skill_id": skill_id,
                "filename": "helper.py",
                "content": "print('new version must not replace retry bundle')\n",
                "content_type": "text/x-python",
            },
            factory,
            task.created_by,
            artifact_store=engine._agent_loop.artifact_store,
        )
        assert not result.is_error, result.output
        return connection

    loop.providers.executor.websocket.wait_for_connection.side_effect = reconnect
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status.value == "completed", json.dumps(result.workflow_state.step_outputs)
        assert len(calls) == 2
        assert calls[0] == calls[1]
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_prepare_send_fence_cannot_be_hidden_as_instruction_warning(tmp_path, monkeypatch):
    from cognis.core.task_execution import StaleTaskExecutionOwner

    db, _, engine, task, workflow, loop, calls, _ = await _runtime(tmp_path, monkeypatch)
    loop._bind_tool_dispatch_fence.side_effect = StaleTaskExecutionOwner("Owner changed at send")
    try:
        with pytest.raises(StaleTaskExecutionOwner):
            await engine.execute_workflow(task, workflow)
        assert not calls
        loop.providers.executor.websocket.wait_for_connection.assert_not_awaited()
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_prepared_runtime_renders_and_dispatches_receipt_without_inference(
    tmp_path, monkeypatch
):
    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorCapabilities, ExecutorConfig
    from cognis.providers.executor.websocket import WebSocketExecutorConnection
    from cognis.tools.executor.definitions import executor_tool_definitions
    from cognis.tools.executor.shell import handle_bash

    db, factory, engine, task, workflow, loop, _, connection = await _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "executor-cache"))
    registry = engine._resolve_step_runtime.return_value.tool_registry
    bash = next(tool for tool in executor_tool_definitions() if tool.name == "bash")
    registry.register(RegisteredTool(definition=bash))
    prepared = workflow.steps[0].tool_call
    prepared.outputs = {
        "asset_manifest": "{{ result.value.asset_manifest }}",
        "python_environment": "{{ result.value.python_environment }}",
    }
    published = await handle_skill_management_tool(
        "skill_asset_write",
        {
            "skill_id": prepared.args["skill_id"],
            "filename": "helper.py",
            "content": (
                "import json, os\n"
                "print(json.dumps({'needs_agent': False, "
                "'details_artifact_id': os.environ['SOURCE_ID']}))\n"
            ),
            "content_type": "text/x-python",
        },
        factory,
        task.created_by,
        artifact_store=engine._agent_loop.artifact_store,
    )
    assert not published.is_error, published.output
    workflow.steps[1:1] = [
        StepDefinition(
            name="preflight_receipt",
            type="tool_call",
            tool_call={
                "tool": "bash",
                "allow_side_effects": True,
                "args": {
                    "command": (
                        "python3 -B - <<'PY'\n"
                        "import json, os, subprocess\n"
                        "assets=json.loads(os.environ['ASSET_MANIFEST'])\n"
                        "paths=[a['local_path'] for a in assets if a['filename']=='helper.py']\n"
                        "assert len(paths)==1\n"
                        "raise SystemExit(subprocess.call("
                        "[os.environ['MANAGED_PYTHON'], '-B', paths[0]]))\n"
                        "PY"
                    ),
                    "workdir": str(tmp_path),
                    "env": {
                        "ASSET_MANIFEST": (
                            "{{ steps.transport_assets.outputs.asset_manifest | tojson }}"
                        ),
                        "MANAGED_PYTHON": (
                            "{{ steps.transport_assets.outputs.python_environment.python_path }}"
                        ),
                        "SOURCE_ID": task.task_id,
                    },
                },
            },
        )
    ]
    requests = asyncio.Queue()
    responses = asyncio.Queue()
    received = []

    class ControllerSocket:
        async def send_json(self, message):
            received.append(message)
            await requests.put(json.dumps(message))

        async def receive_json(self):
            return await responses.get()

        async def close(self, **kwargs):
            pass

    class RunnerSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return await requests.get()

        async def send(self, message):
            await responses.put(json.loads(message))

    runner = ExecutorRunner(ExecutorConfig(executor_id="exec-active", controller_token="isolated"))
    runner._configured = True
    runner._runtime_state = "active"
    runner._configured_tool_definitions = [bash]
    runner._tool_handlers = {"bash": handle_bash}
    rpc = WebSocketExecutorConnection(ControllerSocket(), "exec-active", ExecutorCapabilities())
    connection.rpc_call = rpc.rpc_call
    connection.tool_execute = rpc.tool_execute
    rpc.start_receiver()
    runner_task = asyncio.create_task(runner._message_loop(RunnerSocket()))
    try:
        result = await engine.execute_workflow(task, workflow)
        assert result.status.value == "completed", json.dumps(result.workflow_state.step_outputs)
        outputs = result.workflow_state.step_outputs
        assert outputs["transport_assets"]["outputs"]["python_environment"]["ready"] is True
        assert outputs["preflight_receipt"]["outputs"] == {
            "needs_agent": False,
            "details_artifact_id": task.task_id,
            "tool_output_ref": outputs["preflight_receipt"]["metadata"]["output_ref"],
        }
        assert [message["method"] for message in received] == [
            "skill.assets.prepare",
            "tool.execute",
        ]
        assert received[1]["params"]["tool_name"] == "bash"
        engine._providers.llm.assert_not_called()
        engine._providers.llm.complete.assert_not_awaited()
        loop.providers.executor.websocket.wait_for_connection.assert_not_awaited()
    finally:
        runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)
        await runner._cancel_connection_handler_tasks()
        await rpc.close()
        await db.dispose()

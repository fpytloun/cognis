from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cognis.core.agent_loop import AgentLoop, _ProgramInvocation
from cognis.core.program_plan import build_effective_program_plan
from cognis.models.program import ProgramRuntimeBinding, ProgramToolCall
from cognis.models.tool import ToolCall, ToolDefinition, ToolResult
from cognis.tools.builtin.artifact_tools import ARTIFACT_WRITE_TOOL
from cognis.tools.executor.definitions import WEB_SEARCH_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry


@pytest.mark.asyncio
async def test_program_escalation_publishes_live_approval_state_with_linkage() -> None:
    definition = ARTIFACT_WRITE_TOOL.model_copy(update={"output_schema": None})
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=definition))
    plan = build_effective_program_plan(registry)
    descriptor = plan.tools[0]
    assert descriptor.read_only is False
    parent_call_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    scope_id = uuid.uuid4().hex
    executor_id = uuid.uuid4().hex
    executor_instance_id = uuid.uuid4().hex
    parent_call = ToolCall(
        call_id=parent_call_id,
        name="python_execute",
        arguments={},
        execution_scope_id=scope_id,
    )
    statuses: list[dict[str, Any]] = []
    live_results: list[dict[str, Any]] = []
    flushed_events: list[dict[str, Any]] = []
    deadline_updates: list[tuple[str, str, Any]] = []

    async def send_status(status: dict[str, Any]) -> None:
        statuses.append(status)

    async def on_tool_result(
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: Any,
        attachments: list[dict[str, Any]],
        file_diffs: list[dict[str, Any]],
        presentation: dict[str, Any] | None,
        *,
        turn_cycle_index: int | None = None,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        live_results.append(
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "result": result,
                "is_error": is_error,
                "duration_ms": duration_ms,
                "evaluation": evaluation,
                "runtime_metadata": runtime_metadata,
            }
        )

    context = SimpleNamespace(
        turn_id=uuid.uuid4().hex,
        current_turn_cycle_index=4,
        on_tool_call_callback=None,
        on_tool_result_callback=on_tool_result,
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.tool_router = SimpleNamespace(
        update_program_approval_status=lambda program_id, status, timeout=None, sequence=0: (
            deadline_updates.append((program_id, status, timeout, sequence))
        )
    )
    connection = SimpleNamespace(
        executor_id=executor_id,
        executor_instance_id=executor_instance_id,
        send_program_status=send_status,
    )
    loop._program_invocations = {
        parent_call_id: _ProgramInvocation(
            context=context,
            parent_call=parent_call,
            connection=connection,
            program_context=ProgramRuntimeBinding(
                program_id=parent_call_id,
                execution_id=execution_id,
                scope_id=scope_id,
                expected_generation=1,
                description="Save an approved artifact.",
                tool_manifest_digest=plan.plan_hash,
                tools=plan.tools,
                executor_id=executor_id,
                executor_instance_id=executor_instance_id,
            ),
            program_plan=plan,
            max_calls=4,
            max_concurrency=2,
        )
    }
    loop._program_invocations_lock = asyncio.Lock()
    loop._get_tool_registry = lambda _context: registry
    loop._get_classified_tool_registry = lambda _context, current: current
    loop._record_execution_evidence = lambda *_args, **_kwargs: None

    async def flush_events(_context: Any, events: list[Any], *, reason: str) -> None:
        flushed_events.extend(dict(event.data) for event in events)

    loop._flush_events_incremental = flush_events
    loop.execute_controller_tool = AsyncMock(
        return_value=ToolResult(
            output="Approval is required.",
            is_error=True,
            metadata={
                "evaluation": {
                    "decision": "escalate",
                    "risk": "medium",
                    "reasoning": "The write needs user approval.",
                }
            },
        )
    )
    loop.persist_controller_tool_output = AsyncMock(
        return_value=ToolResult(output="Approved result.", is_error=False, duration_ms=5)
    )

    async def resolve_escalation(
        result: ToolResult,
        child_call: ToolCall,
        _context: Any,
        _events: list[Any],
        callback: Any,
    ) -> ToolResult:
        assert result.metadata is not None
        await callback(
            child_call.call_id,
            child_call.name,
            "Waiting for user approval...",
            False,
            None,
            {
                **result.metadata["evaluation"],
                "pending": True,
                "phase": "waiting_for_approval",
                "approval_timeout_seconds": 30.0,
            },
            [],
            [],
            None,
            turn_cycle_index=4,
        )
        return ToolResult(output="Approved result.", is_error=False, duration_ms=5)

    loop._handle_escalation = AsyncMock(side_effect=resolve_escalation)
    child = ProgramToolCall(
        program_id=parent_call_id,
        execution_id=execution_id,
        kernel_generation=1,
        sequence=1,
        tool_id=descriptor.tool_id,
        schema_hash=descriptor.schema_hash,
        arguments={"filename": "approval.txt", "content": "approved"},
    )

    response = await loop._handle_program_tool_rpc(
        connection,
        {
            **child.model_dump(mode="json"),
            "scope_id": scope_id,
            "executor_id": executor_id,
            "executor_instance_id": executor_instance_id,
        },
    )

    assert response["text"] == "Approved result."
    assert statuses == [
        {
            "program_id": parent_call_id,
            "execution_id": execution_id,
            "kernel_generation": 1,
            "scope_id": scope_id,
            "sequence": 1,
            "status": "awaiting_approval",
            "approval_timeout_seconds": 30.0,
        },
        {
            "program_id": parent_call_id,
            "execution_id": execution_id,
            "kernel_generation": 1,
            "scope_id": scope_id,
            "sequence": 1,
            "status": "running",
        },
    ]
    assert deadline_updates == [
        (parent_call_id, "awaiting_approval", 30.0, 1),
        (parent_call_id, "running", None, 1),
    ]
    pending = next(item for item in live_results if item["evaluation"]["pending"])
    assert pending["evaluation"]["phase"] == "waiting_for_approval"
    assert pending["runtime_metadata"] == {
        "parent_call_id": parent_call_id,
        "program_id": parent_call_id,
        "kernel_generation": 1,
    }
    assert any(
        event.get("caller", {}).get("type") == "program" and event.get("kernel_generation") == 1
        for event in flushed_events
    )


@pytest.mark.asyncio
async def test_program_child_uses_normal_controller_path_and_live_linkage() -> None:
    definition = WEB_SEARCH_TOOL.model_copy(update={"output_schema": None})
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=definition))
    plan = build_effective_program_plan(registry)
    descriptor = plan.tools[0]

    parent_call_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    scope_id = uuid.uuid4().hex
    executor_id = uuid.uuid4().hex
    executor_instance_id = uuid.uuid4().hex
    parent_call = ToolCall(
        call_id=parent_call_id,
        name="python_execute",
        arguments={},
        execution_scope_id=scope_id,
    )
    binding = ProgramRuntimeBinding(
        program_id=parent_call_id,
        execution_id=execution_id,
        scope_id=scope_id,
        expected_generation=1,
        description="Find information.",
        tool_manifest_digest=plan.plan_hash,
        tools=plan.tools,
        executor_id=executor_id,
        executor_instance_id=executor_instance_id,
    )
    child_connection = SimpleNamespace(
        executor_id=executor_id,
        executor_instance_id=executor_instance_id,
    )
    child_call_started = asyncio.Event()
    cancelled_call_started = asyncio.Event()
    approval_continued = asyncio.Event()
    dispatch_count = 0
    call_events: list[dict[str, Any]] = []
    result_events: list[dict[str, Any]] = []
    live_calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    live_results: list[tuple[str, str, str, dict[str, Any]]] = []
    context = SimpleNamespace(
        turn_id=uuid.uuid4().hex,
        current_turn_cycle_index=3,
        on_tool_call_callback=None,
        on_tool_result_callback=None,
    )

    async def on_tool_call(
        tool_name: str,
        call_id: str,
        arguments: dict[str, Any] | None,
        *,
        turn_cycle_index: int | None = None,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        live_calls.append((tool_name, call_id, arguments or {}, runtime_metadata or {}))

    async def on_tool_result(
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int,
        evaluation: Any,
        attachments: list[dict[str, Any]],
        file_diffs: list[dict[str, Any]],
        presentation: dict[str, Any] | None,
        *,
        turn_cycle_index: int | None = None,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        live_results.append((call_id, tool_name, result, runtime_metadata or {}))

    context.on_tool_call_callback = on_tool_call
    context.on_tool_result_callback = on_tool_result

    loop = AgentLoop.__new__(AgentLoop)
    loop._program_invocations = {
        parent_call_id: _ProgramInvocation(
            context=context,
            parent_call=parent_call,
            connection=child_connection,
            program_context=binding,
            program_plan=plan,
            max_calls=4,
            max_concurrency=2,
        )
    }
    loop._program_invocations_lock = asyncio.Lock()
    loop._get_tool_registry = lambda _context: registry
    loop._get_classified_tool_registry = lambda _context, current: current
    loop._record_execution_evidence = lambda *_args, **_kwargs: None

    async def flush_events(_context: Any, events: list[Any], *, reason: str) -> None:
        target = call_events if reason == "program_tool_call" else result_events
        target.extend(dict(event.data) for event in events)

    loop._flush_events_incremental = flush_events

    async def normal_controller_dispatch(_context: Any, child_call: ToolCall) -> ToolResult:
        nonlocal dispatch_count
        dispatch_count += 1
        if dispatch_count > 1:
            cancelled_call_started.set()
            await asyncio.Event().wait()
        child_call_started.set()
        await approval_continued.wait()
        assert child_call.name == definition.name
        assert child_call.arguments == {"query": "current status"}
        assert child_call.execution_scope_id == scope_id
        return ToolResult(output="Approved search result", is_error=False, duration_ms=5)

    loop.execute_controller_tool = AsyncMock(side_effect=normal_controller_dispatch)
    loop.persist_controller_tool_output = AsyncMock(
        side_effect=lambda _context, _child_call, tool_result: tool_result
    )

    child = ProgramToolCall(
        program_id=parent_call_id,
        execution_id=execution_id,
        kernel_generation=1,
        sequence=1,
        tool_id=descriptor.tool_id,
        schema_hash=descriptor.schema_hash,
        arguments={"query": "current status"},
    )
    params = {
        **child.model_dump(mode="json"),
        "scope_id": scope_id,
        "executor_id": executor_id,
        "executor_instance_id": executor_instance_id,
    }
    dispatch = asyncio.create_task(loop._handle_program_tool_rpc(child_connection, params))
    await asyncio.wait_for(child_call_started.wait(), timeout=2)
    assert not dispatch.done()
    assert len(live_calls) == 1
    assert live_results == []
    approval_continued.set()
    response = await asyncio.wait_for(dispatch, timeout=2)

    assert response["text"] == "Approved search result"
    assert len(call_events) == 1
    assert len(result_events) == 1
    for event in (call_events[0], result_events[0]):
        assert event["parent_call_id"] == parent_call_id
        assert event["program_id"] == parent_call_id
        assert event["kernel_generation"] == 1
    assert live_calls == [
        (
            definition.name,
            response["call_id"],
            {"query": "current status"},
            {
                "parent_call_id": parent_call_id,
                "program_id": parent_call_id,
                "kernel_generation": 1,
            },
        )
    ]
    assert live_results == [
        (
            response["call_id"],
            definition.name,
            "Approved search result",
            {
                "parent_call_id": parent_call_id,
                "program_id": parent_call_id,
                "kernel_generation": 1,
            },
        )
    ]
    loop.execute_controller_tool.assert_awaited_once()

    cancelled_child = child.model_copy(update={"sequence": 2})
    cancelled_params = {
        **cancelled_child.model_dump(mode="json"),
        "scope_id": scope_id,
        "executor_id": executor_id,
        "executor_instance_id": executor_instance_id,
    }
    cancelled_dispatch = asyncio.create_task(
        loop._handle_program_tool_rpc(child_connection, cancelled_params)
    )
    await asyncio.wait_for(cancelled_call_started.wait(), timeout=2)
    cancelled_dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_dispatch

    assert len(call_events) == 2
    assert len(result_events) == 2
    assert result_events[-1]["is_error"] is True
    assert result_events[-1]["parent_call_id"] == parent_call_id
    assert result_events[-1]["program_id"] == parent_call_id
    assert result_events[-1]["kernel_generation"] == 1
    assert live_results[-1][2] == "Program child call was cancelled."
    assert loop.execute_controller_tool.await_count == 2

    replay = await loop._handle_program_tool_rpc(child_connection, cancelled_params)
    assert replay["text"] == "Program child call was cancelled."
    assert replay["outcome_certainty"] == "unknown"
    assert loop.execute_controller_tool.await_count == 2


@pytest.mark.asyncio
async def test_program_child_arguments_are_validated_before_dispatch() -> None:
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=WEB_SEARCH_TOOL))
    plan = build_effective_program_plan(registry)
    descriptor = plan.tools[0]
    parent_call_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    scope_id = uuid.uuid4().hex
    executor_id = uuid.uuid4().hex
    executor_instance_id = uuid.uuid4().hex
    connection = SimpleNamespace(
        executor_id=executor_id,
        executor_instance_id=executor_instance_id,
    )
    invocation = _ProgramInvocation(
        context=SimpleNamespace(),
        parent_call=ToolCall(
            call_id=parent_call_id,
            name="python_execute",
            arguments={},
            execution_scope_id=scope_id,
        ),
        connection=connection,
        program_context=ProgramRuntimeBinding(
            program_id=parent_call_id,
            execution_id=execution_id,
            scope_id=scope_id,
            expected_generation=1,
            description="Search the web.",
            tool_manifest_digest=plan.plan_hash,
            tools=plan.tools,
            executor_id=executor_id,
            executor_instance_id=executor_instance_id,
        ),
        program_plan=plan,
        max_calls=4,
        max_concurrency=1,
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop._program_invocations = {parent_call_id: invocation}
    loop._program_invocations_lock = asyncio.Lock()
    child = ProgramToolCall(
        program_id=parent_call_id,
        execution_id=execution_id,
        kernel_generation=1,
        sequence=1,
        tool_id=descriptor.tool_id,
        schema_hash=descriptor.schema_hash,
        arguments={"query": 42},
    )

    response = await loop._handle_program_tool_rpc(
        connection,
        {
            **child.model_dump(mode="json"),
            "scope_id": scope_id,
            "executor_id": executor_id,
            "executor_instance_id": executor_instance_id,
        },
    )

    assert response["is_error"] is True
    assert response["outcome_certainty"] == "known"
    assert "did not match the expected schema" in response["text"]
    assert invocation.admitted == 0


@pytest.mark.asyncio
async def test_program_output_parse_failure_preserves_unknown_child_outcome() -> None:
    payload = WEB_SEARCH_TOOL.model_dump(mode="python")
    payload["source"]["type"] = "intaris_mcp"
    payload["output_schema"] = {"type": "object"}
    payload["descriptor"] = None
    payload["native_operations"] = None
    definition = ToolDefinition.model_validate(payload)
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=definition))
    plan = build_effective_program_plan(registry)
    descriptor = plan.tools[0]
    parent_call_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    scope_id = uuid.uuid4().hex
    executor_id = uuid.uuid4().hex
    executor_instance_id = uuid.uuid4().hex
    connection = SimpleNamespace(
        executor_id=executor_id,
        executor_instance_id=executor_instance_id,
    )
    invocation = _ProgramInvocation(
        context=SimpleNamespace(
            turn_id=uuid.uuid4().hex,
            current_turn_cycle_index=1,
            on_tool_call_callback=None,
            on_tool_result_callback=None,
        ),
        parent_call=ToolCall(
            call_id=parent_call_id,
            name="python_execute",
            arguments={},
            execution_scope_id=scope_id,
        ),
        connection=connection,
        program_context=ProgramRuntimeBinding(
            program_id=parent_call_id,
            execution_id=execution_id,
            scope_id=scope_id,
            expected_generation=1,
            description="Read a reference.",
            tool_manifest_digest=plan.plan_hash,
            tools=plan.tools,
            executor_id=executor_id,
            executor_instance_id=executor_instance_id,
        ),
        program_plan=plan,
        max_calls=2,
        max_concurrency=1,
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop._program_invocations = {parent_call_id: invocation}
    loop._program_invocations_lock = asyncio.Lock()
    loop._get_tool_registry = lambda _context: registry
    loop._get_classified_tool_registry = lambda _context, current: current
    loop._record_execution_evidence = lambda *_args, **_kwargs: None
    loop.execute_controller_tool = AsyncMock(
        return_value=ToolResult(
            output="not JSON",
            is_error=False,
            metadata={"outcome_certainty": "unknown"},
        )
    )
    loop.persist_controller_tool_output = AsyncMock(side_effect=lambda _ctx, _call, result: result)
    loop._flush_events_incremental = AsyncMock()
    loop.tool_router = SimpleNamespace(
        update_program_approval_status=lambda *_args, **_kwargs: None
    )
    child = ProgramToolCall(
        program_id=parent_call_id,
        execution_id=execution_id,
        kernel_generation=1,
        sequence=1,
        tool_id=descriptor.tool_id,
        schema_hash=descriptor.schema_hash,
        arguments={"query": "uncertain output"},
    )

    response = await loop._handle_program_tool_rpc(
        connection,
        {
            **child.model_dump(mode="json"),
            "scope_id": scope_id,
            "executor_id": executor_id,
            "executor_instance_id": executor_instance_id,
        },
    )

    assert response["is_error"] is True
    assert response["outcome_certainty"] == "unknown"
    assert "not valid JSON" in response["text"]
    assert invocation.uncertain is True


@pytest.mark.asyncio
async def test_queued_program_sequence_is_settled_when_prior_outcome_becomes_unknown() -> None:
    definition = ARTIFACT_WRITE_TOOL.model_copy(update={"output_schema": None})
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=definition))
    plan = build_effective_program_plan(registry)
    descriptor = plan.tools[0]
    parent_call_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    scope_id = uuid.uuid4().hex
    executor_id = uuid.uuid4().hex
    executor_instance_id = uuid.uuid4().hex
    connection = SimpleNamespace(
        executor_id=executor_id,
        executor_instance_id=executor_instance_id,
    )
    invocation = _ProgramInvocation(
        context=SimpleNamespace(
            turn_id=uuid.uuid4().hex,
            current_turn_cycle_index=1,
            on_tool_call_callback=None,
            on_tool_result_callback=None,
        ),
        parent_call=ToolCall(
            call_id=parent_call_id,
            name="python_execute",
            arguments={},
            execution_scope_id=scope_id,
        ),
        connection=connection,
        program_context=ProgramRuntimeBinding(
            program_id=parent_call_id,
            execution_id=execution_id,
            scope_id=scope_id,
            expected_generation=1,
            description="Write one approved artifact.",
            tool_manifest_digest=plan.plan_hash,
            tools=plan.tools,
            executor_id=executor_id,
            executor_instance_id=executor_instance_id,
        ),
        program_plan=plan,
        max_calls=4,
        max_concurrency=1,
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop._program_invocations = {parent_call_id: invocation}
    loop._program_invocations_lock = asyncio.Lock()
    loop._get_tool_registry = lambda _context: registry
    loop._get_classified_tool_registry = lambda _context, current: current
    loop._record_execution_evidence = lambda *_args, **_kwargs: None
    loop._flush_events_incremental = AsyncMock()
    loop.persist_controller_tool_output = AsyncMock(
        side_effect=lambda _context, _call, result: result
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    dispatched: list[ToolCall] = []

    async def execute_child(_context: Any, child: ToolCall) -> ToolResult:
        dispatched.append(child)
        first_started.set()
        await release_first.wait()
        return ToolResult(
            output="The artifact write outcome is unknown.",
            is_error=True,
            metadata={"outcome_certainty": "unknown"},
        )

    loop.execute_controller_tool = AsyncMock(side_effect=execute_child)

    def params(sequence: int, filename: str) -> dict[str, Any]:
        child = ProgramToolCall(
            program_id=parent_call_id,
            execution_id=execution_id,
            kernel_generation=1,
            sequence=sequence,
            tool_id=descriptor.tool_id,
            schema_hash=descriptor.schema_hash,
            arguments={"filename": filename, "content": "sequence recovery test"},
        )
        return {
            **child.model_dump(mode="json"),
            "scope_id": scope_id,
            "executor_id": executor_id,
            "executor_instance_id": executor_instance_id,
        }

    first_params = params(1, "ptc-sequence-first.txt")
    second_params = params(2, "ptc-sequence-queued.txt")
    first_task = asyncio.create_task(loop._handle_program_tool_rpc(connection, first_params))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second_task = asyncio.create_task(loop._handle_program_tool_rpc(connection, second_params))
    for _ in range(100):
        if invocation.pending_children == 1:
            break
        await asyncio.sleep(0)
    assert invocation.pending_children == 1

    release_first.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    replay = await asyncio.wait_for(
        loop._handle_program_tool_rpc(connection, second_params),
        timeout=0.5,
    )

    assert first_result["outcome_certainty"] == "unknown"
    assert second_result["outcome_certainty"] == "unknown"
    assert "not dispatched" in second_result["text"]
    assert replay == second_result
    assert invocation.pending_children == 0
    assert invocation.active_children == 0
    assert len(dispatched) == 1
    assert loop.execute_controller_tool.await_count == 1


def test_tool_runtime_metadata_preserves_trusted_program_caller_marker() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._tool_runtime_metadata = lambda _context: {}
    loop.session_cache = SimpleNamespace(
        get_loaded_skill_snapshots=lambda _session_id: {},
        get_events_since_compaction=lambda _session_id, types: [],
    )
    program_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    parent_call_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    description = "Call one governed child tool."
    context = SimpleNamespace(
        session=SimpleNamespace(session_id=session_id, user_email="ptc-user"),
        pending_events=[],
        memory_policy=None,
    )
    call = ToolCall(
        call_id=uuid.uuid4().hex,
        name="memory_add",
        arguments={"content": "governed child"},
        runtime_metadata={
            "_program_caller": {
                "program_id": program_id,
                "execution_id": execution_id,
                "parent_call_id": parent_call_id,
                "description": description,
            }
        },
    )

    metadata = loop._tool_runtime_metadata_for_call(context, call)

    assert metadata["_program_caller"] == {
        "program_id": program_id,
        "execution_id": execution_id,
        "parent_call_id": parent_call_id,
        "description": description,
    }

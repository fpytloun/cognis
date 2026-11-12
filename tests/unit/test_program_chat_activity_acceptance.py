from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.realtime import tool_call_runtime_item, tool_result_runtime_item
from cognis.api.chat_v2.schemas import ToolCallTimelineItem
from cognis.core.agent_loop import AgentLoop, PauseWaiter, PendingPause, _ProgramInvocation
from cognis.core.legacy_approvals import resolve_legacy_approval
from cognis.core.program_plan import build_effective_program_plan
from cognis.executor.program_runtime import ProgramRuntime
from cognis.models.program import ProgramExecutionRequest, ProgramLimits, ProgramRuntimeBinding
from cognis.models.session import SessionEvent
from cognis.models.tool import ToolCall, ToolResult
from cognis.tools.executor.definitions import WEB_SEARCH_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry


@pytest.mark.asyncio
async def test_python_execution_nested_activity_reconciles_after_approval(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real Python kernel → controller RPC → Chat v2 projection path."""
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "ptc-state.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(definition=WEB_SEARCH_TOOL.model_copy(update={"output_schema": None}))
    )
    plan = build_effective_program_plan(registry)
    descriptor = plan.tools[0]

    session_id = f"session-{uuid.uuid4().hex}"
    parent_call_id = f"program-{uuid.uuid4().hex}"
    execution_id = f"execution-{uuid.uuid4().hex}"
    scope_id = f"scope-{uuid.uuid4().hex}"
    conversation_id = f"conversation-{uuid.uuid4().hex}"
    approval_id = f"approval-{uuid.uuid4().hex}"
    executor_id = runtime.executor_id
    executor_instance_id = f"instance-{uuid.uuid4().hex}"
    turn_id = f"turn-{uuid.uuid4().hex}"
    parent_call = ToolCall(
        call_id=parent_call_id,
        name="python_execute",
        arguments={"description": "Read a reference and report its summary."},
        execution_scope_id=scope_id,
    )
    binding = ProgramRuntimeBinding(
        program_id=parent_call_id,
        execution_id=execution_id,
        scope_id=scope_id,
        expected_generation=None,
        description="Read a reference and report its summary.",
        tool_manifest_digest=plan.plan_hash,
        tools=plan.tools,
        executor_id=executor_id,
        executor_instance_id=executor_instance_id,
    )
    approval_pending = asyncio.Event()
    approval_resolved = asyncio.Event()
    pause_waiter = PauseWaiter()
    submit_approval = AsyncMock()
    runtime_items: dict[str, ToolCallTimelineItem] = {}
    flushed_events: list[tuple[str, SessionEvent]] = []
    program_statuses: list[dict[str, Any]] = []

    async def send_program_status(status: dict[str, Any]) -> None:
        program_statuses.append(dict(status))
        assert runtime.update_status(status)

    connection = SimpleNamespace(
        executor_id=executor_id,
        executor_instance_id=executor_instance_id,
        send_program_status=send_program_status,
    )

    def _as_runtime_metadata(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return dict(value or {})

    async def on_tool_call(
        tool_name: str,
        call_id: str,
        arguments: dict[str, Any] | None,
        *,
        turn_cycle_index: int | None = None,
        runtime_metadata: Any = None,
    ) -> None:
        linkage = _as_runtime_metadata(runtime_metadata)
        runtime_items[call_id] = tool_call_runtime_item(
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            turn_id=turn_id,
            assistant_phase_index=2,
            turn_cycle_index=turn_cycle_index,
            timestamp="2026-01-01T00:00:00Z",
            parent_call_id=linkage.get("parent_call_id"),
            program_id=linkage.get("program_id"),
            kernel_generation=linkage.get("kernel_generation"),
        )

    async def on_tool_result(
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: dict[str, Any] | None,
        attachments: list[dict[str, Any]],
        file_diffs: list[dict[str, Any]],
        presentation: dict[str, Any] | None,
        *,
        turn_cycle_index: int | None = None,
        runtime_metadata: Any = None,
    ) -> None:
        linkage = _as_runtime_metadata(runtime_metadata)
        runtime_items[call_id] = tool_result_runtime_item(
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            result=result,
            is_error=is_error,
            duration_ms=duration_ms,
            evaluation=evaluation,
            attachments=attachments,
            file_diffs=file_diffs,
            turn_id=turn_id,
            assistant_phase_index=2,
            turn_cycle_index=turn_cycle_index,
            timestamp="2026-01-01T00:00:01Z",
            presentation=presentation,
            parent_call_id=linkage.get("parent_call_id"),
            program_id=linkage.get("program_id"),
            kernel_generation=linkage.get("kernel_generation"),
        )

    context = SimpleNamespace(
        turn_id=turn_id,
        current_turn_cycle_index=3,
        on_tool_call_callback=on_tool_call,
        on_tool_result_callback=on_tool_result,
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop._program_invocations = {
        parent_call_id: _ProgramInvocation(
            context=context,
            parent_call=parent_call,
            connection=connection,
            program_context=binding,
            program_plan=plan,
            max_calls=2,
            max_concurrency=1,
        )
    }
    loop._program_invocations_lock = asyncio.Lock()
    loop._get_tool_registry = lambda _context: registry
    loop._get_classified_tool_registry = lambda _context, current: current
    loop._record_execution_evidence = lambda *_args, **_kwargs: None

    async def flush_events(_context: Any, events: list[SessionEvent], *, reason: str) -> None:
        flushed_events.extend((reason, event) for event in events)

    loop._flush_events_incremental = flush_events
    loop.tool_router = SimpleNamespace(
        update_program_approval_status=lambda *_args, **_kwargs: None
    )
    loop.execute_controller_tool = AsyncMock(
        return_value=ToolResult(
            output="Approval is required.",
            is_error=True,
            metadata={
                "evaluation": {
                    "decision": "escalate",
                    "risk": "medium",
                    "reasoning": "The reference lookup needs approval.",
                    "call_id": approval_id,
                }
            },
        )
    )
    loop.persist_controller_tool_output = AsyncMock(
        side_effect=lambda _context, _child_call, result: result
    )

    async def wait_for_approval(
        result: ToolResult,
        child_call: ToolCall,
        _context: Any,
        _events: list[SessionEvent],
        publish_wait: Any,
    ) -> ToolResult:
        assert result.metadata is not None
        await publish_wait(
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
            turn_cycle_index=3,
        )
        pause_waiter.register(
            PendingPause(
                pause_id=approval_id,
                pause_type="escalation",
                conversation_id=conversation_id,
                context={"call_id": approval_id},
            )
        )
        approval_pending.set()
        await asyncio.wait_for(approval_resolved.wait(), timeout=3)
        resolution = await pause_waiter.wait(approval_id, timeout=3)
        assert resolution.decision == "approve"
        return ToolResult(output="Approved reference summary.", is_error=False, duration_ms=5)

    loop._handle_escalation = AsyncMock(side_effect=wait_for_approval)

    runtime_items[parent_call_id] = tool_call_runtime_item(
        session_id=session_id,
        call_id=parent_call_id,
        tool_name="python_execute",
        arguments=parent_call.arguments,
        turn_id=turn_id,
        assistant_phase_index=2,
        turn_cycle_index=3,
        timestamp="2026-01-01T00:00:00Z",
    )
    request = ProgramExecutionRequest(
        program_id=parent_call_id,
        execution_id=execution_id,
        scope_id=scope_id,
        description="Read a reference and report its summary.",
        code=(
            f"value = await cognis.tools.{descriptor.alias}(query='approval-gated reference')\n"
            "await cognis.emit(value.text)"
        ),
        tools=[descriptor.model_dump(mode="json")],
        output_schema={"type": "string"},
        limits=ProgramLimits(wall_seconds=10, max_calls=1, concurrency=1),
    )

    async def dispatch_child(child_call: Any) -> Any:
        from cognis.models.program import ProgramToolValue

        return ProgramToolValue.model_validate(
            await loop._handle_program_tool_rpc(
                connection,
                {
                    **child_call.model_dump(mode="json"),
                    "scope_id": scope_id,
                    "executor_id": executor_id,
                    "executor_instance_id": executor_instance_id,
                },
            )
        )

    execution = asyncio.create_task(runtime.execute(request, tool_call_handler=dispatch_child))
    try:
        await asyncio.wait_for(approval_pending.wait(), timeout=5)
        child_id = next(call_id for call_id in runtime_items if call_id != parent_call_id)
        pending_item = runtime_items[child_id]
        assert runtime_items[parent_call_id].status == "running"
        assert pending_item.status == "waiting"
        assert pending_item.evaluation is not None
        assert pending_item.evaluation["phase"] == "waiting_for_approval"
        assert pending_item.parent_call_id == parent_call_id
        assert pending_item.program_id == parent_call_id
        assert isinstance(pending_item.kernel_generation, int)
        kernel_generation = pending_item.kernel_generation
        assert [reason for reason, _event in flushed_events] == ["program_tool_call"]

        assert await resolve_legacy_approval(
            pause_waiter,
            approval_id,
            conversation_id=conversation_id,
            decision="approve",
            note="Approved the reference read.",
            submit=submit_approval,
        )
        submit_approval.assert_awaited_once_with(
            approval_id, "approve", "Approved the reference read."
        )
        approval_resolved.set()
        result = await asyncio.wait_for(execution, timeout=5)
        assert result.status == "completed", result.model_dump()
        assert result.result == "Approved reference summary."
        assert result.nested_admitted == result.nested_completed == 1
        assert [item["status"] for item in program_statuses] == [
            "awaiting_approval",
            "running",
        ]
        assert runtime_items[child_id].status == "complete"
        assert runtime_items[child_id].parent_call_id == parent_call_id
        assert runtime_items[child_id].program_id == parent_call_id
        assert runtime_items[child_id].kernel_generation == kernel_generation
        assert [reason for reason, _event in flushed_events] == [
            "program_tool_call",
            "program_tool_result",
        ]

        parent_result = json.dumps(
            {"status": result.status, "result": result.result},
            separators=(",", ":"),
        )
        runtime_items[parent_call_id] = tool_result_runtime_item(
            session_id=session_id,
            call_id=parent_call_id,
            tool_name="python_execute",
            result=parent_result,
            is_error=False,
            duration_ms=result.wall_ms,
            evaluation=None,
            attachments=[],
            file_diffs=[],
            turn_id=turn_id,
            assistant_phase_index=2,
            turn_cycle_index=3,
            timestamp="2026-01-01T00:00:02Z",
        )

        raw_events = [
            RawSessionEvent(
                store_id="intaris",
                session_id=session_id,
                seq=1,
                type="tool_call",
                data={
                    "call_id": parent_call_id,
                    "name": "python_execute",
                    "arguments": parent_call.arguments,
                    "turn_id": turn_id,
                    "assistant_phase_index": 2,
                    "turn_cycle_index": 3,
                },
            )
        ]
        raw_events.extend(
            RawSessionEvent(
                store_id="intaris",
                session_id=session_id,
                seq=index + 2,
                type=event.type,
                data=event.data,
            )
            for index, (_reason, event) in enumerate(flushed_events)
        )
        raw_events.append(
            RawSessionEvent(
                store_id="intaris",
                session_id=session_id,
                seq=len(raw_events) + 1,
                type="tool_result",
                data={
                    "call_id": parent_call_id,
                    "name": "python_execute",
                    "result": parent_result,
                    "is_error": False,
                    "turn_id": turn_id,
                    "assistant_phase_index": 2,
                    "turn_cycle_index": 3,
                },
            )
        )
        canonical = project_timeline(normalize_session_events(raw_events).events).timeline.items
        canonical_by_call = {
            item.call_id: item for item in canonical if isinstance(item, ToolCallTimelineItem)
        }
        assert set(canonical_by_call) == {parent_call_id, child_id}
        assert canonical_by_call[parent_call_id].status == "complete"
        reconciled_child = canonical_by_call[child_id]
        assert reconciled_child.id == pending_item.id == runtime_items[child_id].id
        assert reconciled_child.status == "complete"
        assert reconciled_child.stable is True
        assert reconciled_child.parent_call_id == parent_call_id
        assert reconciled_child.program_id == parent_call_id
        assert reconciled_child.kernel_generation == kernel_generation
    finally:
        approval_resolved.set()
        if not execution.done():
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
        await runtime.close()

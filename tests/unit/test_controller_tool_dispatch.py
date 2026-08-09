"""Characterization tests for the shared controller-side tool dispatcher."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.core.agent_loop import WORKFLOW_POLICY, AgentLoop, StepContext
from cognis.models.agent import AgentDefinition
from cognis.models.tool import NativeToolDefinition, ToolCall, ToolResult, ToolSource
from cognis.models.workflow import StepDefinition
from cognis.tools.registry import RegisteredTool, ToolRegistry


class _Router:
    def __init__(self) -> None:
        self.calls: list[tuple[ToolCall, object]] = []

    async def execute(
        self,
        tool_call: ToolCall,
        _session: object,
        _agent: object,
        _registry: object,
        executor: object,
        *,
        output_chunk_callback: object = None,
    ) -> ToolResult:
        del output_chunk_callback
        self.calls.append((tool_call, executor))
        return ToolResult(output="ok", metadata={"executor_id": "executor-b"})


@pytest.mark.asyncio
async def test_regular_agent_tool_uses_shared_dispatch_and_strips_target_executor() -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="read_file",
                description="Read a file.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                source=ToolSource(type="executor"),
                read_only=True,
            )
        )
    )
    router = _Router()
    loop = object.__new__(AgentLoop)
    loop.tool_router = router
    loop.providers = SimpleNamespace(executor=None)
    loop._resolve_target_connection = (  # type: ignore[method-assign]
        lambda **kwargs: "connection-b" if kwargs["target_executor_id"] == "executor-b" else None
    )
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
    )
    target = SimpleNamespace(
        executor_id="executor-b",
        executor_type="in_process",
        usable=True,
        state=SimpleNamespace(value="usable"),
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="fetch", type="run"),
        session=SimpleNamespace(
            session_id="session-1",
            user_email="user@example.com",
            parent_session_id=None,
            delegation_mode=None,
        ),
        conversation=SimpleNamespace(
            conversation_id="conversation-1",
            active_executor_generation=3,
            context=SimpleNamespace(type="task", ref="task-1", platform_data={}),
        ),
        agent=agent,
        executor_agent=agent,
        task_id="task-1",
        step_run_id="step-run-1",
        policy=WORKFLOW_POLICY,
        tool_registry=registry,
        executor_connection="connection-a",
        executor_pool=SimpleNamespace(
            by_id=lambda executor_id: target if executor_id == "executor-b" else None
        ),
        active_executor_id="executor-a",
        turn_id="turn-1",
    )

    result = await loop._execute_regular_tool(  # noqa: SLF001
        ctx,
        ToolCall(
            call_id="call-1",
            name="read_file",
            arguments={"path": "/tmp/input", "target_executor": "executor-b"},
        ),
    )

    assert result.output == "ok"
    assert len(router.calls) == 1
    routed_call, routed_executor = router.calls[0]
    assert routed_call.arguments == {"path": "/tmp/input"}
    assert routed_call.runtime_metadata["task_id"] == "task-1"
    assert routed_call.runtime_metadata["step_run_id"] == "step-run-1"
    assert routed_executor == "connection-b"


@pytest.mark.asyncio
async def test_shared_dispatch_rejects_target_executor_for_non_executor_tool() -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name="controller_read",
                description="Read controller state.",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="controller"),
                read_only=True,
            )
        )
    )
    router = _Router()
    loop = object.__new__(AgentLoop)
    loop.tool_router = router
    loop.providers = SimpleNamespace(executor=None)
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
    )
    ctx = StepContext(
        step_definition=StepDefinition(name="fetch", type="run"),
        session=SimpleNamespace(session_id="session-1", user_email="user@example.com"),
        conversation=SimpleNamespace(
            conversation_id="conversation-1",
            context=SimpleNamespace(type="task", ref="task-1", platform_data={}),
        ),
        agent=agent,
        tool_registry=registry,
        executor_connection="connection-a",
    )

    result = await loop.execute_controller_tool(
        ctx,
        ToolCall(
            call_id="call-2",
            name="controller_read",
            arguments={"target_executor": "executor-b"},
        ),
    )

    assert result.is_error is True
    assert "only supported on executor-routed tools" in result.output
    assert router.calls == []

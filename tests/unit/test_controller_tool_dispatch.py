"""Characterization tests for the shared controller-side tool dispatcher."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.agent_loop import WORKFLOW_POLICY, AgentLoop, StepContext
from cognis.models.agent import AgentDefinition
from cognis.models.tool import NativeToolDefinition, ToolCall, ToolResult, ToolSource
from cognis.models.workflow import StepDefinition
from cognis.tools.executor.definitions import ALL_EXECUTOR_TOOLS
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
        before_executor_send: object = None,
        after_executor_send: object = None,
        workflow_step_scope: object = None,
        workflow_executor_id: str | None = None,
        turn_id: str | None = None,
    ) -> ToolResult:
        del output_chunk_callback, before_executor_send, after_executor_send, turn_id
        self.calls.append((tool_call, executor))
        return ToolResult(output="ok", metadata={"executor_id": "executor-b"})


@pytest.mark.asyncio
async def test_program_target_checks_actual_connection_before_prepare() -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=next(tool for tool in ALL_EXECUTOR_TOOLS if tool.name == "python_execute")
        )
    )
    router = _Router()
    loop = object.__new__(AgentLoop)
    loop.tool_router = router
    loop.providers = SimpleNamespace(executor=None)
    loop.session_manager = SimpleNamespace(session_factory=None)
    loop.session_cache = SimpleNamespace(
        get_loaded_skill_snapshots=lambda _: {},
        get_events_since_compaction=lambda *_, **__: [],
    )
    target_connection = SimpleNamespace(
        executor_id="executor-b", prepare_program_runtime=AsyncMock()
    )
    loop._resolve_target_connection = lambda **_: target_connection
    target = SimpleNamespace(
        executor_id="executor-b",
        executor_type="in_process",
        usable=True,
        state=SimpleNamespace(value="usable"),
    )
    seen = []

    async def program_policy(connection):
        seen.append(connection.executor_id)
        return connection.executor_id == "executor-a"

    agent = AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent")
    ctx = StepContext(
        step_definition=StepDefinition(name="run", type="run"),
        session=SimpleNamespace(
            session_id="session-1",
            intaris_session_id="session-1",
            user_email="user@example.com",
            parent_session_id=None,
            delegation_mode=None,
        ),
        conversation=SimpleNamespace(
            conversation_id="conversation-1",
            context=SimpleNamespace(type="task", ref="task-1", platform_data={}),
        ),
        agent=agent,
        tool_registry=registry,
        executor_connection=SimpleNamespace(executor_id="executor-a"),
        executor_pool=SimpleNamespace(by_id=lambda executor_id: target),
        active_executor_id="executor-a",
        program_policy=program_policy,
    )
    result = await loop.execute_controller_tool(
        ctx,
        ToolCall(
            call_id="call-ptc",
            name="python_execute",
            arguments={
                "target_executor": "executor-b",
                "description": "Check the selected executor policy",
                "code": "print(1)",
            },
        ),
    )
    assert result.is_error and result.metadata["code"] == "program_runtime_disabled"
    assert seen == ["executor-b"]
    target_connection.prepare_program_runtime.assert_not_awaited()
    assert not router.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id,tool_name",
    [("gpt-6-astra", "edit"), ("gpt-5-codex", "write"), ("claude-sonnet-4-5", "apply_patch")],
)
async def test_dispatch_rejects_incompatible_editor(model_id: str, tool_name: str) -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=NativeToolDefinition(
                name=tool_name,
                description="Edit a file.",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                read_only=False,
            )
        )
    )
    router = _Router()
    loop = object.__new__(AgentLoop)
    loop.tool_router = router
    loop.session_cache = None
    loop.providers = SimpleNamespace(executor=None)
    loop.session_manager = SimpleNamespace(session_factory=None)
    agent = AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent")
    ctx = StepContext(
        step_definition=StepDefinition(name="edit", type="run"),
        session=SimpleNamespace(
            session_id="session-1",
            user_email="user@example.com",
            parent_session_id=None,
            delegation_mode=None,
        ),
        conversation=SimpleNamespace(conversation_id="conversation-1", context=None),
        agent=agent,
        executor_agent=agent,
        task_id="task-1",
        step_run_id="step-run-1",
        policy=WORKFLOW_POLICY,
        tool_registry=registry,
        current_model=model_id,
    )
    result = await loop.execute_controller_tool(
        ctx, ToolCall(call_id="call-1", name=tool_name, arguments={})
    )
    assert result.is_error
    assert "unavailable for the current model" in result.output
    assert not router.calls


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
    loop.session_manager = SimpleNamespace(session_factory=None)
    loop.session_cache = SimpleNamespace(
        get_loaded_skill_snapshots=lambda _: {},
        get_events_since_compaction=lambda *_, **__: [],
    )
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
            intaris_session_id="session-1",
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
    retried = await loop.execute_controller_tool(
        ctx,
        ToolCall(
            call_id="call-1",
            name="read_file",
            arguments={"path": "/tmp/input", "target_executor": "executor-b"},
        ),
        approval_call_id="approved-evaluation",
    )
    assert retried.output == "ok"
    retry_call, retry_executor = router.calls[1]
    assert retry_call.arguments == routed_call.arguments
    assert retry_executor == routed_executor
    assert retry_call.runtime_metadata["approval_call_id"] == "approved-evaluation"
    assert "approval_call_id" not in routed_call.runtime_metadata


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
    loop.session_manager = SimpleNamespace(session_factory=None)
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

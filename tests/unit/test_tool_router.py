from __future__ import annotations

import asyncio

import pytest

from cognis.core.tool_router import ToolRoute, ToolRouter
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.session import SessionModel
from cognis.models.tool import Permission, ToolCall, ToolDefinition, ToolResult, ToolSource
from cognis.tools.registry import RegisteredTool, ToolRegistry


class _Guardrails:
    def __init__(self) -> None:
        self.evaluate_calls = 0
        self.mcp_calls = 0

    async def evaluate(
        self, session_id: str, tool_name: str, arguments: dict, context: dict
    ) -> object:
        del session_id, tool_name, arguments, context
        self.evaluate_calls += 1
        return type(
            "Evaluation",
            (),
            {
                "decision": "approve",
                "reasoning": None,
                "risk": None,
                "path": None,
                "latency_ms": 0,
                "call_id": "eval_mock",
            },
        )()

    async def call_mcp_tool(
        self, session_id: str, server_name: str, tool_name: str, arguments: dict
    ) -> ToolResult:
        del session_id, server_name, tool_name, arguments
        self.mcp_calls += 1
        return ToolResult(output="remote result")


class _Executor:
    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls = 0
        self.cancelled: list[str] = []
        self.result = result or ToolResult(output="local result")

    async def tool_execute(
        self, tool_call: ToolCall, timeout_seconds: int | None = None
    ) -> ToolResult:
        del tool_call, timeout_seconds
        self.calls += 1
        return self.result

    async def cancel_call(self, call_id: str) -> None:
        self.cancelled.append(call_id)


class _SlowExecutor(_Executor):
    async def tool_execute(
        self, tool_call: ToolCall, timeout_seconds: int | None = None
    ) -> ToolResult:
        del tool_call, timeout_seconds
        await asyncio.sleep(0.05)
        return ToolResult(output="too slow")


def _registry_with_result_limit(max_result_size: int = 20) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="filesystem/read_file",
                description="local",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="local_mcp", server_name="filesystem"),
                timeout_seconds=1,
                max_result_size=max_result_size,
            )
        )
    )
    return registry


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="delegate",
                description="delegate",
                parameters={"type": "object", "properties": {"task": {"type": "string"}}},
                source=ToolSource(type="builtin"),
                category="orchestration",
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="github/search",
                description="remote",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="intaris_mcp", server_name="github"),
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="filesystem/read_file",
                description="local",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="local_mcp", server_name="filesystem"),
                timeout_seconds=1,
                max_result_size=20,
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="shell",
                description="shell",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="builtin"),
                non_bypassable=True,
            )
        )
    )
    return registry


def _agent(tool_permissions: dict[str, Permission] | None = None) -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-a",
        owner_email="user@example.com",
        name="Agent A",
        tools={},
        permissions=AgentPermissions(tool_permissions=tool_permissions or {"*": Permission.ALLOW}),
    )


def _session() -> SessionModel:
    return SessionModel(
        session_id="session-a",
        conversation_id="conv-a",
        user_email="user@example.com",
        agent_id="agent-a",
    )


def test_tool_router_classifies_routes() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=["shell"])
    registry = _registry()

    assert router.classify("delegate", registry) is ToolRoute.ORCHESTRATION
    assert router.classify("github/search", registry) is ToolRoute.INTARIS_MCP
    assert router.classify("filesystem/read_file", registry) is ToolRoute.LOCAL
    assert router.classify("missing", registry) is ToolRoute.UNKNOWN


@pytest.mark.asyncio
async def test_tool_router_dispatches_intaris_mcp() -> None:
    guardrails = _Guardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=["shell"])

    result = await router.execute(
        ToolCall(call_id="1", name="github/search", arguments={}),
        _session(),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert guardrails.mcp_calls == 1
    assert 'trust="untrusted"' in result.output


@pytest.mark.asyncio
async def test_tool_router_enforces_non_bypassable_guardrails() -> None:
    guardrails = _Guardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=["shell"])

    await router.execute(
        ToolCall(call_id="2", name="shell", arguments={}),
        _session(),
        _agent({"*": Permission.ALLOW}),
        _registry(),
        _Executor(),
    )

    assert guardrails.evaluate_calls == 1


@pytest.mark.asyncio
async def test_tool_router_truncates_and_wraps_local_results() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    # Output exceeds max_result_size (20 chars) but middle-truncation has
    # a minimum size of 500 chars.  Use a larger output and a larger limit
    # to verify the middle-truncation path works.
    executor = _Executor(result=ToolResult(output="x" * 2000))

    result = await router.execute(
        ToolCall(call_id="3", name="filesystem/read_file", arguments={}),
        _session(),
        _agent(),
        _registry_with_result_limit(600),
        executor,
    )

    assert executor.calls == 1
    assert result.metadata is not None
    assert result.metadata["wrapped"] is True
    assert result.metadata["truncated"] is True
    assert result.metadata["evaluation"]["decision"] == "approve"
    assert "middle truncated" in result.output


@pytest.mark.asyncio
async def test_tool_router_times_out_and_cancels() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    executor = _SlowExecutor()
    registry = _registry()
    registry.get("filesystem/read_file").definition.timeout_seconds = 0  # type: ignore[union-attr]

    result = await router.execute(
        ToolCall(call_id="4", name="filesystem/read_file", arguments={}),
        _session(),
        _agent(),
        registry,
        executor,
    )

    assert executor.cancelled == ["4"]
    assert "Tool execution timed" in result.output

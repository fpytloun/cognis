from __future__ import annotations

import asyncio

import pytest

from cognis.core.tool_router import ToolRoute, ToolRouter
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.session import SessionModel
from cognis.models.tool import (
    Permission,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolSource,
    sanitize_mcp_tool_name,
)
from cognis.tools.registry import RegisteredTool, ToolRegistry


class _Guardrails:
    def __init__(self) -> None:
        self.evaluate_calls = 0
        self.mcp_calls = 0
        self.last_mcp_call: tuple[str, str] | None = None
        self.mcp_result = ToolResult(output="remote result")

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
        del session_id, arguments
        self.mcp_calls += 1
        self.last_mcp_call = (server_name, tool_name)
        return self.mcp_result


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


class _RemoteExecutor(_Executor):
    def __init__(self, result: ToolResult | None = None) -> None:
        super().__init__(result=result)
        self.executor_id = "remote-exec"
        self.executor_type = "websocket"


def _registry_with_result_limit(max_result_size: int = 20) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name=sanitize_mcp_tool_name("filesystem", "read_file"),
                description="local",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(
                    type="local_mcp", server_name="filesystem", raw_tool_name="read_file"
                ),
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
                name=sanitize_mcp_tool_name("github", "search"),
                description="remote",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search"),
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name=sanitize_mcp_tool_name("filesystem", "read_file"),
                description="local",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(
                    type="local_mcp", server_name="filesystem", raw_tool_name="read_file"
                ),
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
    assert (
        router.classify(sanitize_mcp_tool_name("github", "search"), registry)
        is ToolRoute.INTARIS_MCP
    )
    assert (
        router.classify(sanitize_mcp_tool_name("filesystem", "read_file"), registry)
        is ToolRoute.LOCAL
    )
    assert router.classify("missing", registry) is ToolRoute.UNKNOWN


@pytest.mark.asyncio
async def test_tool_router_dispatches_intaris_mcp() -> None:
    guardrails = _Guardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=["shell"])

    result = await router.execute(
        ToolCall(call_id="1", name=sanitize_mcp_tool_name("github", "search"), arguments={}),
        _session(),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert guardrails.mcp_calls == 1
    assert guardrails.last_mcp_call == ("github", "search")
    assert 'trust="untrusted"' in result.output


@pytest.mark.asyncio
async def test_tool_router_dispatches_intaris_mcp_using_raw_tool_name() -> None:
    guardrails = _Guardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=[])
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name=sanitize_mcp_tool_name("github", "search/issues"),
                description="remote",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(
                    type="intaris_mcp",
                    server_name="github",
                    raw_tool_name="search/issues",
                ),
            )
        )
    )

    await router.execute(
        ToolCall(
            call_id="raw", name=sanitize_mcp_tool_name("github", "search/issues"), arguments={}
        ),
        _session(),
        _agent(),
        registry,
        _Executor(),
    )

    assert guardrails.last_mcp_call == ("github", "search/issues")


@pytest.mark.asyncio
async def test_tool_router_wraps_intaris_mcp_escalation_metadata() -> None:
    guardrails = _Guardrails()
    guardrails.mcp_result = ToolResult(
        output="This tool call has been escalated for review (call_id: call-123).",
        is_error=True,
        metadata={
            "decision": "escalate",
            "call_id": "call-123",
            "reasoning": "Needs approval",
            "risk": "high",
            "latency_ms": 42,
        },
    )
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=[])

    result = await router.execute(
        ToolCall(call_id="esc-1", name=sanitize_mcp_tool_name("github", "search"), arguments={}),
        _session(),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert result.metadata is not None
    assert result.metadata["decision"] == "escalate"
    assert result.metadata["call_id"] == "call-123"
    assert result.metadata["evaluation"] == {
        "decision": "escalate",
        "reasoning": "Needs approval",
        "source": "guardrails",
        "risk": "high",
        "path": None,
        "latency_ms": 42,
        "call_id": "call-123",
    }


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
        ToolCall(call_id="3", name=sanitize_mcp_tool_name("filesystem", "read_file"), arguments={}),
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
async def test_tool_router_executes_registered_builtin_handler_locally() -> None:
    async def builtin_handler(arguments: dict[str, object], context: object) -> object:
        del arguments
        return {"executor_type": context.executor_handle.executor_type}

    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    executor = _RemoteExecutor()
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="get_current_datetime",
                description="datetime",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="builtin"),
                category="datetime",
                read_only=True,
            ),
            handler=builtin_handler,
        )
    )

    result = await router.execute(
        ToolCall(call_id="builtin-1", name="get_current_datetime", arguments={}),
        _session(),
        _agent(),
        registry,
        executor,
    )

    assert executor.calls == 0
    assert '"executor_type": "websocket"' in result.output


@pytest.mark.asyncio
async def test_tool_router_times_out_and_cancels() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    executor = _SlowExecutor()
    registry = _registry()
    registry.get(sanitize_mcp_tool_name("filesystem", "read_file")).definition.timeout_seconds = 0  # type: ignore[union-attr]

    result = await router.execute(
        ToolCall(call_id="4", name=sanitize_mcp_tool_name("filesystem", "read_file"), arguments={}),
        _session(),
        _agent(),
        registry,
        executor,
    )

    assert executor.cancelled == ["4"]
    assert "Tool execution timed" in result.output


@pytest.mark.asyncio
async def test_tool_router_logs_do_not_include_tool_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])

    await router.execute(
        ToolCall(
            call_id="5",
            name=sanitize_mcp_tool_name("filesystem", "read_file"),
            arguments={"secret": "top-secret-value"},
        ),
        _session(),
        _agent(),
        _registry_with_result_limit(600),
        _Executor(),
    )

    assert "top-secret-value" not in caplog.text

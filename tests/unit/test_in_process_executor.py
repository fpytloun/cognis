from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.models.tool import (
    ExecutorCapabilities,
    ExecutorConfig,
    ExecutorHandle,
    MCPServerConfig,
    ToolCall,
    ToolDefinition,
    ToolSource,
    sanitize_mcp_tool_name,
)
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.executor.in_process import (
    InProcessExecutorConnection,
    InProcessExecutorProvider,
)
from cognis.security import create_password_hasher
from cognis.store.models import Agent, User
from cognis.tools.builtin.system import LIST_AGENTS_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _mcp_server_script() -> str:
    return """
from __future__ import annotations
import json
import sys


def read_message() -> dict:
    header = bytearray()
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise EOFError
        header.extend(line)
        if header.endswith(b"\\r\\n\\r\\n"):
            break
    content_length = 0
    for raw_header in header.decode("utf-8").split("\\r\\n"):
        if raw_header.lower().startswith("content-length:"):
            content_length = int(raw_header.split(":", 1)[1].strip())
            break
    body = sys.stdin.buffer.read(content_length)
    return json.loads(body.decode("utf-8"))


def write_message(payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


while True:
    try:
        request = read_message()
    except EOFError:
        break
    # Skip notifications (no "id" field) — e.g. notifications/initialized
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        write_message({"jsonrpc": "2.0", "id": request["id"], "result": {"capabilities": {}}})
    elif method == "tools/list":
        write_message(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "inspect",
                            "description": "Inspect something",
                            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
                        }
                    ]
                },
            }
        )
    elif method == "tools/call":
        write_message(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": "inspected"}]},
            }
        )
    else:
        write_message({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unknown method"}})
"""


@pytest.mark.asyncio
async def test_in_process_executor_lists_and_executes_system_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    config = load_config()
    password_hasher = create_password_hasher()
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    async with session_factory() as session:
        session.add(User(email="user@example.com", name="User", password_hash="hash", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-a",
                owner_email="user@example.com",
                name="Agent A",
                description="Test agent",
                status="active",
            )
        )
        await session.commit()

    provider = InProcessExecutorProvider(session_factory=session_factory)
    config_model = ExecutorConfig(
        executor_id="exec-1",
        tools=[LIST_AGENTS_TOOL],
        metadata={"user_email": "user@example.com"},
    )

    handle = await provider.spawn(config_model)
    connection = await provider.get_executor(handle)
    listed_tools = await connection.list_tools()
    result = await connection.tool_execute(
        ToolCall(call_id="call-1", name="list_agents", arguments={})
    )
    await provider.cleanup()
    await engine.dispose()

    assert listed_tools[0]["name"] == "list_agents"
    assert '"agent_id": "agent-a"' in result.output


@pytest.mark.asyncio
async def test_list_agents_returns_empty_without_user_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    config = load_config()
    password_hasher = create_password_hasher()
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    async with session_factory() as session:
        session.add(User(email="user@example.com", name="User", password_hash="hash", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-a",
                owner_email="user@example.com",
                name="Agent A",
                description="Test agent",
                status="active",
            )
        )
        await session.commit()

    provider = InProcessExecutorProvider(session_factory=session_factory)
    handle = await provider.spawn(
        ExecutorConfig(executor_id="exec-empty", tools=[LIST_AGENTS_TOOL])
    )
    connection = await provider.get_executor(handle)
    result = await connection.tool_execute(
        ToolCall(call_id="call-empty", name="list_agents", arguments={})
    )

    await provider.cleanup()
    await engine.dispose()

    assert result.output == "[]"


@pytest.mark.asyncio
async def test_executor_rejects_duplicate_mcp_server_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    config = load_config()
    password_hasher = create_password_hasher()
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    provider = InProcessExecutorProvider(session_factory=session_factory)

    with pytest.raises(ValueError):
        await provider.spawn(
            ExecutorConfig(
                executor_id="exec-dup",
                tools=[],
                mcp_servers=[
                    {"name": "dup", "command": "python", "args": []},
                    {"name": "dup", "command": "python", "args": []},
                ],
            )
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_executor_connection_trips_circuit_breaker_after_failures() -> None:
    async def failing_handler(arguments: dict[str, object], context: object) -> str:
        del arguments, context
        raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="explode",
                description="explode",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="builtin"),
            ),
            handler=failing_handler,
        )
    )
    connection = InProcessExecutorConnection(
        ExecutorHandle(
            executor_id="exec",
            executor_type="in_process",
            capabilities=ExecutorCapabilities(tools=["explode"]),
        ),
        registry,
        CircuitBreaker(failure_threshold=2, recovery_timeout=30),
    )

    await connection.tool_execute(ToolCall(call_id="1", name="explode", arguments={}))
    await connection.tool_execute(ToolCall(call_id="2", name="explode", arguments={}))

    assert connection.breaker.state == "open"


@pytest.mark.asyncio
async def test_in_process_executor_discovers_local_mcp_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(_mcp_server_script())

    config = load_config()
    password_hasher = create_password_hasher()
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    provider = InProcessExecutorProvider(session_factory=session_factory)

    handle = await provider.spawn(
        ExecutorConfig(
            executor_id="exec-mcp",
            mcp_servers=[
                MCPServerConfig(
                    name="filesystem",
                    command=sys.executable,
                    args=[str(server)],
                    timeout_seconds=2,
                )
            ],
        )
    )
    connection = await provider.get_executor(handle)
    tools = await connection.list_tools()
    result = await connection.tool_execute(
        ToolCall(
            call_id="call-mcp",
            name=sanitize_mcp_tool_name("filesystem", "inspect"),
            arguments={},
        )
    )

    await provider.cleanup()
    await engine.dispose()

    assert any(tool["name"] == sanitize_mcp_tool_name("filesystem", "inspect") for tool in tools)
    assert result.output == "inspected"

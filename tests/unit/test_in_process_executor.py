from __future__ import annotations

import asyncio
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
    ToolResult,
    ToolSource,
    sanitize_mcp_tool_name,
)
from cognis.models.tool import (
    NativeToolDefinition as ToolDefinition,
)
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.executor.in_process import (
    InProcessExecutorConnection,
    InProcessExecutorProvider,
)
from cognis.security import create_password_hasher
from cognis.store.models import Agent, User
from cognis.tools.builtin.system import LIST_AGENTS_TOOL
from cognis.tools.executor.browser.manager import (
    BROWSER_MANAGER_KEY,
    BrowserLifecycleError,
    BrowserManager,
    BrowserSession,
    BrowserSessionOwner,
)
from cognis.tools.executor.definitions import BASH_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _contract_call(
    connection: InProcessExecutorConnection,
    *,
    call_id: str,
    name: str,
    arguments: dict[str, object],
    contract_hash: str | None = None,
    runtime_metadata: dict[str, object] | None = None,
    execution_scope_id: str | None = None,
) -> ToolCall:
    registered = connection.registry.get(name)
    assert registered is not None
    descriptor = registered.definition.descriptor
    assert descriptor is not None
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments=arguments,
        runtime_metadata={
            **(runtime_metadata or {}),
            "tool_contract_hash": contract_hash or descriptor.schema_hash,
        },
        execution_scope_id=execution_scope_id,
    )


def _mcp_server_script() -> str:
    # The MCP SDK uses newline-delimited JSON (one JSON object per line),
    # not Content-Length framing.
    return """
from __future__ import annotations
import json
import sys


def read_message() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return json.loads(line.strip())


def write_message(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


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
        write_message({"jsonrpc": "2.0", "id": request["id"], "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "test-server", "version": "0.1.0"},
        }})
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
        _contract_call(
            connection,
            call_id="call-1",
            name="list_agents",
            arguments={},
        )
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
        _contract_call(
            connection,
            call_id="call-empty",
            name="list_agents",
            arguments={},
        )
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

    await connection.tool_execute(
        _contract_call(connection, call_id="1", name="explode", arguments={})
    )
    await connection.tool_execute(
        _contract_call(connection, call_id="2", name="explode", arguments={})
    )

    assert connection.breaker.state == "open"


@pytest.mark.asyncio
async def test_executor_connection_preserves_browser_lifecycle_error_metadata() -> None:
    async def failing_handler(arguments: dict[str, object], context: object) -> str:
        del arguments, context
        raise BrowserLifecycleError(
            "browser_unauthorized",
            "Browser session belongs to another active execution.",
        )

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="browser_probe",
                description="probe",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
            ),
            handler=failing_handler,
        )
    )
    connection = InProcessExecutorConnection(
        ExecutorHandle(
            executor_id="exec",
            executor_type="in_process",
            capabilities=ExecutorCapabilities(tools=["browser_probe"]),
        ),
        registry,
        CircuitBreaker(),
    )

    result = None
    for index in range(6):
        result = await connection.tool_execute(
            _contract_call(
                connection,
                call_id=str(index),
                name="browser_probe",
                arguments={},
            )
        )
    assert result is not None
    assert result.is_error is True
    assert result.metadata["browser_lifecycle_error"] == "browser_unauthorized"
    assert connection.breaker.state == "closed"


@pytest.mark.asyncio
async def test_executor_connection_cleans_terminal_browser_scope() -> None:
    closed: list[bool] = []

    class _Context:
        async def close(self) -> None:
            closed.append(True)

    manager = BrowserManager()
    owner = BrowserSessionOwner(
        execution_scope_id="child-session",
        session_id="child-session",
        user_email="user@example.com",
        parent_session_id="parent-session",
    )
    manager._sessions["browser-session"] = BrowserSession(  # noqa: SLF001
        session_id="browser-session",
        context=_Context(),
        page=object(),
        owner=owner,
    )
    connection = InProcessExecutorConnection(
        ExecutorHandle(
            executor_id="exec",
            executor_type="in_process",
            capabilities=ExecutorCapabilities(),
        ),
        ToolRegistry(),
        CircuitBreaker(failure_threshold=2, recovery_timeout=30),
        runtime_metadata={BROWSER_MANAGER_KEY: manager},
    )

    result = await connection.rpc_call(
        "browser.session_terminal",
        {
            "owner": {
                "execution_scope_id": "child-session",
                "session_id": "child-session",
                "user_email": "user@example.com",
                "parent_session_id": "parent-session",
            }
        },
    )

    assert result == {"closed": 1, "complete": True}
    assert closed == [True]


@pytest.mark.asyncio
async def test_terminal_rpc_reports_retryable_close_failure() -> None:
    class _Context:
        async def close(self) -> None:
            raise RuntimeError("close failed")

    manager = BrowserManager()
    owner = BrowserSessionOwner(
        execution_scope_id="child-session",
        session_id="child-session",
        user_email="user@example.com",
        parent_session_id="parent-session",
    )
    manager._sessions["browser-session"] = BrowserSession(  # noqa: SLF001
        session_id="browser-session",
        context=_Context(),
        page=object(),
        owner=owner,
        profile_mode="persistent_local",
        profile_id="profile",
    )
    connection = InProcessExecutorConnection(
        ExecutorHandle(
            executor_id="exec",
            executor_type="in_process",
            capabilities=ExecutorCapabilities(),
        ),
        ToolRegistry(),
        CircuitBreaker(failure_threshold=2, recovery_timeout=30),
        runtime_metadata={BROWSER_MANAGER_KEY: manager},
    )

    with pytest.raises(BrowserLifecycleError) as exc_info:
        await connection.rpc_call(
            "browser.session_terminal",
            {
                "owner": {
                    "execution_scope_id": "child-session",
                    "session_id": "child-session",
                    "user_email": "user@example.com",
                    "parent_session_id": "parent-session",
                }
            },
        )

    assert exc_info.value.code == "browser_session_close_failed"
    assert "browser-session" in manager._closing_sessions  # noqa: SLF001
    with pytest.raises(BrowserLifecycleError) as profile_exc:
        await manager._reserve_profile_id("profile", owner=owner)  # noqa: SLF001
    assert profile_exc.value.code == "browser_profile_locked"


@pytest.mark.asyncio
async def test_cancel_retains_manager_after_permanent_forced_teardown_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    config = load_config()
    _, engine, session_factory, _ = await bootstrap_runtime(config, create_password_hasher())
    provider = InProcessExecutorProvider(session_factory=session_factory)
    handle = await provider.spawn(
        ExecutorConfig(
            executor_id="exec-browser-cleanup",
            metadata={"browser": {"enabled": True}},
        )
    )
    connection = await provider.get_executor(handle)
    manager = connection.runtime_metadata[BROWSER_MANAGER_KEY]
    manager._reserved_profile_ids["profile"] = None  # noqa: SLF001

    async def _permanent_teardown_failure() -> list[BaseException]:
        return [RuntimeError("forced teardown failed")]

    monkeypatch.setattr(manager, "_force_runtime_teardown", _permanent_teardown_failure)

    await provider.cancel(handle)

    assert manager in provider._browser_cleanup_retainer.managers  # noqa: SLF001
    assert manager._reserved_profile_ids == {"profile": None}  # noqa: SLF001
    tasks = list(provider._browser_cleanup_retainer._tasks.values())  # noqa: SLF001
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_retains_browser_manager_when_client_cleanup_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    config = load_config()
    _, engine, session_factory, _ = await bootstrap_runtime(config, create_password_hasher())
    provider = InProcessExecutorProvider(session_factory=session_factory)
    handle = await provider.spawn(
        ExecutorConfig(
            executor_id="exec-browser-cancelled",
            metadata={"browser": {"enabled": True}},
        )
    )
    connection = await provider.get_executor(handle)
    manager = connection.runtime_metadata[BROWSER_MANAGER_KEY]
    manager._reserved_profile_ids["profile"] = None  # noqa: SLF001

    async def _permanent_teardown_failure() -> list[BaseException]:
        return [RuntimeError("forced teardown failed")]

    monkeypatch.setattr(manager, "_force_runtime_teardown", _permanent_teardown_failure)

    async def _cancelled_client_cleanup(_: object) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "cognis.providers.executor.in_process._close_clients",
        _cancelled_client_cleanup,
    )

    with pytest.raises(asyncio.CancelledError):
        await provider.cancel(handle)

    assert manager in provider._browser_cleanup_retainer.managers  # noqa: SLF001
    tasks = list(provider._browser_cleanup_retainer._tasks.values())  # noqa: SLF001
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await engine.dispose()


@pytest.mark.asyncio
async def test_in_process_background_bash_completion_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    config = load_config()
    password_hasher = create_password_hasher()
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    provider = InProcessExecutorProvider(session_factory=session_factory)
    completed: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()

    async def on_completed(executor_id: str, status: dict[str, object]) -> None:
        completed.append((executor_id, status))
        done.set()

    provider.register_background_shell_completed_callback(on_completed)
    handle = await provider.spawn(
        ExecutorConfig(
            executor_id="exec-bash",
            tools=[BASH_TOOL],
            metadata={
                "conversation_id": "conv-1",
                "session_id": "sess-1",
                "agent_id": "agent-1",
            },
        )
    )
    connection = await provider.get_executor(handle)

    result = await connection.tool_execute(
        _contract_call(
            connection,
            call_id="call-bash",
            name="bash",
            arguments={
                "command": "printf done",
                "description": "quick background command",
                "run_in_background": True,
                "timeout": 100,
            },
            runtime_metadata={
                "turn_id": "turn-1",
                "tool_call_id": "call-bash",
                "runtime_access": {
                    "conversation_id": "conv-1",
                    "session_id": "sess-1",
                    "agent_id": "agent-1",
                },
            },
            execution_scope_id="scope-1",
        )
    )

    await asyncio.wait_for(done.wait(), timeout=5)
    await provider.cleanup()
    await engine.dispose()

    assert result.is_error is False
    assert completed[0][0] == "exec-bash"
    status = completed[0][1]
    assert status["shell_id"]
    assert status["status"] == "completed"
    assert status["description"] == "quick background command"
    assert status["conversation_id"] == "conv-1"
    assert status["call_id"] == "call-bash"


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
                    timeout_seconds=10,
                )
            ],
        )
    )
    connection = await provider.get_executor(handle)
    tools = await connection.list_tools()
    result = await connection.tool_execute(
        _contract_call(
            connection,
            call_id="call-mcp",
            name=sanitize_mcp_tool_name("filesystem", "inspect"),
            arguments={},
        )
    )

    await provider.cleanup()
    await engine.dispose()

    assert any(tool["name"] == sanitize_mcp_tool_name("filesystem", "inspect") for tool in tools)
    assert result.output == "inspected"


@pytest.mark.asyncio
async def test_in_process_executor_rejects_malformed_arguments_before_handler() -> None:
    called = False

    async def handler(_arguments: dict[str, object], _context: object) -> str:
        nonlocal called
        called = True
        return "unexpected"

    definition = ToolDefinition(
        name="requires_value",
        description="Require a value.",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        source=ToolSource(type="executor"),
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=definition, handler=handler))
    connection = InProcessExecutorConnection(
        ExecutorHandle(
            executor_id="exec-validation",
            executor_type="in_process",
            state="ready",
            capabilities=ExecutorCapabilities(),
        ),
        registry,
        CircuitBreaker(),
    )

    result = await connection.tool_execute(
        _contract_call(
            connection,
            call_id="invalid",
            name="requires_value",
            arguments={},
        )
    )

    assert result.is_error is True
    assert result.metadata["code"] == "invalid_tool_arguments"
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_hash", [None, "sha256:stale"])
async def test_in_process_executor_rejects_missing_or_mismatched_contract_hash(
    contract_hash: str | None,
) -> None:
    definition = ToolDefinition(
        name="contract_probe",
        description="Probe contract enforcement.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="executor"),
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=definition,
            handler=lambda _arguments, _context: "unexpected",
        )
    )
    connection = InProcessExecutorConnection(
        ExecutorHandle(
            executor_id="exec-contract",
            executor_type="in_process",
            state="ready",
            capabilities=ExecutorCapabilities(),
        ),
        registry,
        CircuitBreaker(),
    )
    runtime_metadata = {} if contract_hash is None else {"tool_contract_hash": contract_hash}

    result = await connection.tool_execute(
        ToolCall(
            call_id="contract",
            name="contract_probe",
            arguments={},
            runtime_metadata=runtime_metadata,
        )
    )

    assert result.is_error is True
    assert result.metadata["code"] == "tool_contract_mismatch"


@pytest.mark.asyncio
async def test_in_process_executor_internal_probe_remains_private_and_executable() -> None:
    async def internal_handler(
        arguments: dict[str, object],
        _context: object,
    ) -> ToolResult:
        return ToolResult(output=str(arguments["project_hint"]))

    connection = InProcessExecutorConnection(
        ExecutorHandle(
            executor_id="exec-internal",
            executor_type="in_process",
            state="ready",
            capabilities=ExecutorCapabilities(),
        ),
        ToolRegistry(),
        CircuitBreaker(),
        internal_handlers={"_project_context_probe": internal_handler},
    )

    result = await connection.tool_execute(
        ToolCall(
            call_id="internal",
            name="_project_context_probe",
            arguments={"project_hint": "cognis"},
        )
    )

    assert result.is_error is False
    assert result.output == "cognis"

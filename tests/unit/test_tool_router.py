from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from cognis.core import tool_router as tool_router_module
from cognis.core.chat_modes import is_plan_hidden_tool
from cognis.core.tool_router import (
    ToolRoute,
    ToolRouter,
    _extract_output_anchor_names,
    caller_assignable_tools,
)
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.credential import CredentialAccessError, CredentialRecord
from cognis.models.session import SessionModel
from cognis.models.tool import (
    NativeToolDefinition as ToolDefinition,
)
from cognis.models.tool import (
    Permission,
    ToolCall,
    ToolResult,
    ToolSource,
    sanitize_mcp_tool_name,
)
from cognis.store.models import ArtifactRecordRow, AuditLog
from cognis.tools.builtin.schedule import MANAGE_SCHEDULES_TOOL
from cognis.tools.builtin.skill_management import SKILL_PATCH_TOOL
from cognis.tools.mcp import MCPClientError
from cognis.tools.registry import RegisteredTool, ToolExecutionContext, ToolRegistry

pytest_plugins = ("tests.unit.test_task_continuation_tools",)


def test_caller_assignable_tools_excludes_permission_denied_registry_tools() -> None:
    registry = ToolRegistry()
    for name in ("allowed_tool", "denied_tool"):
        registry.register(
            RegisteredTool(
                definition=ToolDefinition(
                    name=name,
                    description=f"{name}.",
                    parameters={"type": "object", "properties": {}},
                    source=ToolSource(type="builtin"),
                    read_only=True,
                )
            )
        )
    agent = AgentDefinition(
        agent_id="restricted",
        owner_email="owner@example.com",
        name="Restricted",
        permissions=AgentPermissions(denied_tools=["denied_tool"]),
    )

    assert [tool.name for tool in caller_assignable_tools(registry, agent)] == ["allowed_tool"]


class _Guardrails:
    def __init__(self) -> None:
        self.evaluate_calls = 0
        self.mcp_calls = 0
        self.last_mcp_call: tuple[str, str] | None = None
        self.last_mcp_arguments: dict | None = None
        self.last_mcp_context: dict | None = None
        self.last_evaluate_call: tuple[str, str, dict, dict] | None = None
        self.mcp_result = ToolResult(output="remote result")

    async def evaluate(
        self, session_id: str, tool_name: str, arguments: dict, context: dict
    ) -> object:
        self.evaluate_calls += 1
        self.last_evaluate_call = (session_id, tool_name, dict(arguments), dict(context))
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
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict,
        context: dict | None = None,
    ) -> ToolResult:
        del session_id
        self.mcp_calls += 1
        self.last_mcp_call = (server_name, tool_name)
        self.last_mcp_arguments = dict(arguments)
        self.last_mcp_context = dict(context or {})
        return self.mcp_result


class _Executor:
    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls = 0
        self.cancelled: list[str] = []
        self.result = result or ToolResult(output="local result")

    async def tool_execute(
        self,
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: object | None = None,
    ) -> ToolResult:
        del tool_call, timeout_seconds, output_chunk_callback
        self.calls += 1
        return self.result

    async def cancel_call(self, call_id: str) -> None:
        self.cancelled.append(call_id)


class _SlowExecutor(_Executor):
    async def tool_execute(
        self,
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: object | None = None,
    ) -> ToolResult:
        del tool_call, timeout_seconds, output_chunk_callback
        await asyncio.sleep(0.05)
        return ToolResult(output="too slow")


class _CapturingExecutor(_Executor):
    def __init__(self) -> None:
        super().__init__(result=ToolResult(output="captured"))
        self.tool_calls: list[ToolCall] = []

    async def tool_execute(
        self,
        tool_call: ToolCall,
        timeout_seconds: int | None = None,
        output_chunk_callback: object | None = None,
    ) -> ToolResult:
        del timeout_seconds, output_chunk_callback
        self.calls += 1
        self.tool_calls.append(tool_call)
        return self.result


class _RemoteExecutor(_Executor):
    def __init__(self, result: ToolResult | None = None) -> None:
        super().__init__(result=result)
        self.executor_id = "remote-exec"
        self.executor_type = "websocket"


@pytest.mark.asyncio
async def test_remote_mcp_401_refreshes_reconfigures_and_retries_once() -> None:
    router = object.__new__(ToolRouter)
    oauth_service = SimpleNamespace(
        refresh_token_for_server_id=AsyncMock(return_value=True),
        mark_token_invalid_for_server=AsyncMock(return_value=True),
        require_reauthorization_for_server=AsyncMock(return_value=None),
    )
    router._mcp_oauth_service = oauth_service
    router._session_factory = None
    router._wait_for_executor_reconfigure = AsyncMock(return_value=True)
    executor = _RemoteExecutor(result=ToolResult(output="recovered"))
    registered_tool = SimpleNamespace(
        definition=SimpleNamespace(
            source=SimpleNamespace(server_id="mcp-1"),
            read_only=True,
        )
    )
    first = ToolResult(
        output="Unauthorized",
        is_error=True,
        metadata={
            "mcp_auth_error": True,
            "authorization_required": True,
            "status_code": 401,
        },
    )
    tool_call = ToolCall(call_id="call-1", name="mcp_tool", arguments={})

    result = await router._recover_remote_mcp_oauth_once(
        result=first,
        tool_call=tool_call,
        registered_tool=registered_tool,
        session=SimpleNamespace(user_email="alice@example.com"),
        executor=executor,
        timeout_seconds=30,
        outer_timeout=31,
        output_chunk_callback=None,
    )

    assert result.output == "recovered"
    assert result.metadata["mcp_oauth_retry_attempted"] is True
    assert executor.calls == 1
    oauth_service.refresh_token_for_server_id.assert_awaited_once_with(
        user_email="alice@example.com",
        server_id="mcp-1",
        force=True,
        reason="mcp_tool_401",
    )
    router._wait_for_executor_reconfigure.assert_awaited_once_with("remote-exec")
    oauth_service.mark_token_invalid_for_server.assert_not_awaited()
    oauth_service.require_reauthorization_for_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_mcp_401_does_not_auto_replay_mutating_tool() -> None:
    router = object.__new__(ToolRouter)
    oauth_service = SimpleNamespace(
        refresh_token_for_server_id=AsyncMock(return_value=True),
    )
    router._mcp_oauth_service = oauth_service
    router._session_factory = None
    router._wait_for_executor_reconfigure = AsyncMock(return_value=True)
    executor = _RemoteExecutor(result=ToolResult(output="must not execute"))
    registered_tool = SimpleNamespace(
        definition=SimpleNamespace(
            source=SimpleNamespace(server_id="mcp-1"),
            read_only=False,
        )
    )

    result = await router._recover_remote_mcp_oauth_once(
        result=ToolResult(
            output="Unauthorized",
            is_error=True,
            metadata={"mcp_auth_error": True, "status_code": 401},
        ),
        tool_call=ToolCall(call_id="call-1", name="mcp_mutate", arguments={}),
        registered_tool=registered_tool,
        session=SimpleNamespace(user_email="alice@example.com"),
        executor=executor,
        timeout_seconds=30,
        outer_timeout=31,
        output_chunk_callback=None,
    )

    assert result.metadata["code"] == "mcp_oauth_refreshed_retry_required"
    assert result.metadata["retryable"] is True
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_remote_mcp_insufficient_scope_preserves_rfc9728_challenge() -> None:
    router = object.__new__(ToolRouter)
    oauth_service = SimpleNamespace(
        refresh_token_for_server_id=AsyncMock(return_value=True),
        require_reauthorization_for_server=AsyncMock(return_value=None),
    )
    router._mcp_oauth_service = oauth_service
    router._session_factory = None
    executor = _RemoteExecutor(result=ToolResult(output="must not execute"))
    registered_tool = SimpleNamespace(
        definition=SimpleNamespace(
            source=SimpleNamespace(server_id="mcp-1"),
            read_only=True,
        )
    )
    challenge = {
        "resource_metadata": "https://mcp.example/.well-known/oauth-protected-resource",
        "scope": "tools.write",
    }

    result = await router._recover_remote_mcp_oauth_once(
        result=ToolResult(
            output="Forbidden",
            is_error=True,
            metadata={
                "mcp_auth_error": True,
                "status_code": 403,
                "auth_error": "insufficient_scope",
                "authorization_challenge": challenge,
            },
        ),
        tool_call=ToolCall(call_id="call-1", name="mcp_tool", arguments={}),
        registered_tool=registered_tool,
        session=SimpleNamespace(
            user_email="alice@example.com",
            conversation_id="conv-1",
            session_id="sess-1",
        ),
        executor=executor,
        timeout_seconds=30,
        outer_timeout=31,
        output_chunk_callback=None,
    )

    assert result.metadata["reason"] == "insufficient_scope"
    oauth_service.require_reauthorization_for_server.assert_awaited_once_with(
        user_email="alice@example.com",
        server_id="mcp-1",
        reason="insufficient_scope",
        authorization_challenge=challenge,
        conversation_id="conv-1",
        session_id="sess-1",
    )
    oauth_service.refresh_token_for_server_id.assert_not_awaited()
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_remote_generic_403_does_not_refresh_or_reauthorize() -> None:
    router = object.__new__(ToolRouter)
    oauth_service = SimpleNamespace(
        refresh_token_for_server_id=AsyncMock(return_value=True),
        require_reauthorization_for_server=AsyncMock(return_value=None),
    )
    router._mcp_oauth_service = oauth_service
    router._session_factory = None
    executor = _RemoteExecutor(result=ToolResult(output="must not execute"))
    registered_tool = SimpleNamespace(
        definition=SimpleNamespace(
            source=SimpleNamespace(server_id="mcp-1"),
            read_only=True,
        )
    )
    original = ToolResult(
        output="Forbidden",
        is_error=True,
        metadata={
            "mcp_auth_error": True,
            "authorization_required": True,
            "status_code": 403,
        },
    )

    result = await router._recover_remote_mcp_oauth_once(
        result=original,
        tool_call=ToolCall(call_id="call-1", name="mcp_tool", arguments={}),
        registered_tool=registered_tool,
        session=SimpleNamespace(user_email="alice@example.com"),
        executor=executor,
        timeout_seconds=30,
        outer_timeout=31,
        output_chunk_callback=None,
    )

    assert result is original
    oauth_service.refresh_token_for_server_id.assert_not_awaited()
    oauth_service.require_reauthorization_for_server.assert_not_awaited()
    assert executor.calls == 0


class _FakeMCPRow(SimpleNamespace):
    server_id: str
    name: str
    status: str
    transport: str
    command: str | None
    url: str | None
    args: list[str]
    env: dict[str, str]
    headers: dict[str, str]
    auth_config: dict[str, object]
    timeout_seconds: int


class _ArtifactStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, str, bytes, str, str | None]] = []
        self.public_url_calls: list[tuple[str, str, str, int | None, str]] = []

    def generate_id(self, prefix: str) -> str:
        return f"{prefix}_1"

    async def async_save(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
        owner_email: str | None = None,
    ) -> None:
        self.saved.append((namespace, object_id, filename, content, mime_type, owner_email))

    async def async_get_public_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
        mode: str = "download",
    ) -> str:
        self.public_url_calls.append((namespace, object_id, filename, ttl_seconds, mode))
        suffix = "" if mode == "download" else f"?mode={mode}"
        return f"https://cognis.example.com/{namespace}/{object_id}/{filename}{suffix}"

    async def async_load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        return b"image-bytes", "image/png"


class _CredentialFailingProvider:
    async def resolve_ref(self, ref: str, *, agent: AgentDefinition, user_email: str) -> object:
        del ref, agent, user_email
        raise CredentialAccessError(
            "credential_not_allowed",
            "Credential not allowed for agent: rohlik",
            credential_id="rohlik",
        )


class _CredentialProvider:
    async def resolve_ref(self, ref: str, *, agent: AgentDefinition, user_email: str) -> object:
        del agent, user_email
        if ref == "$credential:reddit_mfa.otp":
            return SimpleNamespace(value="123456")
        raise AssertionError(f"unexpected ref: {ref}")


class _BrowserAuthStateCredentialProvider:
    def __init__(self, *, existing: bool = False) -> None:
        self.existing = existing
        self.upserts: list[dict[str, object]] = []

    async def get_credential(self, credential_id: str, user_email: str) -> CredentialRecord | None:
        if not self.existing:
            return None
        return CredentialRecord(
            credential_id=credential_id,
            user_email=user_email,
            kind="browser_storage_state",
            label="Existing browser state",
        )

    async def upsert_credential(self, **kwargs: object) -> CredentialRecord:
        self.upserts.append(kwargs)
        return CredentialRecord(
            credential_id=str(kwargs["credential_id"]),
            user_email=str(kwargs["user_email"]),
            kind="browser_storage_state",
            label=str(kwargs["label"]),
        )


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
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="memory_add",
                description="add memory",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="builtin"),
                category="memory",
                read_only=False,
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="memory_add_batch",
                description="add memories",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="builtin"),
                category="memory",
                read_only=False,
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="manage_schedules",
                description="manage schedules",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="builtin"),
                category="schedule",
                read_only=False,
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="skill_load",
                description="Load a skill by ID",
                parameters={"type": "object", "properties": {"skill_id": {"type": "string"}}},
                source=ToolSource(type="builtin"),
                category="system",
                read_only=True,
            )
        )
    )
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="skill_asset_materialize",
                description="materialize skill asset",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                category="filesystem",
                read_only=False,
            )
        )
    )
    return registry


def _readonly_registry(name: str = "memory_search") -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name=name,
                description="memory search",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="builtin"),
                read_only=True,
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


def _session_factory() -> object:
    class _Session:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def factory() -> object:
        yield _Session()

    return factory


async def _execute_controller_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int,
    auth_error: str | None,
    read_only: bool,
) -> tuple[ToolResult, SimpleNamespace, list[str]]:
    mcp_row = _FakeMCPRow(
        server_id="mcp_1",
        name="mfg-portal",
        status="active",
        transport="streamable_http",
        command=None,
        url="https://mfg.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
    )

    async def fake_get_mcp_server(*_args: object, **_kwargs: object) -> object:
        return mcp_row

    async def fake_get_setting_value(_session: object, key: str, default: object) -> object:
        return 300 if key == "mcp.tool_timeout_seconds" else 15

    oauth_service = SimpleNamespace(
        inject_authorization_header=AsyncMock(
            return_value=SimpleNamespace(
                authorization_required=False,
                headers={"Authorization": "Bearer access"},
            )
        ),
        refresh_token_for_server_id=AsyncMock(return_value=True),
        require_reauthorization_for_server=AsyncMock(return_value=None),
    )
    tool_calls: list[str] = []

    class _Client:
        async def connect(self) -> None:
            return None

        async def call_tool(self, raw_name: str, _arguments: dict[str, object]) -> object:
            tool_calls.append(raw_name)
            raise MCPClientError(
                "mfg-portal",
                "call_tool",
                "authorization rejected",
                error_class="http_status",
                status_code=status_code,
                auth_error=auth_error,
                authorization_challenge=(
                    {"scope": "tools.write"} if auth_error == "insufficient_scope" else None
                ),
            )

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            return None

    monkeypatch.setattr(tool_router_module, "get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(tool_router_module, "get_setting_value", fake_get_setting_value)
    monkeypatch.setattr(
        tool_router_module,
        "build_mcp_client",
        lambda _config, secrets: _Client(),
    )
    router = ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
        mcp_oauth_service=oauth_service,
    )
    registered_tool = RegisteredTool(
        definition=ToolDefinition(
            name="mcp_mfg_portal__tool",
            description="tool",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                type="local_mcp",
                server_id="mcp_1",
                server_name="mfg-portal",
                raw_tool_name="tool",
            ),
            read_only=read_only,
        )
    )
    result = await router._execute_controller_oauth_mcp_if_applicable(
        ToolCall(call_id="call_1", name="mcp_mfg_portal__tool", arguments={}),
        registered_tool=registered_tool,
        session=_session(),
    )
    assert result is not None
    return result, oauth_service, tool_calls


@pytest.mark.asyncio
async def test_controller_generic_403_does_not_refresh_or_reauthorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, oauth_service, tool_calls = await _execute_controller_auth_failure(
        monkeypatch,
        status_code=403,
        auth_error=None,
        read_only=True,
    )

    assert result.metadata["code"] == "mcp_tool_call_failed"
    assert result.metadata["authorization_required"] is False
    assert tool_calls == ["tool"]
    oauth_service.refresh_token_for_server_id.assert_not_awaited()
    oauth_service.require_reauthorization_for_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_mutating_401_refreshes_without_auto_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, oauth_service, tool_calls = await _execute_controller_auth_failure(
        monkeypatch,
        status_code=401,
        auth_error="invalid_token",
        read_only=False,
    )

    assert result.metadata["code"] == "mcp_oauth_refreshed_retry_required"
    assert tool_calls == ["tool"]
    oauth_service.refresh_token_for_server_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_controller_insufficient_scope_preserves_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, oauth_service, tool_calls = await _execute_controller_auth_failure(
        monkeypatch,
        status_code=403,
        auth_error="insufficient_scope",
        read_only=True,
    )

    assert result.metadata["reason"] == "insufficient_scope"
    assert tool_calls == ["tool"]
    oauth_service.require_reauthorization_for_server.assert_awaited_once_with(
        user_email="user@example.com",
        server_id="mcp_1",
        reason="insufficient_scope",
        authorization_challenge={"scope": "tools.write"},
        conversation_id="conv-a",
        session_id="session-a",
    )
    oauth_service.refresh_token_for_server_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_executes_oauth_http_mcp_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_row = _FakeMCPRow(
        server_id="mcp_1",
        name="mfg-portal",
        status="active",
        transport="streamable_http",
        command=None,
        url="https://mfg.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
    )
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_mcp_server(
        _session: object,
        server_id: str,
        *,
        owner_email: str,
        include_shared: bool,
    ) -> object:
        assert server_id == "mcp_1"
        assert owner_email == "user@example.com"
        assert include_shared is True
        return mcp_row

    async def fake_get_setting_value(_session: object, key: str, default: object) -> object:
        if key == "mcp.tool_timeout_seconds":
            assert default == 300
            return 300
        if key == "mcp.connect_timeout_seconds":
            assert default == 15
            return 15
        raise AssertionError(key)

    class _OAuthService:
        async def inject_authorization_header(
            self,
            *,
            user_email: str,
            server: object,
            headers: dict[str, str],
            conversation_id: str,
            session_id: str,
            task_id: object = None,
            step_name: object = None,
            step_run_id: object = None,
            delivery_mode: str | None = None,
        ) -> object:
            assert user_email == "user@example.com"
            assert server is mcp_row
            assert headers == {}
            assert conversation_id == "conv-a"
            assert session_id == "session-a"
            assert task_id is None
            assert step_name is None
            assert step_run_id is None
            assert delivery_mode == "same_conversation"
            return SimpleNamespace(
                authorization_required=False,
                headers={"Authorization": "Bearer fresh"},
            )

    class _Client:
        def __init__(self, config: object, secrets: dict[str, str]) -> None:
            assert secrets == {}
            self.config = config
            self.closed = False

        async def connect(self) -> None:
            assert self.config.headers == {"Authorization": "Bearer fresh"}

        async def call_tool(self, raw_name: str, arguments: dict[str, object]) -> object:
            calls.append((raw_name, arguments))
            return {"content": [{"type": "text", "text": "ok"}]}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled
            self.closed = True

    def fake_build_mcp_client(config: object, secrets: dict[str, str]) -> object:
        return _Client(config, secrets)

    monkeypatch.setattr(tool_router_module, "get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(tool_router_module, "get_setting_value", fake_get_setting_value)
    monkeypatch.setattr(tool_router_module, "build_mcp_client", fake_build_mcp_client)

    router = ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
        mcp_oauth_service=_OAuthService(),
    )
    registered_tool = RegisteredTool(
        definition=ToolDefinition(
            name="mcp_mfg_portal__whoami",
            description="whoami",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                type="local_mcp",
                server_id="mcp_1",
                server_name="mfg-portal",
                raw_tool_name="whoami",
            ),
        )
    )

    result = await router._execute_controller_oauth_mcp_if_applicable(
        ToolCall(call_id="call_1", name="mcp_mfg_portal__whoami", arguments={"x": 1}),
        registered_tool=registered_tool,
        session=_session(),
    )

    assert result is not None
    assert result.output == "ok"
    assert result.metadata is not None
    assert result.metadata["executed_by"] == "controller_oauth_mcp"
    assert result.metadata["timeout_seconds"] == 300
    assert calls == [("whoami", {"x": 1})]


@pytest.mark.asyncio
async def test_controller_oauth_mcp_returns_auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_row = _FakeMCPRow(
        server_id="mcp_1",
        name="mfg-portal",
        status="active",
        transport="streamable_http",
        command=None,
        url="https://mfg.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
    )

    async def fake_get_mcp_server(
        _session: object,
        _server_id: str,
        *,
        owner_email: str,
        include_shared: bool,
    ) -> object:
        assert owner_email == "user@example.com"
        assert include_shared is True
        return mcp_row

    async def fake_get_setting_value(_session: object, key: str, default: object) -> object:
        if key == "mcp.tool_timeout_seconds":
            assert default == 300
            return 300
        if key == "mcp.connect_timeout_seconds":
            assert default == 15
            return 15
        raise AssertionError(key)

    class _OAuthService:
        calls: list[dict[str, object]] = []

        async def inject_authorization_header(self, **_kwargs: object) -> object:
            self.calls.append(dict(_kwargs))
            return SimpleNamespace(
                authorization_required=True,
                headers={},
                transaction_id="txn_1",
                authorization_url="https://auth.example",
                authorization_expires_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
                reason="token_expired",
            )

    monkeypatch.setattr(tool_router_module, "get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(tool_router_module, "get_setting_value", fake_get_setting_value)

    router = ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
        mcp_oauth_service=_OAuthService(),
    )
    registered_tool = RegisteredTool(
        definition=ToolDefinition(
            name="mcp_mfg_portal__whoami",
            description="whoami",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type="local_mcp", server_id="mcp_1", raw_tool_name="whoami"),
        )
    )

    result = await router._execute_controller_oauth_mcp_if_applicable(
        ToolCall(call_id="call_1", name="mcp_mfg_portal__whoami", arguments={}),
        registered_tool=registered_tool,
        session=_session(),
    )

    assert result is not None
    assert result.is_error is True
    assert "MCP authorization is required for mfg-portal" in result.output
    assert "https://auth.example" in result.output
    assert "2026-01-02T03:04:05+00:00" in result.output
    assert "retry the tool call" in result.output
    assert result.metadata is not None
    assert result.metadata["code"] == "mcp_authorization_required"
    assert result.metadata["server_id"] == "mcp_1"
    assert result.metadata["server_name"] == "mfg-portal"
    assert result.metadata["transaction_id"] == "txn_1"
    assert result.metadata["authorization_url"] == "https://auth.example"
    assert result.metadata["authorization_expires_at"] == "2026-01-02T03:04:05+00:00"
    assert router._mcp_oauth_service.calls[0]["delivery_mode"] == "same_conversation"


@pytest.mark.asyncio
async def test_controller_oauth_mcp_returns_setup_failure_when_auth_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_row = _FakeMCPRow(
        server_id="mcp_1",
        name="mfg-portal",
        status="active",
        transport="streamable_http",
        command=None,
        url="https://mfg.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
    )

    async def fake_get_mcp_server(
        _session: object,
        _server_id: str,
        *,
        owner_email: str,
        include_shared: bool,
    ) -> object:
        assert owner_email == "user@example.com"
        assert include_shared is True
        return mcp_row

    async def fake_get_setting_value(_session: object, key: str, default: object) -> object:
        if key == "mcp.tool_timeout_seconds":
            assert default == 300
            return 300
        if key == "mcp.connect_timeout_seconds":
            assert default == 15
            return 15
        raise AssertionError(key)

    class _OAuthService:
        async def inject_authorization_header(self, **_kwargs: object) -> object:
            assert _kwargs["delivery_mode"] == "same_conversation"
            return SimpleNamespace(
                authorization_required=True,
                headers={},
                transaction_id=None,
                authorization_url=None,
                reason="authorization_required",
            )

    monkeypatch.setattr(tool_router_module, "get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(tool_router_module, "get_setting_value", fake_get_setting_value)

    router = ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
        mcp_oauth_service=_OAuthService(),
    )
    registered_tool = RegisteredTool(
        definition=ToolDefinition(
            name="mcp_mfg_portal__whoami",
            description="whoami",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type="local_mcp", server_id="mcp_1", raw_tool_name="whoami"),
        )
    )

    result = await router._execute_controller_oauth_mcp_if_applicable(
        ToolCall(call_id="call_1", name="mcp_mfg_portal__whoami", arguments={}),
        registered_tool=registered_tool,
        session=_session(),
    )

    assert result is not None
    assert result.is_error is True
    assert "MCP OAuth setup failed for mfg-portal" in result.output
    assert "could not generate an OAuth authorization URL" in result.output
    assert "MCP authorization is required before this tool can be used" not in result.output
    assert result.metadata is not None
    assert result.metadata["code"] == "mcp_oauth_setup_failed"
    assert result.metadata["server_id"] == "mcp_1"
    assert result.metadata["server_name"] == "mfg-portal"
    assert result.metadata["retryable"] is False


@pytest.mark.asyncio
async def test_execute_bypasses_executor_for_oauth_http_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_row = _FakeMCPRow(
        server_id="mcp_1",
        name="mfg-portal",
        status="active",
        transport="streamable_http",
        command=None,
        url="https://mfg.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
    )

    async def fake_get_mcp_server(
        _session: object,
        _server_id: str,
        *,
        owner_email: str,
        include_shared: bool,
    ) -> object:
        assert owner_email == "user@example.com"
        assert include_shared is True
        return mcp_row

    async def fake_get_setting_value(_session: object, key: str, default: object) -> object:
        if key == "mcp.tool_timeout_seconds":
            assert default == 300
            return 300
        if key == "mcp.connect_timeout_seconds":
            assert default == 15
            return 15
        raise AssertionError(key)

    class _OAuthService:
        async def inject_authorization_header(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                authorization_required=False,
                headers={"Authorization": "Bearer fresh"},
            )

    class _Client:
        def __init__(self, config: object, _secrets: dict[str, str]) -> None:
            self.config = config

        async def connect(self) -> None:
            return None

        async def call_tool(self, _raw_name: str, _arguments: dict[str, object]) -> object:
            return {"content": [{"type": "text", "text": "controller result"}]}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled

    def fake_build_mcp_client(config: object, secrets: dict[str, str]) -> object:
        return _Client(config, secrets)

    monkeypatch.setattr(tool_router_module, "get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(tool_router_module, "get_setting_value", fake_get_setting_value)
    monkeypatch.setattr(tool_router_module, "build_mcp_client", fake_build_mcp_client)

    tool_name = "mcp_mfg_portal__whoami"
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name=tool_name,
                description="whoami",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="local_mcp", server_id="mcp_1", raw_tool_name="whoami"),
                timeout_seconds=5,
            )
        )
    )
    executor = _Executor()
    router = ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
        mcp_oauth_service=_OAuthService(),
    )

    result = await router.execute(
        ToolCall(call_id="call_1", name=tool_name, arguments={}),
        _session(),
        _agent(),
        registry,
        executor,
    )

    assert "controller result" in str(result.output)
    assert executor.calls == 0
    assert result.metadata is not None
    assert result.metadata["executed_by"] == "controller_oauth_mcp"
    assert result.metadata["timeout_seconds"] == 300


def test_tool_router_classifies_routes() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=["shell"])
    registry = _registry()

    assert router.classify("delegate", registry) is ToolRoute.ORCHESTRATION
    assert router.classify("artifact_read", registry) is ToolRoute.ARTIFACT
    assert router.classify("artifact_list_recent", registry) is ToolRoute.ARTIFACT
    assert router.classify("artifact_search", registry) is ToolRoute.ARTIFACT
    assert router.classify("artifact_get_metadata", registry) is ToolRoute.ARTIFACT
    assert (
        router.classify(sanitize_mcp_tool_name("github", "search"), registry)
        is ToolRoute.INTARIS_MCP
    )
    assert (
        router.classify(sanitize_mcp_tool_name("filesystem", "read_file"), registry)
        is ToolRoute.LOCAL
    )
    assert router.classify("missing", registry) is ToolRoute.UNKNOWN


def test_plan_hidden_tool_policy_blocks_writes_but_not_unknown_or_readonly() -> None:
    registry = _registry()
    assert is_plan_hidden_tool(registry.get("memory_add").definition) is False
    assert is_plan_hidden_tool(registry.get("memory_add_batch").definition) is False
    assert is_plan_hidden_tool(registry.get("skill_asset_materialize").definition) is False
    assert is_plan_hidden_tool(registry.get("shell").definition) is False
    assert (
        is_plan_hidden_tool(registry.get(sanitize_mcp_tool_name("github", "search")).definition)
        is False
    )
    assert is_plan_hidden_tool(_readonly_registry().get("memory_search").definition) is False

    ambiguous = ToolDefinition(
        name="ambiguous",
        description="ambiguous",
        parameters={},
        source=ToolSource(type="builtin"),
        read_only=True,
        classification_status="unknown",
    )
    assert is_plan_hidden_tool(ambiguous) is False


@pytest.mark.asyncio
async def test_plan_mode_allows_memory_add_before_execution() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    executor = _Executor()

    result = await router.execute(
        ToolCall(
            call_id="plan-1",
            name="memory_add",
            arguments={"content": "do not write"},
            runtime_metadata={"read_only_required": True, "chat_mode": "plan"},
        ),
        _session(),
        _agent(),
        _registry(),
        executor,
    )

    assert result.metadata is None or result.metadata.get("code") != "plan_mode_mutation_denied"
    assert "Write tools are disabled" not in result.output
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_plan_mode_denies_routed_schedule_write_before_execution() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])

    result = await router.execute(
        ToolCall(
            call_id="plan-schedule",
            name="manage_schedules",
            arguments={"action": "create", "name": "x"},
            runtime_metadata={"read_only_required": True, "chat_mode": "plan"},
        ),
        _session().model_copy(update={"user_email": "owner@example.com"}),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert result.is_error is True
    assert "Plan mode is active" in result.output
    assert result.metadata and result.metadata["code"] == "plan_mode_mutation_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_action"),
    [
        ({"action": "list"}, "list"),
        ({"action": "trigger", "schedule_id": "schedule-1"}, "trigger"),
    ],
)
async def test_schedule_dispatch_validates_domain_without_agent_management(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, str],
    expected_action: str,
) -> None:
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=MANAGE_SCHEDULES_TOOL))
    validated_contexts: list[object] = []

    async def validate_domain(
        definitions: list[ToolDefinition],
        tool_name: str,
        call_arguments: dict[str, str],
        context: object,
    ) -> dict[str, object]:
        assert definitions == [MANAGE_SCHEDULES_TOOL]
        assert tool_name == "manage_schedules"
        assert call_arguments == arguments
        validated_contexts.append(context)
        return {"valid": True}

    async def handle_schedule(**kwargs: object) -> ToolResult:
        assert kwargs["arguments"] == arguments
        return ToolResult(output=expected_action)

    monkeypatch.setattr(
        tool_router_module,
        "validate_available_tool_call_with_context",
        validate_domain,
    )
    monkeypatch.setattr(tool_router_module, "handle_schedule_tool", handle_schedule)

    result = await ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
    ).execute(
        ToolCall(
            call_id=f"schedule-{expected_action}", name="manage_schedules", arguments=arguments
        ),
        _session(),
        _agent(),
        registry,
        _Executor(),
    )

    assert result.is_error is False
    assert expected_action in result.output
    assert len(validated_contexts) == 1


@pytest.mark.asyncio
async def test_schedule_dispatch_rejects_invalid_domain_without_agent_management(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=MANAGE_SCHEDULES_TOOL))
    handler = AsyncMock(return_value=ToolResult(output="should not run"))

    async def reject_domain(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"valid": False, "error": "schedule_not_found"}

    monkeypatch.setattr(
        tool_router_module,
        "validate_available_tool_call_with_context",
        reject_domain,
    )
    monkeypatch.setattr(tool_router_module, "handle_schedule_tool", handler)

    result = await ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
    ).execute(
        ToolCall(
            call_id="schedule-invalid-domain",
            name="manage_schedules",
            arguments={"action": "trigger", "schedule_id": "schedule-1"},
        ),
        _session(),
        _agent(),
        registry,
        _Executor(),
    )

    assert result.is_error is True
    assert result.metadata and result.metadata["code"] == "invalid_tool_arguments"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_skill_patch_marks_skill_epoch_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=SKILL_PATCH_TOOL))

    async def handle_skill_patch(**_kwargs: object) -> ToolResult:
        return ToolResult(output="patched", metadata={"version": 2})

    monkeypatch.setattr(tool_router_module, "handle_skill_management_tool", handle_skill_patch)

    result = await ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
    ).execute(
        ToolCall(
            call_id="skill-patch",
            name="skill_patch",
            arguments={"skill_id": "skill-1", "instructions": "Updated"},
        ),
        _session(),
        _agent(),
        registry,
        _Executor(),
    )

    assert result.is_error is False
    assert result.metadata and result.metadata["skill_epoch_stale"] is True
    assert result.metadata["version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        "Skill 'skill-1' not found",
        "no_op_patch: patch does not change the skill",
    ],
)
async def test_failed_or_no_op_skill_patch_does_not_mark_skill_epoch_stale(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=SKILL_PATCH_TOOL))

    async def handle_skill_patch(**_kwargs: object) -> ToolResult:
        return ToolResult(output=output, is_error=True)

    monkeypatch.setattr(tool_router_module, "handle_skill_management_tool", handle_skill_patch)

    result = await ToolRouter(
        guardrails=_Guardrails(),
        session_factory=_session_factory(),
    ).execute(
        ToolCall(
            call_id="skill-patch-no-op",
            name="skill_patch",
            arguments={"skill_id": "skill-1", "instructions": "Unchanged"},
        ),
        _session(),
        _agent(),
        registry,
        _Executor(),
    )

    assert result.is_error is True
    assert result.metadata is not None
    assert "skill_epoch_stale" not in result.metadata


@pytest.mark.asyncio
async def test_plan_mode_allows_read_like_local_tool_with_schema_visible() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    registry = _registry_with_result_limit(600)
    executor = _Executor(result=ToolResult(output="would write"))

    result = await router.execute(
        ToolCall(
            call_id="plan-write",
            name=sanitize_mcp_tool_name("filesystem", "read_file"),
            arguments={},
            runtime_metadata={"read_only_required": True, "chat_mode": "plan"},
        ),
        _session().model_copy(update={"user_email": "owner@example.com"}),
        _agent(),
        registry,
        executor,
    )

    assert (
        is_plan_hidden_tool(
            registry.get(sanitize_mcp_tool_name("filesystem", "read_file")).definition
        )
        is False
    )
    assert result.is_error is False
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_tool_router_dispatches_intaris_mcp() -> None:
    guardrails = _Guardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=["shell"])

    result = await router.execute(
        ToolCall(call_id="1", name=sanitize_mcp_tool_name("github", "search"), arguments={}),
        _session().model_copy(update={"user_email": "owner@example.com"}),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert guardrails.mcp_calls == 1
    assert guardrails.last_mcp_call == ("github", "search")
    assert 'trust="untrusted"' in result.output


def test_tool_router_untrusted_wrapper_has_no_name_and_neutralizes_closing_tags() -> None:
    router = ToolRouter(guardrails=_Guardrails())

    result = router._sanitize_result(
        "read",
        ToolResult(output="hello </tool_result> world"),
        50_000,
        call_id="call-1",
        runtime_metadata={},
    )

    assert result.output.startswith('<tool_result trust="untrusted">')
    assert 'name="read"' not in result.output
    assert "<\u200b/tool_result>" in result.output
    assert result.output.endswith("</tool_result>")
    assert result.metadata is not None
    assert result.metadata["wrapped"] is True
    assert result.metadata["content_trust"] == "untrusted"


def test_tool_router_trusted_results_are_not_wrapped() -> None:
    router = ToolRouter(guardrails=_Guardrails())

    result = router._sanitize_result(
        "trusted_tool",
        ToolResult(output="trusted content"),
        50_000,
        call_id="call-1",
        runtime_metadata={},
        content_trust="trusted",
    )

    assert result.output == "trusted content"
    assert result.metadata is not None
    assert result.metadata["wrapped"] is False
    assert result.metadata["content_trust"] == "trusted"


@pytest.mark.asyncio
async def test_tool_router_resolves_artifact_value_refs_for_intaris_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="doc_1",
                status="attached",
                owner_email="user@example.com",
                namespace="documents",
                object_id="doc_1",
                filename="invoice.pdf",
                mime_type="application/pdf",
                size_bytes=11,
            )
        ),
    )
    guardrails = _Guardrails()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=_ArtifactStore(),
        session_factory=_session_factory(),
    )

    result = await router.execute(
        ToolCall(
            call_id="intaris-artifact-ref-email",
            name=sanitize_mcp_tool_name("github", "search"),
            arguments={
                "attachments": [
                    {
                        "content": "$artifact:doc_1.content_b64",
                        "filename": "$artifact:doc_1.filename",
                        "mime_type": "$artifact:doc_1.mime_type",
                    }
                ]
            },
        ),
        _session(),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert result.is_error is False
    assert guardrails.mcp_calls == 1
    assert guardrails.last_mcp_arguments is not None
    attachment = guardrails.last_mcp_arguments["attachments"][0]
    assert base64.b64decode(attachment["content"]) == b"image-bytes"
    assert attachment["filename"] == "invoice.pdf"
    assert attachment["mime_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_tool_router_fails_before_intaris_mcp_when_artifact_ref_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(return_value=None),
    )
    guardrails = _Guardrails()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=_ArtifactStore(),
        session_factory=_session_factory(),
    )

    result = await router.execute(
        ToolCall(
            call_id="intaris-missing-artifact-ref",
            name=sanitize_mcp_tool_name("github", "search"),
            arguments={"attachment": "$artifact:missing.content_b64"},
        ),
        _session(),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert result.is_error is True
    assert "Artifact not found: missing" in result.output
    assert guardrails.mcp_calls == 0


@pytest.mark.asyncio
async def test_tool_router_passes_plan_mode_context_to_intaris_mcp() -> None:
    guardrails = _Guardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=[])

    await router.execute(
        ToolCall(
            call_id="1",
            name=sanitize_mcp_tool_name("github", "search"),
            arguments={},
            runtime_metadata={
                "chat_mode": "plan",
                "chat_mode_source": "one_shot",
                "read_only_required": True,
            },
        ),
        _session(),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert guardrails.mcp_calls == 1
    assert guardrails.last_mcp_context is not None
    assert guardrails.last_mcp_context["chat_mode"] == "plan"
    assert guardrails.last_mcp_context["chat_mode_source"] == "one_shot"
    assert guardrails.last_mcp_context["read_only_required"] is True


def test_extract_output_anchor_names_prefers_metadata() -> None:
    metadata = {
        "output_anchors": [
            {"anchor": "error:1", "label": "first error"},
            {"name": "summary"},
            "result:1",
        ]
    }
    raw_output = "[[stale]]\nsome content\n"

    names = _extract_output_anchor_names(metadata, raw_output)

    # Metadata-supplied anchors win over inline scan, in order, deduped.
    assert names == ["error:1", "summary", "result:1"]


def test_extract_output_anchor_names_falls_back_to_inline_scan() -> None:
    raw_output = "header\n[[result:1]]\nbody\n[[result:2]]\nmore\n[[result:1]]\n"

    names = _extract_output_anchor_names({}, raw_output)

    assert names == ["result:1", "result:2"]


def test_extract_output_anchor_names_includes_markdown_headings() -> None:
    raw_output = "# Summary\nBody\n\n### Must Fix\nDetails\n\n#### Too Deep\nIgnored\n"

    names = _extract_output_anchor_names({}, raw_output)

    assert names == ["heading:summary", "heading:must-fix"]


def test_extract_output_anchor_names_does_not_duplicate_stored_markdown_headings() -> None:
    raw_output = "# Summary\nBody\n\n## Verdict\nDone\n"
    metadata = {
        "output_anchors": [
            {
                "anchor": "heading:summary",
                "label": "Summary",
                "kind": "markdown_heading",
                "start_line": 1,
                "end_line": 3,
            },
            {
                "anchor": "heading:verdict",
                "label": "Verdict",
                "kind": "markdown_heading",
                "start_line": 4,
                "end_line": 5,
            },
        ]
    }

    names = _extract_output_anchor_names(metadata, raw_output)

    assert names == ["heading:summary", "heading:verdict"]


def test_extract_output_anchor_names_handles_missing_metadata() -> None:
    assert _extract_output_anchor_names(None, "no anchors here") == []


def test_decision_cache_key_buckets_read_only_by_tool_name() -> None:
    """Read-only cache shares one slot per session/tool/runtime regardless of args."""

    key_a = ToolRouter._decision_cache_key(
        "session-a", "read", {"file_path": "/a/foo.py"}, read_only=True
    )
    key_b = ToolRouter._decision_cache_key(
        "session-a", "read", {"file_path": "/b/bar.py"}, read_only=True
    )
    key_other_tool = ToolRouter._decision_cache_key(
        "session-a", "grep", {"pattern": "foo"}, read_only=True
    )

    assert key_a == key_b
    assert key_a != key_other_tool


def test_decision_cache_key_separates_read_only_by_executor_runtime() -> None:
    """Executor switches must not reuse path-policy decisions from another runtime."""

    key_a = ToolRouter._decision_cache_key(
        "session-a",
        "read",
        {"file_path": "/a/foo.py"},
        read_only=True,
        context={"executor_environment": {"executor_id": "exec-a", "cwd": "/home/a"}},
    )
    key_b = ToolRouter._decision_cache_key(
        "session-a",
        "read",
        {"file_path": "/a/foo.py"},
        read_only=True,
        context={"executor_environment": {"executor_id": "exec-b", "cwd": "/home/b"}},
    )

    assert key_a != key_b


@pytest.mark.asyncio
async def test_evaluation_context_infers_executor_home_from_workspace_root() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    context = await router._evaluation_context(
        ToolCall(
            call_id="r1",
            name="read",
            arguments={"file_path": "~/src/cognis/README.md"},
            runtime_metadata={
                "workspace_root": "/home/riker/src/cognis",
                "working_directory": "/home/riker/src/cognis",
                "executor_environment": {"executor_id": "exec-1", "home": None},
            },
        )
    )

    assert context["executor_environment"]["home"] == "/home/riker"


def test_decision_cache_key_separates_writes_by_arguments() -> None:
    """Non-read-only callers retain per-argument keys to prevent cross-payload reuse."""

    key_a = ToolRouter._decision_cache_key(
        "session-a", "write", {"file_path": "/a/foo.py"}, read_only=False
    )
    key_b = ToolRouter._decision_cache_key(
        "session-a", "write", {"file_path": "/b/bar.py"}, read_only=False
    )

    assert key_a != key_b


@pytest.mark.asyncio
async def test_tool_router_caches_read_only_approval_across_arguments() -> None:
    """The first read-only approval warms the cache for subsequent reads of the same tool."""

    guardrails = _Guardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=[])

    first = await router.evaluate_tool_call(
        ToolCall(call_id="r1", name="memory_search", arguments={"query": "alpha"}),
        _agent({"*": Permission.EVALUATE}),
        _session(),
        _readonly_registry(),
    )
    second = await router.evaluate_tool_call(
        ToolCall(call_id="r2", name="memory_search", arguments={"query": "beta"}),
        _agent({"*": Permission.EVALUATE}),
        _session(),
        _readonly_registry(),
    )

    assert first.decision == "approve"
    assert second.decision == "approve"
    assert second.source == "guardrails_cache"
    assert guardrails.evaluate_calls == 1


@pytest.mark.asyncio
async def test_tool_router_passes_executor_runtime_to_guardrails() -> None:
    guardrails = _Guardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=[])

    await router.evaluate_tool_call(
        ToolCall(
            call_id="r1",
            name="artifact_publish",
            arguments={"path": "/home/riker/image.png"},
            runtime_metadata={
                "working_directory": "/home/riker",
                "executor_environment": {
                    "executor_id": "riker-laptop",
                    "executor_type": "websocket",
                    "cwd": "/home/riker",
                    "home": "/home/riker",
                },
            },
        ),
        _agent({"*": Permission.EVALUATE}),
        _session(),
        _readonly_registry("artifact_publish"),
    )

    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, _arguments, context = guardrails.last_evaluate_call
    assert context["working_directory"] == "/home/riker"
    assert context["executor_environment"]["cwd"] == "/home/riker"
    assert "hostname" not in context["executor_environment"]
    assert context["tool"]["description"] == "memory search"
    assert context["tool"]["read_only"] is True
    assert context["tool"]["id"] == "builtin:artifact_publish"


@pytest.mark.asyncio
async def test_evaluation_context_summarizes_tool_classification_and_parameters() -> None:
    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    context = await router._evaluation_context(
        ToolCall(call_id="r1", name="mcp_alertmanager__silences", arguments={}),
        ToolDefinition(
            name="mcp_alertmanager__silences",
            description="List Alertmanager silences",
            parameters={
                "type": "object",
                "required": ["filter"],
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Matcher used to filter active silences.",
                    },
                    "large": {"type": "object", "x-internal": "not included"},
                },
            },
            source=ToolSource(
                type="local_mcp",
                server_id="alertmanager-prod",
                server_name="alertmanager",
                raw_tool_name="silences",
            ),
            category="observability",
            profile_group="development",
            read_only=True,
            classification_status="ready",
            classification_source="llm",
            classification_confidence=0.92,
            risk_level="low",
        ),
    )

    assert context["tool"]["id"] == "mcp:alertmanager-prod:silences"
    assert context["tool"]["read_only"] is True
    assert context["tool"]["classification"] == {
        "status": "ready",
        "source": "llm",
        "confidence": 0.92,
    }
    assert context["tool"]["parameters_summary"]["required"] == ["filter"]
    assert context["tool"]["parameters_summary"]["properties"]["filter"] == {
        "type": "string",
        "description": "Matcher used to filter active silences.",
    }
    assert context["tool"]["parameters_summary"]["properties"]["large"] == {"type": "object"}


@pytest.mark.asyncio
async def test_tool_router_passes_skill_load_metadata_to_guardrails(monkeypatch) -> None:
    async def _fake_get_skill_scoped(db, skill_id: str, *, owner_email=None):
        del db, owner_email
        return SimpleNamespace(
            skill_id=skill_id,
            name="lumilens-loki-query",
            description="Safe procedure for querying Lumilens production Loki.",
            tags=["lumilens"],
        )

    import cognis.store.queries as queries

    monkeypatch.setattr(queries, "get_skill_scoped", _fake_get_skill_scoped)
    guardrails = _Guardrails()
    router = ToolRouter(
        guardrails=guardrails,
        non_bypassable_patterns=[],
        session_factory=_session_factory(),
    )

    await router.evaluate_tool_call(
        ToolCall(
            call_id="skill-1",
            name="skill_load",
            arguments={"skill_id": "skill_bbff42a5255b"},
        ),
        _agent({"*": Permission.EVALUATE}),
        _session(),
        _registry(),
    )

    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, _arguments, context = guardrails.last_evaluate_call
    assert context["tool"]["description"] == "Load a skill by ID"
    assert context["tool"]["read_only"] is True
    assert context["skill"]["skill_id"] == "skill_bbff42a5255b"
    assert context["skill"]["name"] == "lumilens-loki-query"
    assert "Loki" in context["skill"]["description"]


@pytest.mark.asyncio
async def test_tool_router_does_not_cache_escalate_for_read_only_tools() -> None:
    class _EscalateThenApproveGuardrails:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(
            self, session_id: str, tool_name: str, arguments: dict, context: dict
        ) -> object:
            del session_id, tool_name, arguments, context
            self.calls += 1
            decision = "escalate" if self.calls == 1 else "approve"
            return type(
                "Evaluation",
                (),
                {
                    "decision": decision,
                    "reasoning": None,
                    "risk": None,
                    "path": None,
                    "latency_ms": 0,
                    "call_id": f"eval_{self.calls}",
                },
            )()

    guardrails = _EscalateThenApproveGuardrails()
    router = ToolRouter(guardrails=guardrails, non_bypassable_patterns=[])
    tool_call = ToolCall(call_id="cache-1", name="memory_search", arguments={"query": "x"})

    first = await router.evaluate_tool_call(
        tool_call,
        _agent({"*": Permission.EVALUATE}),
        _session(),
        _readonly_registry(),
    )
    second = await router.evaluate_tool_call(
        tool_call,
        _agent({"*": Permission.EVALUATE}),
        _session(),
        _readonly_registry(),
    )

    assert first.decision == "escalate"
    assert second.decision == "approve"
    assert guardrails.calls == 2


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
async def test_tool_router_rejects_malformed_local_tool_arguments() -> None:
    called = False

    async def bash_handler(arguments: dict[str, object], context: object) -> object:
        nonlocal called
        del arguments, context
        called = True
        return "should not execute"

    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="bash",
                description="Run shell commands",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                source=ToolSource(type="executor"),
                category="shell",
                read_only=False,
                non_bypassable=True,
            ),
            handler=bash_handler,
        )
    )

    result = await router.execute(
        ToolCall(
            call_id="bash-raw-1",
            name="bash",
            arguments={"_raw": '{"command":"unterminated'},
        ),
        _session(),
        _agent(),
        registry,
        _RemoteExecutor(),
    )

    assert result.is_error is True
    assert called is False
    assert result.metadata is not None
    assert result.metadata["code"] == "invalid_tool_arguments"
    assert "invalid_tool_arguments" in result.output
    assert "valid JSON" in result.output


@pytest.mark.asyncio
async def test_tool_router_accepts_multiline_local_tool_arguments() -> None:
    captured: dict[str, object] = {}

    async def bash_handler(arguments: dict[str, object], context: object) -> object:
        del context
        captured.update(arguments)
        return "accepted"

    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="bash",
                description="Run shell commands",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "workdir": {"type": "string"},
                    },
                    "required": ["command"],
                },
                source=ToolSource(type="executor"),
                category="shell",
                read_only=False,
                non_bypassable=True,
            ),
            handler=bash_handler,
        )
    )
    command = (
        'gh pr create --title "feat(blr-lab): configure rclone bisync" '
        '--body "Mirrors the HA bisync model.\n\n'
        'AWS secret JSON: {\\"username\\":\\"bisync\\",\\"password\\":\\"...\\"}"'
    )

    result = await router.execute(
        ToolCall(
            call_id="bash-multiline-1",
            name="bash",
            arguments={
                "command": command,
                "workdir": "/Users/fpytloun/src/lumilens/beskar",
            },
        ),
        _session(),
        _agent(),
        registry,
        _RemoteExecutor(),
    )

    assert result.is_error is False
    assert captured["command"] == command


@pytest.mark.asyncio
async def test_tool_router_passes_merged_runtime_metadata_to_registered_handler() -> None:
    captured: dict[str, object] = {}

    async def builtin_handler(
        arguments: dict[str, object], context: ToolExecutionContext
    ) -> object:
        del arguments
        captured.update(context.runtime_metadata)
        runtime_access = context.runtime_metadata.get("runtime_access", {})
        user_email = runtime_access.get("user_email") if isinstance(runtime_access, dict) else None
        interaction_mode = (
            runtime_access.get("interaction_mode") if isinstance(runtime_access, dict) else None
        )
        return {"user_email": user_email, "interaction_mode": interaction_mode}

    router = ToolRouter(guardrails=_Guardrails(), non_bypassable_patterns=[])
    executor = _RemoteExecutor()
    executor.runtime_metadata = {"user_email": "user@example.com", "executor_key": "executor"}
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="local_context_probe",
                description="context probe",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="builtin"),
                category="system",
                read_only=True,
            ),
            handler=builtin_handler,
        )
    )

    result = await router.execute(
        ToolCall(
            call_id="local-context-1",
            name="local_context_probe",
            arguments={},
            runtime_metadata={
                "tool_key": "tool",
                "runtime_access": {
                    "user_email": "user@example.com",
                    "interaction_mode": "none",
                },
            },
        ),
        _session(),
        _agent(),
        registry,
        executor,
    )

    assert executor.calls == 0
    assert result.is_error is False
    assert '"interaction_mode": "none"' in str(result.output)
    assert captured["user_email"] == "user@example.com"
    assert captured["executor_key"] == "executor"
    assert captured["tool_key"] == "tool"
    assert captured["runtime_access"] == {
        "user_email": "user@example.com",
        "interaction_mode": "none",
    }


@pytest.mark.asyncio
async def test_tool_router_returns_recoverable_result_for_credential_resolution_errors() -> None:
    router = ToolRouter(
        guardrails=_Guardrails(),
        non_bypassable_patterns=[],
        credentials_provider=_CredentialFailingProvider(),
    )

    result = await router.execute(
        ToolCall(
            call_id="cred-1",
            name=sanitize_mcp_tool_name("filesystem", "read_file"),
            arguments={"value_ref": "$credential:rohlik.username"},
        ),
        _session(),
        _agent(),
        _registry(),
        _Executor(),
    )

    assert result.is_error is True
    assert result.metadata is not None
    assert result.metadata["code"] == "credential_not_allowed"
    assert result.metadata["recoverable"] is True
    assert result.metadata["credential_id"] == "rohlik"
    assert "Credential not allowed for agent" in result.output


@pytest.mark.asyncio
async def test_tool_router_persist_browser_auth_state_grants_new_credential_to_agent() -> None:
    provider = _BrowserAuthStateCredentialProvider()
    router = ToolRouter(guardrails=_Guardrails(), credentials_provider=provider)
    agent = _agent()

    result = await router._persist_browser_auth_state_if_needed(  # noqa: SLF001
        ToolResult(
            output="captured",
            metadata={
                "browser_auth_state": {
                    "credential_id": "bazos-browser",
                    "label": "Bazos browser state",
                    "kind": "browser_storage_state",
                    "metadata": {"origin": "https://www.bazos.cz"},
                    "payload": {"storage_state": {"cookies": [], "origins": []}},
                }
            },
        ),
        _session(),
        agent,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["saved_credential_id"] == "bazos-browser"
    assert result.metadata["credential_granted_to_agent"] is True
    assert provider.upserts[0]["credential_id"] == "bazos-browser"
    assert agent.permissions is not None
    assert "bazos-browser" in agent.permissions.allowed_credentials


@pytest.mark.asyncio
async def test_tool_router_persist_browser_auth_state_rejects_existing_ungranted_credential() -> (
    None
):
    provider = _BrowserAuthStateCredentialProvider(existing=True)
    router = ToolRouter(guardrails=_Guardrails(), credentials_provider=provider)

    with pytest.raises(CredentialAccessError, match="Credential not allowed"):
        await router._persist_browser_auth_state_if_needed(  # noqa: SLF001
            ToolResult(
                output="captured",
                metadata={
                    "browser_auth_state": {
                        "credential_id": "bazos-browser",
                        "label": "Bazos browser state",
                        "payload": {"storage_state": {"cookies": [], "origins": []}},
                    }
                },
            ),
            _session(),
            _agent(),
        )

    assert provider.upserts == []


@pytest.mark.asyncio
async def test_tool_router_resolves_browser_eval_args_after_guardrails() -> None:
    guardrails = _Guardrails()
    router = ToolRouter(
        guardrails=guardrails,
        non_bypassable_patterns=[],
        credentials_provider=_CredentialProvider(),
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="browser_eval",
                description="eval",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="local_mcp", server_name="browser", raw_tool_name="eval"),
                non_bypassable=True,
                timeout_seconds=1,
            )
        )
    )
    executor = _CapturingExecutor()

    result = await router.execute(
        ToolCall(
            call_id="browser-eval-1",
            name="browser_eval",
            arguments={
                "session_id": "browser-1",
                "script": "(code) => code",
                "args": [{"value_ref": "$credential:reddit_mfa.otp"}],
            },
        ),
        _session(),
        _agent(),
        registry,
        executor,
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    assert guardrails.last_evaluate_call[2]["args"] == ["<resolved-at-execution>"]
    assert executor.tool_calls[0].arguments["args"] == ["123456"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["browser_type", "browser_press"])
async def test_tool_router_resolves_browser_typing_value_ref_after_guardrails(
    tool_name: str,
) -> None:
    guardrails = _Guardrails()
    router = ToolRouter(
        guardrails=guardrails,
        non_bypassable_patterns=[],
        credentials_provider=_CredentialProvider(),
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name=tool_name,
                description="type",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="local_mcp", server_name="browser", raw_tool_name=tool_name),
                non_bypassable=True,
                timeout_seconds=1,
            )
        )
    )
    executor = _CapturingExecutor()

    result = await router.execute(
        ToolCall(
            call_id=f"{tool_name}-1",
            name=tool_name,
            arguments={"session_id": "browser-1", "value_ref": "$credential:reddit_mfa.otp"},
        ),
        _session(),
        _agent(),
        registry,
        executor,
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    assert guardrails.last_evaluate_call[2]["value_ref"] == "<resolved-at-execution>"
    assert executor.tool_calls[0].arguments["value"] == "123456"


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
    assert result.metadata["code"] == "tool_execution_timeout"
    assert result.metadata["retryable"] is False


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


@pytest.mark.asyncio
async def test_tool_router_materializes_inline_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact_store = _ArtifactStore()
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=artifact_store,
        session_factory=_session_factory(),
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="document_generate",
                description="doc",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )
    monkeypatch.setattr("cognis.core.tool_router.create_artifact_record", AsyncMock())

    result = await router.execute(
        ToolCall(call_id="6", name="document_generate", arguments={"content": "x"}),
        _session(),
        _agent(),
        registry,
        _RemoteExecutor(
            ToolResult(
                output="ok",
                attachments=[
                    {
                        "filename": "report.pdf",
                        "mime_type": "application/pdf",
                        "content_b64": base64.b64encode(b"pdf").decode("ascii"),
                    }
                ],
            )
        ),
    )

    assert artifact_store.saved
    assert result.attachments is not None
    assert result.attachments[0]["artifact_id"] == "doc_1"
    assert result.metadata is not None
    raw_output = result.metadata["_raw_output"]
    assert "artifact_id" in raw_output
    assert "https://cognis.example.com/documents/doc_1/report.pdf" in raw_output


@pytest.mark.asyncio
async def test_tool_router_enriches_inline_attachment_output_with_artifact_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = _ArtifactStore()
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=artifact_store,
        session_factory=_session_factory(),
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="web_fetch",
                description="fetch",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )
    monkeypatch.setattr("cognis.core.tool_router.create_artifact_record", AsyncMock())

    result = await router.execute(
        ToolCall(call_id="6b", name="web_fetch", arguments={"url": "https://example.com/a.png"}),
        _session(),
        _agent(),
        registry,
        _RemoteExecutor(
            ToolResult(
                output=(
                    "[[metadata]]\n"
                    "Filename: a.png\n"
                    "Content type: image/png\n"
                    "Binary content: attached as artifact\n"
                    "Use artifact_read to analyze this image with a vision-capable model."
                ),
                attachments=[
                    {
                        "filename": "a.png",
                        "mime_type": "image/png",
                        "content_b64": base64.b64encode(b"png").decode("ascii"),
                    }
                ],
            )
        ),
    )

    assert result.attachments is not None
    assert result.attachments[0]["artifact_id"] == "att_1"
    assert result.metadata is not None
    raw_output = result.metadata["_raw_output"]
    assert 'Binary content: attached as artifact (artifact_id="att_1")' in raw_output
    assert 'artifact_read with artifact_id="att_1" to analyze this image' in raw_output
    assert "[[attachments]]" in raw_output
    assert "Artifact ID: att_1" in raw_output
    assert "Filename: a.png" in raw_output
    assert 'artifact_read with artifact_id="att_1"' in raw_output
    assert 'artifact_get_url with artifact_id="att_1"' in raw_output
    anchors = result.metadata["output_anchors"]
    assert {anchor["anchor"] for anchor in anchors} == {"binary", "attachment:1"}
    assert all("artifact_candidate" not in anchor for anchor in anchors)


@pytest.mark.asyncio
async def test_tool_router_prepares_document_artifact_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = _ArtifactStore()
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=artifact_store,
        session_factory=_session_factory(),
    )
    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=type(
                "ArtifactRow",
                (),
                {
                    "status": "attached",
                    "owner_email": "user@example.com",
                    "namespace": "attachments",
                    "object_id": "art_1",
                    "filename": "diagram.png",
                },
            )()
        ),
    )

    prepared = await router._prepare_local_tool_call(  # noqa: SLF001
        ToolCall(
            call_id="7",
            name="document_generate",
            arguments={
                "content": "![x](asset:diag)",
                "assets": [{"name": "diag", "artifact_id": "art_1"}],
            },
        ),
        _session(),
        AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
    )

    asset = prepared.arguments["assets"][0]
    assert asset["filename"] == "diagram.png"
    assert asset["mime_type"] == "image/png"
    assert base64.b64decode(asset["content_b64"]) == b"image-bytes"


@pytest.mark.asyncio
async def test_tool_router_rejects_expired_artifact_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = _ArtifactStore()
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=artifact_store,
        session_factory=_session_factory(),
    )
    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=type(
                "ArtifactRow",
                (),
                {
                    "status": "temporary",
                    "owner_email": "user@example.com",
                    "namespace": "tool-outputs",
                    "object_id": "toolout_1",
                    "filename": "call.txt",
                    "expires_at": datetime(2026, 4, 21, 13, 0, tzinfo=UTC),
                },
            )()
        ),
    )

    with pytest.raises(ValueError, match="Artifact not found: toolout_1"):
        await router._prepare_local_tool_call(  # noqa: SLF001
            ToolCall(
                call_id="7-expired",
                name="document_generate",
                arguments={
                    "content": "![x](asset:diag)",
                    "assets": [{"name": "diag", "artifact_id": "toolout_1"}],
                },
            ),
            _session(),
            AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        )


@pytest.mark.asyncio
async def test_tool_router_enriches_already_materialized_attachment_output() -> None:
    artifact_store = _ArtifactStore()
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=artifact_store,
        session_factory=_session_factory(),
    )
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="artifact_publish",
                description="artifact",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )

    result = await router.execute(
        ToolCall(call_id="8", name="artifact_publish", arguments={"path": "/tmp/x"}),
        _session(),
        _agent(),
        registry,
        _RemoteExecutor(
            ToolResult(
                output='{"images": [], "model": "test"}',
                attachments=[
                    {
                        "artifact_id": "img_1",
                        "url": "https://cognis.example.com/images/img_1/image",
                        "mime_type": "image/jpeg",
                        "filename": "img_1.jpg",
                        "size_bytes": 12,
                    }
                ],
            )
        ),
    )

    assert result.metadata is not None
    raw_output = result.metadata["_raw_output"]
    assert '"artifact_id": "img_1"' in raw_output
    assert '"mime_type": "image/jpeg"' in raw_output


@pytest.mark.asyncio
async def test_tool_router_enriches_image_tool_output() -> None:
    artifact_store = _ArtifactStore()
    image_provider = AsyncMock(
        image_generate=AsyncMock(
            return_value=type(
                "ImageResult",
                (),
                {
                    "images": [
                        type(
                            "Image",
                            (),
                            {
                                "b64_json": "YWJj",
                                "content_type": "image/png",
                                "revised_prompt": None,
                            },
                        )()
                    ],
                    "model": "test-model",
                },
            )()
        )
    )
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=artifact_store,
        image_generation_provider=image_provider,
    )

    result = await router.execute(
        ToolCall(call_id="9", name="image_generate", arguments={"prompt": "banner"}),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.metadata is not None
    raw_output = result.metadata["_raw_output"]
    assert '"artifact_id": "img_1"' in raw_output
    assert '"mime_type": "image/png"' in raw_output


@pytest.mark.asyncio
async def test_tool_router_handles_artifact_read_with_current_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    class _Llm:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] | None = None
            self.model_info_calls: list[dict[str, object]] = []

        async def get_model_info(
            self,
            model_id: str,
            provider_id: str | None = None,
            acting_user_email: str | None = None,
        ) -> object:
            self.model_info_calls.append(
                {
                    "model_id": model_id,
                    "provider_id": provider_id,
                    "acting_user_email": acting_user_email,
                }
            )
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            del kwargs
            self.messages = messages
            return {"choices": [{"message": {"content": "It is a blue square."}}]}

    class _Session:
        async def get(self, model: object, key: str) -> object | None:
            del model, key
            return None

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="img_1",
                status="attached",
                owner_email="user@example.com",
                namespace="images",
                object_id="img_1",
                filename="image.png",
                mime_type="image/png",
                kind="image",
                size_bytes=8,
            )
        ),
    )

    llm = _Llm()
    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=llm,
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-1",
            name="artifact_read",
            arguments={"artifact_id": "img_1", "offset": 99, "limit": 1},
            runtime_metadata={"resolved_model": "gpt-4o-mini"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert (
        "Prepared artifact 'image.png' for native model inspection"
        in result.metadata["_raw_output"]
    )
    assert result.metadata["native_attachment"] is True
    assert llm.model_info_calls == [
        {
            "model_id": "gpt-4o-mini",
            "provider_id": None,
            "acting_user_email": "user@example.com",
        }
    ]
    assert result.attachments == [
        {
            "artifact_id": "img_1",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "image.png",
            "size_bytes": 8,
            "url": "https://cognis.example.com/images/img_1/image.png",
            "native_inspection_only": True,
        }
    ]
    assert llm.messages is None


@pytest.mark.asyncio
async def test_artifact_read_materializes_tool_artifact_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.builtin.artifact_tools import handle_artifact_tool

    class _Anchor:
        anchor = "media:1"
        artifact_candidate = {
            "source_type": "remote_url",
            "url": "https://cdn.example.com/product.svg",
            "mime_hint": "image/svg+xml",
            "filename_hint": "product.svg",
            "metadata": {"source_page_url": "https://news.example.com/article"},
        }

    class _ToolOutputStore:
        async def list_anchors(self, call_id: str) -> list[_Anchor]:
            assert call_id == "call-web"
            return [_Anchor()]

    class _Session:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    row = SimpleNamespace(
        artifact_id="att_1",
        namespace="attachments",
        object_id="att_1",
        filename="product.svg",
        owner_email="user@example.com",
        conversation_id="call-web",
        session_id="media:1",
        message_role="assistant",
        purpose="tool_artifact",
        kind="file",
        mime_type="image/svg+xml",
        size_bytes=7,
        status="attached",
        created_at=None,
        expires_at=None,
        deleted_at=None,
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.find_tool_artifact_record",
        AsyncMock(return_value=None),
    )
    create_record = AsyncMock(return_value=row)
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.create_artifact_record",
        create_record,
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.find_tool_output_artifact_record",
        AsyncMock(return_value=SimpleNamespace(conversation_id="conv-a")),
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools._fetch_remote_artifact_candidate",
        AsyncMock(
            return_value=ToolResult(
                output="fetched",
                metadata={
                    "content": b"<svg></svg>",
                    "mime_type": "image/svg+xml",
                    "filename": "product.svg",
                },
            )
        ),
    )

    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "tool_artifact:call-web:media:1"},
        llm=None,
        artifact_store=_ArtifactStore(),
        session_factory=session_factory,
        user_email="user@example.com",
        current_model=None,
        current_provider_id=None,
        runtime_metadata={
            "tool_output_store": _ToolOutputStore(),
            "authorized_lazy_artifact_refs": ["tool_artifact:call-web:media:1"],
            "runtime_access": {
                "conversation_id": "conv-a",
                "session_id": "session-a",
            },
        },
    )

    assert result.is_error is False
    assert "Materialized tool_artifact:call-web:media:1 as artifact att_1" in result.output
    assert result.metadata is not None
    assert result.metadata["materialized_artifact_id"] == "att_1"
    assert result.metadata["source_url"] == "https://news.example.com/article"
    assert result.metadata["asset_url"] == "https://cdn.example.com/product.svg"
    create_kwargs = create_record.await_args.kwargs
    assert create_kwargs["source_tool_call_id"] == "call-web"
    assert create_kwargs["source_anchor"] == "media:1"
    assert create_kwargs["content_hash"] == hashlib.sha256(b"<svg></svg>").hexdigest()
    assert create_kwargs["conversation_id"] == "conv-a"
    assert create_kwargs["session_id"] == "session-a"


@pytest.mark.asyncio
async def test_artifact_read_blocks_unpromoted_lazy_ref_before_store_lookup() -> None:
    from cognis.tools.builtin.artifact_tools import handle_artifact_tool

    class _ToolOutputStore:
        async def list_anchors(self, call_id: str) -> list[object]:
            raise AssertionError(f"unauthorized store lookup for {call_id}")

    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "tool_artifact:call-web:media:1"},
        llm=None,
        artifact_store=object(),
        session_factory=object(),
        user_email="user@example.com",
        current_model=None,
        current_provider_id=None,
        runtime_metadata={
            "tool_output_store": _ToolOutputStore(),
            "authorized_lazy_artifact_refs": [],
        },
    )

    assert result.is_error is True
    assert result.output == "Tool artifact access denied: tool_artifact:call-web:media:1"


@pytest.mark.asyncio
async def test_pinned_remote_backend_connects_to_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpcore

    from cognis.tools.builtin.artifact_tools import _PinnedNetworkBackend

    stream = object()
    connect = AsyncMock(return_value=stream)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    backend = _PinnedNetworkBackend(
        host="cdn.example.com",
        ip_address="203.0.113.10",
    )

    result = await backend.connect_tcp("cdn.example.com", 443, timeout=10)

    assert result is stream
    connect.assert_awaited_once_with(
        "203.0.113.10",
        443,
        timeout=10,
        local_address=None,
        socket_options=None,
    )


@pytest.mark.asyncio
async def test_artifact_read_resolves_binary_tool_artifact_to_persisted_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.builtin.artifact_tools import handle_artifact_tool

    class _Anchor:
        anchor = "binary"
        artifact_candidate = {
            "source_type": "artifact_id",
            "artifact_id": "att_1",
            "mime_hint": "image/png",
            "filename_hint": "a.png",
        }

    class _ToolOutputStore:
        async def list_anchors(self, call_id: str) -> list[_Anchor]:
            assert call_id == "call-web"
            return [_Anchor()]

    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    class _Llm:
        async def get_model_info(
            self,
            model_id: str,
            provider_id: str | None = None,
            acting_user_email: str | None = None,
        ) -> object:
            del model_id, provider_id, acting_user_email
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

    class _Session:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    row = SimpleNamespace(
        artifact_id="att_1",
        namespace="attachments",
        object_id="att_1",
        filename="a.png",
        owner_email="user@example.com",
        conversation_id="conv-a",
        session_id="session-a",
        message_role="assistant",
        purpose="web_fetch",
        kind="image",
        mime_type="image/png",
        size_bytes=9,
        status="attached",
        created_at=None,
        expires_at=None,
        deleted_at=None,
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.find_tool_output_artifact_record",
        AsyncMock(return_value=SimpleNamespace(conversation_id="conv-a")),
    )

    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "tool_artifact:call-web:binary"},
        llm=_Llm(),
        artifact_store=_Store(),
        session_factory=session_factory,
        user_email="user@example.com",
        current_model="gpt-4o-mini",
        current_provider_id=None,
        runtime_metadata={
            "tool_output_store": _ToolOutputStore(),
            "authorized_lazy_artifact_refs": [],
            "runtime_access": {"conversation_id": "conv-a", "session_id": "session-a"},
        },
    )

    assert result.is_error is False
    assert "Materialized tool_artifact:call-web:binary as artifact att_1" in result.output
    assert result.metadata is not None
    assert result.metadata["materialized_artifact_id"] == "att_1"
    assert result.attachments == [
        {
            "artifact_id": "att_1",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "a.png",
            "size_bytes": 9,
            "url": "https://cognis.example.com/attachments/att_1/a.png",
            "native_inspection_only": True,
        }
    ]


@pytest.mark.asyncio
async def test_tool_artifact_remote_fetch_blocks_private_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.builtin.artifact_tools import _fetch_remote_artifact_candidate

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.socket.getaddrinfo",
        lambda *_, **__: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
    )

    result = await _fetch_remote_artifact_candidate("https://example.com/image.png")

    assert result.is_error is True
    assert "blocked network address" in result.output


@pytest.mark.asyncio
async def test_tool_artifact_remote_fetch_blocks_redirect_to_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.builtin.artifact_tools import _fetch_remote_artifact_candidate

    def fake_getaddrinfo(host: str, *_args: object, **_kwargs: object) -> list[object]:
        ip = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443))]

    class _Response:
        is_redirect = True
        headers = {"location": "http://127.0.0.1/private.png"}
        request = SimpleNamespace(url="https://example.com/image.png")

        async def aclose(self) -> None:
            return None

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def build_request(self, *_args: object, **_kwargs: object) -> object:
            return object()

        async def send(self, *_args: object, **_kwargs: object) -> _Response:
            return _Response()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.socket.getaddrinfo",
        fake_getaddrinfo,
    )
    monkeypatch.setattr("cognis.tools.builtin.artifact_tools.httpx.AsyncClient", _Client)

    result = await _fetch_remote_artifact_candidate("https://example.com/image.png")

    assert result.is_error is True
    assert "blocked network address" in result.output


@pytest.mark.asyncio
async def test_tool_router_handles_artifact_read_with_owner_email_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"line one\nline two\n", "text/plain"

    class _Session:
        async def get(self, model: object, key: str) -> object | None:
            del model, key
            return None

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="txt_1",
                status="attached",
                owner_email="user@example.com",
                namespace="texts",
                object_id="txt_1",
                filename="notes.txt",
                mime_type="text/plain",
                kind="file",
                size_bytes=18,
            )
        ),
    )
    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=None,
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-text",
            name="artifact_read",
            arguments={"artifact_id": "txt_1"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert "1: line one" in result.metadata["_raw_output"]
    assert result.metadata["artifact_id"] == "txt_1"


@pytest.mark.asyncio
async def test_artifact_read_uses_effective_owner_for_attachment_analysis_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.builtin.artifact_tools import handle_artifact_tool

    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    class _Llm:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        async def get_model_info(self, model_id: str, provider_id: str | None = None) -> object:
            if model_id == "no-vision":
                return SimpleNamespace(
                    supports_vision=False,
                    supports_pdf_input=False,
                    supports_audio_input=False,
                    supports_file_input=False,
                )
            assert model_id == "vision-route"
            assert provider_id == "route-provider"
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            del messages
            self.kwargs = dict(kwargs)
            return {"choices": [{"message": {"content": "Analyzed with route."}}]}

    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    routing_owner_emails: list[str | None] = []

    async def fake_get_model_routing(
        session: object, task_type: str, owner_email: str | None = None
    ) -> object | None:
        del session
        routing_owner_emails.append(owner_email)
        assert task_type == "attachment_analysis"
        if owner_email == "artifact-owner@example.com":
            return SimpleNamespace(model="vision-route", provider_id="route-provider")
        return None

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="img_1",
                status="attached",
                owner_email="artifact-owner@example.com",
                namespace="images",
                object_id="img_1",
                filename="image.png",
                mime_type="image/png",
                kind="image",
                size_bytes=8,
            )
        ),
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_model_routing",
        fake_get_model_routing,
    )

    llm = _Llm()
    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "img_1"},
        llm=llm,
        artifact_store=_Store(),
        session_factory=session_factory,
        user_email="session-user@example.com",
        current_model="no-vision",
        owner_email="artifact-owner@example.com",
    )

    assert result.is_error is False
    assert result.output == "Analyzed with route."
    assert routing_owner_emails == ["artifact-owner@example.com"]
    assert llm.kwargs == {
        "model": "vision-route",
        "task_type": "attachment_analysis",
        "provider_id": "route-provider",
    }


@pytest.mark.asyncio
async def test_artifact_read_returns_native_attachment_for_capable_current_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.builtin.artifact_tools import handle_artifact_tool

    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"jpeg-bytes", "image/jpeg"

    class _Llm:
        def __init__(self) -> None:
            self.generate_calls = 0

        async def get_model_info(self, model_id: str, provider_id: str | None = None) -> object:
            del model_id, provider_id
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            del messages, kwargs
            self.generate_calls += 1
            raise RuntimeError("provider rejected image input")

    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    async def fake_get_model_routing(
        session: object, task_type: str, owner_email: str | None = None
    ) -> object | None:
        del session, owner_email
        assert task_type == "attachment_analysis"
        return SimpleNamespace(model="gpt-5.5", provider_id=None)

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="img_1",
                status="attached",
                owner_email="user@example.com",
                namespace="images",
                object_id="img_1",
                filename="image.jpg",
                mime_type="image/jpeg",
                kind="image",
                size_bytes=10,
            )
        ),
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_model_routing",
        fake_get_model_routing,
    )

    llm = _Llm()
    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": "img_1", "prompt": "Describe the image briefly."},
        llm=llm,
        artifact_store=_Store(),
        session_factory=session_factory,
        user_email="user@example.com",
        current_model="gpt-5.5",
        current_provider_id="codex",
    )

    assert result.is_error is False
    assert llm.generate_calls == 0
    assert "Prepared artifact 'image.jpg' for native model inspection" in result.output
    assert "Requested analysis prompt: Describe the image briefly." in result.output
    assert result.attachments == [
        {
            "artifact_id": "img_1",
            "kind": "image",
            "mime_type": "image/jpeg",
            "filename": "image.jpg",
            "size_bytes": 10,
            "url": "https://cognis.example.com/images/img_1/image.jpg",
            "native_inspection_only": True,
        }
    ]
    assert result.metadata is not None
    assert result.metadata["native_attachment"] is True
    assert result.metadata["analysis_model"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_tool_router_postprocesses_binary_read_with_current_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"jpeg-bytes", "image/jpeg"

    class _Llm:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] | None = None
            self.model_info_calls: list[dict[str, object]] = []

        async def get_model_info(
            self,
            model_id: str,
            provider_id: str | None = None,
            acting_user_email: str | None = None,
        ) -> object:
            self.model_info_calls.append(
                {
                    "model_id": model_id,
                    "provider_id": provider_id,
                    "acting_user_email": acting_user_email,
                }
            )
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            del kwargs
            self.messages = messages
            return {"choices": [{"message": {"content": "It is a generated banner."}}]}

    class _Session:
        attachment_analysis_lookups = 0

        async def commit(self) -> None:
            return None

        async def get(self, model: object, key: str) -> object | None:
            del model
            if key == "attachment_analysis":
                self.attachment_analysis_lookups += 1
                return SimpleNamespace(model="gpt-4o", provider_id="openai")
            return None

    session = _Session()

    @asynccontextmanager
    async def session_factory() -> object:
        yield session

    monkeypatch.setattr("cognis.core.tool_router.create_artifact_record", AsyncMock())
    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="att_1",
                status="attached",
                owner_email="user@example.com",
                namespace="attachments",
                object_id="att_1",
                filename="photo.jpg",
                mime_type="image/jpeg",
                kind="image",
                size_bytes=10,
            )
        ),
    )

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="read",
                description="read",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )

    llm = _Llm()
    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=llm,
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="read-current-model",
            name="read",
            arguments={"file_path": "/tmp/photo.jpg"},
            runtime_metadata={"resolved_model": "gpt-5.4", "resolved_provider_id": "openai"},
        ),
        _session(),
        _agent(),
        registry,
        _RemoteExecutor(
            ToolResult(
                output="Prepared binary file.",
                metadata={"attachment_analysis_request": {"source": "read"}},
                attachments=[
                    {
                        "filename": "photo.jpg",
                        "mime_type": "image/jpeg",
                        "content_b64": base64.b64encode(b"jpeg-bytes").decode("ascii"),
                    }
                ],
            )
        ),
    )

    assert result.is_error is False
    assert result.attachments is not None
    assert result.attachments[0]["artifact_id"] == "att_1"
    assert result.metadata is not None
    assert (
        "Prepared binary file 'photo.jpg' for native model inspection"
        in result.metadata["_raw_output"]
    )
    assert result.metadata["analysis_model"] == "gpt-5.4"
    assert result.metadata["native_attachment"] is True
    assert llm.model_info_calls == [
        {
            "model_id": "gpt-5.4",
            "provider_id": "openai",
            "acting_user_email": "user@example.com",
        }
    ]
    assert session.attachment_analysis_lookups == 0
    assert llm.messages is None


@pytest.mark.asyncio
async def test_tool_router_retries_artifact_read_with_attachment_route_after_native_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    class _Llm:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def get_model_info(self, model_id: str, provider_id: str | None = None) -> object:
            self.calls.append(
                {"operation": "get_model_info", "model": model_id, "provider_id": provider_id}
            )
            if model_id == "gpt-5.5":
                return SimpleNamespace(
                    supports_vision=False,
                    supports_pdf_input=False,
                    supports_audio_input=False,
                    supports_file_input=False,
                )
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            self.calls.append({"operation": "generate", **kwargs, "messages": messages})
            content = messages[0]["content"]
            assert isinstance(content, list)
            image = content[2]
            assert isinstance(image, dict)
            image_url = image["image_url"]
            assert isinstance(image_url, dict)
            if kwargs["task_type"] == "default":
                raise RuntimeError("Direct Codex request failed: HTTP 400")
            return {"choices": [{"message": {"content": "Fallback route analyzed it."}}]}

    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    async def fake_get_model_routing(
        session: object, task_type: str, owner_email: str | None = None
    ) -> object | None:
        del session, owner_email
        assert task_type == "attachment_analysis"
        return SimpleNamespace(model="vision-route", provider_id="route-provider")

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="img_1",
                status="attached",
                owner_email="user@example.com",
                namespace="images",
                object_id="img_1",
                filename="image.png",
                mime_type="image/png",
                kind="image",
                size_bytes=8,
            )
        ),
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_model_routing",
        fake_get_model_routing,
    )

    llm = _Llm()
    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=llm,
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-fallback",
            name="artifact_read",
            arguments={"artifact_id": "img_1"},
            runtime_metadata={"resolved_model": "gpt-5.5"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["_raw_output"] == "Fallback route analyzed it."
    assert result.metadata["analysis_model"] == "vision-route"
    assert result.metadata["analysis_task_type"] == "attachment_analysis"
    assert result.metadata["used_attachment_analysis_route"] is True
    generate_calls = [call for call in llm.calls if call["operation"] == "generate"]
    assert [call["model"] for call in generate_calls] == ["vision-route"]
    assert [call["task_type"] for call in generate_calls] == ["attachment_analysis"]


@pytest.mark.asyncio
async def test_tool_router_retries_image_url_payload_inline_before_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    class _Llm:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_model_info(self, model_id: str, provider_id: str | None = None) -> object:
            del provider_id
            if model_id == "gpt-5.5":
                return SimpleNamespace(
                    supports_vision=False,
                    supports_pdf_input=False,
                    supports_audio_input=False,
                    supports_file_input=False,
                )
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            del kwargs
            content = messages[0]["content"]
            assert isinstance(content, list)
            image = content[2]
            assert isinstance(image, dict)
            image_url = image["image_url"]
            assert isinstance(image_url, dict)
            url = image_url["url"]
            assert isinstance(url, str)
            self.calls.append(url)
            if url.startswith("https://"):
                raise RuntimeError("provider could not fetch signed URL")
            return {"choices": [{"message": {"content": "Inline image worked."}}]}

    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="img_1",
                status="attached",
                owner_email="user@example.com",
                namespace="images",
                object_id="img_1",
                filename="image.png",
                mime_type="image/png",
                kind="image",
                size_bytes=8,
            )
        ),
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_model_routing",
        AsyncMock(return_value=SimpleNamespace(model="vision-route", provider_id="route-provider")),
    )

    llm = _Llm()
    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=llm,
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-inline-retry",
            name="artifact_read",
            arguments={"artifact_id": "img_1"},
            runtime_metadata={"resolved_model": "gpt-5.5"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["_raw_output"] == "Inline image worked."
    assert result.metadata["analysis_model"] == "vision-route"
    assert result.metadata["analysis_task_type"] == "attachment_analysis"
    assert result.metadata["analysis_payload"] == "inline"
    assert result.metadata["used_attachment_analysis_route"] is True
    assert llm.calls == [
        "https://cognis.example.com/images/img_1/image.png",
        "data:image/png;base64,cG5nLWJ5dGVz",
    ]


@pytest.mark.asyncio
async def test_tool_router_handles_artifact_list_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.list_recent_artifact_records",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    artifact_id="doc_2",
                    filename="report-final.pdf",
                    kind="pdf",
                    mime_type="application/pdf",
                    purpose="tool_output",
                    size_bytes=128,
                    status="attached",
                    created_at=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
                    conversation_id="conv-1",
                    session_id="sess-1",
                )
            ]
        ),
    )

    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=_ArtifactStore(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(call_id="art-list", name="artifact_list_recent", arguments={"limit": 5}),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["count"] == 1
    assert "report-final.pdf" in result.metadata["_raw_output"]


@pytest.mark.asyncio
async def test_tool_router_handles_artifact_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.search_artifact_records",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    artifact_id="doc_3",
                    filename="weekly-report.pdf",
                    kind="pdf",
                    mime_type="application/pdf",
                    purpose="tool_output",
                    size_bytes=256,
                    status="attached",
                    created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
                    conversation_id="conv-2",
                    session_id="sess-2",
                )
            ]
        ),
    )

    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=_ArtifactStore(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-search",
            name="artifact_search",
            arguments={"query": "weekly report", "kind": "pdf"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["count"] == 1
    assert "weekly-report.pdf" in result.metadata["_raw_output"]


@pytest.mark.asyncio
async def test_tool_router_handles_artifact_get_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="doc_4",
                namespace="documents",
                object_id="doc_4",
                filename="summary.pdf",
                owner_email="user@example.com",
                conversation_id="conv-3",
                session_id="sess-3",
                message_role="assistant",
                purpose="tool_output",
                kind="pdf",
                mime_type="application/pdf",
                size_bytes=512,
                status="attached",
                created_at=datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                expires_at=None,
                deleted_at=None,
            )
        ),
    )
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=_ArtifactStore(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-meta",
            name="artifact_get_metadata",
            arguments={"artifact_id": "doc_4"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["artifact_id"] == "doc_4"
    assert result.metadata["download_url_tool"] == "artifact_get_url"
    assert '"namespace": "documents"' in result.metadata["_raw_output"]


@pytest.mark.asyncio
async def test_tool_router_handles_artifact_get_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="img_4",
                namespace="images",
                object_id="img_4",
                filename="image",
                owner_email="user@example.com",
                conversation_id="conv-3",
                session_id="sess-3",
                message_role="assistant",
                purpose="tool_output",
                kind="image",
                mime_type="image/jpeg",
                size_bytes=512,
                status="attached",
                created_at=datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                expires_at=None,
                deleted_at=None,
            )
        ),
    )
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=_ArtifactStore(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-url",
            name="artifact_get_url",
            arguments={"artifact_id": "img_4"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["artifact_id"] == "img_4"
    assert result.metadata["url"] == "https://cognis.example.com/images/img_4/image"
    assert (
        '"url": "https://cognis.example.com/images/img_4/image"' in result.metadata["_raw_output"]
    )
    # artifact_get_url no longer returns attachments; the signed URL is in the JSON
    # output and metadata — the agent should surface it as text, not echo the file
    # back into the assistant message bubble.
    assert not result.attachments


@pytest.mark.asyncio
async def test_tool_router_handles_artifact_get_view_url_and_clamps_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="html_4",
                namespace="reports",
                object_id="html_4",
                filename="report.html",
                owner_email="user@example.com",
                conversation_id="conv-3",
                session_id="sess-3",
                message_role="assistant",
                purpose="tool_output",
                kind="file",
                mime_type="text/html",
                size_bytes=512,
                status="temporary",
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(seconds=120),
                deleted_at=None,
            )
        ),
    )
    artifact_store = _ArtifactStore()
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=artifact_store,
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-view-url",
            name="artifact_get_url",
            arguments={"artifact_id": "html_4", "ttl_seconds": 604800, "mode": "view"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["artifact_id"] == "html_4"
    assert result.metadata["mode"] == "view"
    assert (
        result.metadata["url"] == "https://cognis.example.com/reports/html_4/report.html?mode=view"
    )
    assert artifact_store.public_url_calls[0][3] is not None
    assert 60 <= artifact_store.public_url_calls[0][3] <= 120
    assert artifact_store.public_url_calls[0][4] == "view"


@pytest.mark.asyncio
async def test_tool_router_rejects_artifact_view_url_for_non_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="txt_4",
                namespace="reports",
                object_id="txt_4",
                filename="report.txt",
                owner_email="user@example.com",
                purpose="tool_output",
                kind="file",
                mime_type="text/plain",
                size_bytes=512,
                status="attached",
                created_at=datetime.now(UTC),
                expires_at=None,
                deleted_at=None,
            )
        ),
    )
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=_ArtifactStore(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-view-url",
            name="artifact_get_url",
            arguments={"artifact_id": "txt_4", "mode": "view"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is True
    assert "Artifact view is only supported for HTML artifacts: txt_4" in result.output


@pytest.mark.asyncio
async def test_tool_router_rejects_expired_artifact_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        pass

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="toolout_expired",
                namespace="tool-outputs",
                object_id="toolout_expired",
                filename="call.txt",
                owner_email="user@example.com",
                purpose="tool_output",
                kind="file",
                mime_type="text/plain",
                size_bytes=128,
                status="temporary",
                created_at=datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                expires_at=datetime(2026, 4, 21, 13, 0, tzinfo=UTC),
            )
        ),
    )
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=_ArtifactStore(),
        session_factory=session_factory,
    )

    for tool_name in ("artifact_read", "artifact_get_metadata", "artifact_get_url"):
        result = await router.execute(
            ToolCall(
                call_id=f"{tool_name}-call",
                name=tool_name,
                arguments={"artifact_id": "toolout_expired"},
            ),
            _session(),
            _agent(),
            ToolRegistry(),
            None,
        )
        assert result.is_error is True
        assert "Artifact not found: toolout_expired" in result.output


@pytest.mark.asyncio
async def test_tool_router_reports_attachment_analysis_diagnostics_on_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    class _Llm:
        async def get_model_info(self, model_id: str, provider_id: str | None = None) -> object:
            del provider_id
            if model_id == "gpt-4o-mini":
                return SimpleNamespace(
                    supports_vision=False,
                    supports_pdf_input=False,
                    supports_audio_input=False,
                    supports_file_input=False,
                )
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            del messages, kwargs
            return {
                "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
                "response_status": "completed",
            }

    class _Session:
        async def get(self, model: object, key: str) -> object | None:
            del model, key
            return None

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="img_1",
                status="attached",
                owner_email="user@example.com",
                namespace="images",
                object_id="img_1",
                filename="image.png",
                mime_type="image/png",
                kind="image",
                size_bytes=8,
            )
        ),
    )
    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_model_routing",
        AsyncMock(return_value=SimpleNamespace(model="vision-route", provider_id="route-provider")),
    )

    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=_Llm(),
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-empty",
            name="artifact_read",
            arguments={"artifact_id": "img_1"},
            runtime_metadata={"resolved_model": "gpt-4o-mini"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is True
    assert result.metadata is not None
    assert (
        result.metadata["_raw_output"]
        == "Attachment analysis route model 'vision-route' returned no content for 'image.png'."
    )
    assert result.metadata["response_status"] == "completed"
    assert result.metadata["finish_reason"] == "stop"
    assert result.metadata["has_content"] is False
    assert result.metadata["analysis_model"] == "vision-route"
    assert result.metadata["analysis_task_type"] == "attachment_analysis"


@pytest.mark.asyncio
async def test_tool_router_handles_artifact_read_svg_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return (
                b'<svg xmlns="http://www.w3.org/2000/svg">\n<text>hello</text>\n</svg>\n',
                "image/svg+xml",
            )

    class _Session:
        async def get(self, model: object, key: str) -> object | None:
            del model, key
            return None

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr(
        "cognis.tools.builtin.artifact_tools.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="svg_1",
                status="attached",
                owner_email="user@example.com",
                namespace="attachments",
                object_id="svg_1",
                filename="icon.svg",
                mime_type="image/svg+xml",
                kind="image",
                size_bytes=64,
            )
        ),
    )

    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=None,
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-svg",
            name="artifact_read",
            arguments={"artifact_id": "svg_1", "offset": 2, "limit": 1},
            runtime_metadata={"resolved_model": "gpt-5.4"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["mime_type"] == "image/svg+xml"
    assert result.metadata["kind"] == "image"
    assert "2: <text>hello</text>" in result.metadata["_raw_output"]


@pytest.mark.asyncio
async def test_tool_router_resolves_generic_artifact_value_refs_for_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="att_1",
                status="attached",
                owner_email="user@example.com",
                namespace="attachments",
                object_id="att_1",
                filename="invoice.pdf",
                mime_type="application/pdf",
                size_bytes=11,
            )
        ),
    )

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="mcp_googleworkspace__send_email",
                description="send email",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "attachments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "filename": {"type": "string"},
                                    "mime_type": {"type": "string"},
                                    "size_bytes": {"type": "integer"},
                                    "content_b64": {"type": "string"},
                                    "download_url": {"type": "string"},
                                },
                                "required": [
                                    "filename",
                                    "mime_type",
                                    "size_bytes",
                                    "content_b64",
                                ],
                            },
                        },
                    },
                    "required": ["attachments"],
                },
                source=ToolSource(
                    type="local_mcp",
                    server_name="googleworkspace",
                    raw_tool_name="send_email",
                ),
                timeout_seconds=1,
            )
        )
    )
    guardrails = _Guardrails()
    executor = _CapturingExecutor()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=_ArtifactStore(),
        session_factory=_session_factory(),
    )

    result = await router.execute(
        ToolCall(
            call_id="artifact-ref-email",
            name="mcp_googleworkspace__send_email",
            arguments={
                "to": "finance@example.com",
                "attachments": [
                    {
                        "filename": "$artifact:att_1.filename",
                        "mime_type": "$artifact:att_1.mime_type",
                        "size_bytes": "$artifact:att_1.size_bytes",
                        "content_b64": "$artifact:att_1.content_b64",
                        "download_url": "$artifact:att_1.public_url",
                    }
                ],
                "literal": "prefix $artifact:att_1.content_b64",
            },
        ),
        _session(),
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, evaluate_arguments, _context = guardrails.last_evaluate_call
    evaluate_attachment = evaluate_arguments["attachments"][0]
    assert evaluate_attachment["filename"] == "invoice.pdf"
    assert evaluate_attachment["mime_type"] == "application/pdf"
    assert evaluate_attachment["size_bytes"] == 11
    assert "resolved at execution" in evaluate_attachment["content_b64"]
    assert "aW1hZ2UtYnl0ZXM=" not in str(evaluate_arguments)
    assert "https://cognis.example.com" not in str(evaluate_arguments)

    assert executor.tool_calls
    executed_arguments = executor.tool_calls[0].arguments
    executed_attachment = executed_arguments["attachments"][0]
    assert executed_attachment["filename"] == "invoice.pdf"
    assert executed_attachment["mime_type"] == "application/pdf"
    assert executed_attachment["size_bytes"] == 11
    assert base64.b64decode(executed_attachment["content_b64"]) == b"image-bytes"
    assert (
        executed_attachment["download_url"]
        == "https://cognis.example.com/attachments/att_1/invoice.pdf"
    )
    assert executed_arguments["literal"] == "prefix $artifact:att_1.content_b64"


@pytest.mark.asyncio
async def test_controller_oauth_mcp_receives_resolved_artifact_value_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_row = _FakeMCPRow(
        server_id="mcp_google",
        name="googleworkspace",
        status="active",
        transport="streamable_http",
        command=None,
        url="https://google.example/mcp",
        args=[],
        env={},
        headers={},
        auth_config={"type": "oauth2"},
        timeout_seconds=30,
    )
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_mcp_server(
        _session: object,
        server_id: str,
        *,
        owner_email: str,
        include_shared: bool,
    ) -> object:
        assert server_id == "mcp_google"
        assert owner_email == "user@example.com"
        assert include_shared is True
        return mcp_row

    async def fake_get_setting_value(_session: object, key: str, default: object) -> object:
        if key == "mcp.tool_timeout_seconds":
            return default
        if key == "mcp.connect_timeout_seconds":
            return default
        raise AssertionError(key)

    class _OAuthService:
        async def inject_authorization_header(self, **kwargs: object) -> object:
            assert kwargs["user_email"] == "user@example.com"
            assert kwargs["server"] is mcp_row
            return SimpleNamespace(
                authorization_required=False,
                headers={"Authorization": "Bearer fresh"},
            )

    class _Client:
        def __init__(self, config: object, secrets: dict[str, str]) -> None:
            del secrets
            self.config = config

        async def connect(self) -> None:
            assert self.config.headers == {"Authorization": "Bearer fresh"}

        async def call_tool(self, raw_name: str, arguments: dict[str, object]) -> object:
            calls.append((raw_name, arguments))
            return {"content": [{"type": "text", "text": "sent"}]}

        async def close(self, *, suppress_cancelled: bool = False) -> None:
            del suppress_cancelled

    monkeypatch.setattr(tool_router_module, "get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(tool_router_module, "get_setting_value", fake_get_setting_value)
    monkeypatch.setattr(
        tool_router_module,
        "build_mcp_client",
        lambda config, secrets: _Client(config, secrets),
    )
    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="att_1",
                status="attached",
                owner_email="user@example.com",
                namespace="attachments",
                object_id="att_1",
                filename="invoice.pdf",
                mime_type="application/pdf",
                size_bytes=11,
            )
        ),
    )

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="mcp_googleworkspace__send_email",
                description="send email",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(
                    type="local_mcp",
                    server_id="mcp_google",
                    server_name="googleworkspace",
                    raw_tool_name="send_email",
                ),
            )
        )
    )
    guardrails = _Guardrails()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=_ArtifactStore(),
        session_factory=_session_factory(),
        mcp_oauth_service=_OAuthService(),
    )

    result = await router.execute(
        ToolCall(
            call_id="controller-artifact-ref-email",
            name="mcp_googleworkspace__send_email",
            arguments={
                "attachments": [
                    {
                        "filename": "$artifact:att_1.filename",
                        "mime_type": "$artifact:att_1.mime_type",
                        "content_b64": "$artifact:att_1.content_b64",
                    }
                ]
            },
        ),
        _session(),
        _agent({"*": Permission.EVALUATE}),
        registry,
        _RemoteExecutor(),
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, evaluate_arguments, _context = guardrails.last_evaluate_call
    assert "aW1hZ2UtYnl0ZXM=" not in str(evaluate_arguments)
    assert "resolved at execution" in evaluate_arguments["attachments"][0]["content_b64"]
    assert calls == [
        (
            "send_email",
            {
                "attachments": [
                    {
                        "filename": "invoice.pdf",
                        "mime_type": "application/pdf",
                        "content_b64": base64.b64encode(b"image-bytes").decode("ascii"),
                    }
                ]
            },
        )
    ]


@pytest.mark.asyncio
async def test_tool_router_resolves_artifact_save_content_for_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CapturingExecutor(_RemoteExecutor):
        def __init__(self) -> None:
            super().__init__(ToolResult(output="saved"))
            self.seen_call: ToolCall | None = None

        async def tool_execute(
            self,
            tool_call: ToolCall,
            timeout_seconds: int | None = None,
            output_chunk_callback: object | None = None,
        ) -> ToolResult:
            del timeout_seconds, output_chunk_callback
            self.seen_call = tool_call
            return self.result

    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="att_1",
                status="attached",
                owner_email="user@example.com",
                namespace="attachments",
                object_id="att_1",
                filename="photo.png",
                mime_type="image/png",
                size_bytes=9,
            )
        ),
    )

    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="artifact_save",
                description="save artifact",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )

    guardrails = _Guardrails()
    executor = _CapturingExecutor()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=_Store(),
        session_factory=_session_factory(),
    )

    result = await router.execute(
        ToolCall(
            call_id="artifact-save-1",
            name="artifact_save",
            arguments={"file_path": "/tmp/photo.png", "source_artifact_id": "att_1"},
        ),
        _session(),
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, evaluate_arguments, _context = guardrails.last_evaluate_call
    assert evaluate_arguments == {
        "file_path": "/tmp/photo.png",
        "source_artifact_id": "att_1",
        "source_artifact_filename": "photo.png",
        "source_artifact_mime_type": "image/png",
        "source_artifact_size_bytes": 9,
    }
    assert executor.seen_call is not None
    assert executor.seen_call.arguments["source_artifact_filename"] == "photo.png"
    assert executor.seen_call.arguments["source_artifact_mime_type"] == "image/png"
    assert (
        base64.b64decode(executor.seen_call.arguments["source_artifact_content_b64"])
        == b"png-bytes"
    )


@pytest.mark.asyncio
async def test_tool_router_resolves_deliverable_artifact_save_content_for_executor(
    task_continuation_db,
) -> None:
    class _CapturingExecutor(_RemoteExecutor):
        def __init__(self) -> None:
            super().__init__(ToolResult(output="saved"))
            self.seen_call: ToolCall | None = None

        async def tool_execute(
            self,
            tool_call: ToolCall,
            timeout_seconds: int | None = None,
            output_chunk_callback: object | None = None,
        ) -> ToolResult:
            del timeout_seconds, output_chunk_callback
            self.seen_call = tool_call
            return self.result

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="artifact_save",
                description="save artifact",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )
    guardrails = _Guardrails()
    executor = _CapturingExecutor()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
    )

    result = await router.execute(
        ToolCall(
            call_id="artifact-save-dlv",
            name="artifact_save",
            arguments={"file_path": "/tmp/report.md", "source_artifact_id": "dlv_owner"},
        ),
        _session().model_copy(update={"user_email": "owner@example.com"}),
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, evaluate_arguments, _context = guardrails.last_evaluate_call
    assert evaluate_arguments == {
        "file_path": "/tmp/report.md",
        "source_artifact_id": "dlv_owner",
        "source_artifact_filename": "Full-report.md",
        "source_artifact_mime_type": "text/markdown",
        "source_artifact_size_bytes": len(b"# Full report\n\nComplete deliverable body."),
    }
    assert executor.seen_call is not None
    assert executor.seen_call.arguments["source_artifact_filename"] == "Full-report.md"
    assert executor.seen_call.arguments["source_artifact_mime_type"] == "text/markdown"
    assert (
        base64.b64decode(executor.seen_call.arguments["source_artifact_content_b64"])
        == b"# Full report\n\nComplete deliverable body."
    )


@pytest.mark.asyncio
async def test_tool_router_exports_and_publishes_managed_descendant_deliverable(
    task_continuation_db,
) -> None:
    from tests.unit.test_artifact_virtual_deliverable_refs import _seed_managed_deliverables

    await _seed_managed_deliverables(task_continuation_db)

    class _CapturingDocumentExecutor(_RemoteExecutor):
        def __init__(self) -> None:
            super().__init__(
                ToolResult(
                    output="generated",
                    attachments=[
                        {
                            "filename": "nested-result.pdf",
                            "mime_type": "application/pdf",
                            "content_b64": base64.b64encode(b"%PDF-managed-export").decode("ascii"),
                            "kind": "pdf",
                            "purpose": "document_output",
                        }
                    ],
                )
            )
            self.seen_call: ToolCall | None = None

        async def tool_execute(
            self,
            tool_call: ToolCall,
            timeout_seconds: int | None = None,
            output_chunk_callback: object | None = None,
        ) -> ToolResult:
            del timeout_seconds, output_chunk_callback
            self.seen_call = tool_call
            return self.result

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="document_generate",
                description="generate document",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )
    executor = _CapturingDocumentExecutor()
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
    )
    controller_session = _session().model_copy(
        update={
            "user_email": "owner@example.com",
            "conversation_id": "conv-controller",
            "agent_id": "agent-owner",
        }
    )

    result = await router.execute(
        ToolCall(
            call_id="document-generate-managed-dlv",
            name="document_generate",
            arguments={
                "source_artifact_id": "dlv_grandchild",
                "filename": "nested-result.pdf",
            },
        ),
        controller_session,
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )

    assert result.is_error is False
    assert executor.seen_call is not None
    assert executor.seen_call.arguments["source_artifact_content"] == "Nested deliverable"
    assert result.attachments
    published_artifact_id = result.attachments[0]["artifact_id"]
    async with task_continuation_db() as session:
        artifact = (
            await session.execute(
                select(ArtifactRecordRow).where(
                    ArtifactRecordRow.artifact_id == published_artifact_id
                )
            )
        ).scalar_one()
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "managed_deliverable_access")
            )
        ).scalar_one()
    assert artifact.owner_email == "owner@example.com"
    assert artifact.conversation_id == "conv-controller"
    assert artifact.session_id == controller_session.session_id
    assert artifact.filename == "nested-result.pdf"
    assert artifact.mime_type == "application/pdf"
    assert audit.agent_id == "agent-owner"
    assert audit.details["creator_agent_id"] == "agent-grandchild"
    assert audit.details["creator_conversation_id"] == "conv-grandchild"


@pytest.mark.asyncio
async def test_tool_router_denies_managed_descendant_export_to_unrelated_agent(
    task_continuation_db,
) -> None:
    from tests.unit.test_artifact_virtual_deliverable_refs import _seed_managed_deliverables

    await _seed_managed_deliverables(task_continuation_db)
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="document_generate",
                description="generate document",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )
    executor = _RemoteExecutor(ToolResult(output="must not execute"))
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
    )

    result = await router.execute(
        ToolCall(
            call_id="document-generate-denied-managed-dlv",
            name="document_generate",
            arguments={"source_artifact_id": "dlv_grandchild"},
        ),
        _session().model_copy(
            update={
                "user_email": "owner@example.com",
                "conversation_id": "conv-unrelated",
                "agent_id": "agent-unrelated",
            }
        ),
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )

    assert result.is_error is True
    assert "Artifact not found: dlv_grandchild" in result.output
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_tool_router_preserves_task_scope_for_deliverable_content_refs(
    task_continuation_db,
) -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="artifact_save",
                description="save artifact",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )
    router = ToolRouter(
        guardrails=_Guardrails(),
        artifact_store=_ArtifactStore(),
        session_factory=task_continuation_db,
    )

    result = await router.execute(
        ToolCall(
            call_id="artifact-save-out-of-scope-dlv",
            name="artifact_save",
            arguments={"file_path": "/tmp/report.md", "source_artifact_id": "dlv_sibling"},
            runtime_metadata={
                "conversation_context": {
                    "platform_data": {"forked_from": "task", "task_id": "task-owner"}
                }
            },
        ),
        _session().model_copy(update={"user_email": "owner@example.com"}),
        _agent({"*": Permission.EVALUATE}),
        registry,
        _RemoteExecutor(ToolResult(output="saved")),
    )

    assert result.is_error is True
    assert "Artifact not found: dlv_sibling" in result.output


@pytest.mark.asyncio
async def test_tool_router_resolves_deliverable_browser_upload_guardrails_and_payload(
    task_continuation_db,
) -> None:
    class _CapturingExecutor(_RemoteExecutor):
        def __init__(self) -> None:
            super().__init__(ToolResult(output="uploaded"))
            self.seen_call: ToolCall | None = None

        async def tool_execute(
            self,
            tool_call: ToolCall,
            timeout_seconds: int | None = None,
            output_chunk_callback: object | None = None,
        ) -> ToolResult:
            del timeout_seconds, output_chunk_callback
            self.seen_call = tool_call
            return self.result

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="browser_upload",
                description="upload artifact",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
                non_bypassable=True,
            )
        )
    )
    guardrails = _Guardrails()
    executor = _CapturingExecutor()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=task_continuation_db.artifact_store,
        session_factory=task_continuation_db,
    )

    result = await router.execute(
        ToolCall(
            call_id="browser-upload-dlv",
            name="browser_upload",
            arguments={
                "session_id": "browser-1",
                "ref": "e1",
                "source_artifact_ids": ["dlv_owner"],
            },
        ),
        _session().model_copy(update={"user_email": "owner@example.com"}),
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, evaluate_arguments, _context = guardrails.last_evaluate_call
    assert evaluate_arguments["source_artifacts"] == [
        {
            "artifact_id": "dlv_owner",
            "filename": "Full-report.md",
            "mime_type": "text/markdown",
            "size_bytes": len(b"# Full report\n\nComplete deliverable body."),
        }
    ]
    assert "content_b64" not in str(evaluate_arguments)
    assert executor.seen_call is not None
    assert executor.seen_call.arguments["source_artifacts"][0]["filename"] == "Full-report.md"
    assert executor.seen_call.arguments["source_artifacts"][0]["mime_type"] == "text/markdown"
    assert (
        base64.b64decode(executor.seen_call.arguments["source_artifacts"][0]["content_b64"])
        == b"# Full report\n\nComplete deliverable body."
    )


@pytest.mark.asyncio
async def test_tool_router_resolves_browser_upload_artifacts_without_guardrails_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CapturingExecutor(_RemoteExecutor):
        def __init__(self) -> None:
            super().__init__(ToolResult(output="uploaded"))
            self.seen_call: ToolCall | None = None

        async def tool_execute(
            self,
            tool_call: ToolCall,
            timeout_seconds: int | None = None,
            output_chunk_callback: object | None = None,
        ) -> ToolResult:
            del timeout_seconds, output_chunk_callback
            self.seen_call = tool_call
            return self.result

    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="att_1",
                status="attached",
                owner_email="user@example.com",
                namespace="attachments",
                object_id="att_1",
                filename="photo.png",
                mime_type="image/png",
                size_bytes=9,
            )
        ),
    )

    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="browser_upload",
                description="upload artifact",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
                non_bypassable=True,
            )
        )
    )

    guardrails = _Guardrails()
    executor = _CapturingExecutor()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=_Store(),
        session_factory=_session_factory(),
    )

    result = await router.execute(
        ToolCall(
            call_id="browser-upload-1",
            name="browser_upload",
            arguments={
                "session_id": "browser-1",
                "ref": "e1",
                "source_artifact_ids": ["att_1"],
                "source_artifacts": [
                    {
                        "filename": "evil.bin",
                        "mime_type": "application/octet-stream",
                        "content_b64": base64.b64encode(b"evil").decode("ascii"),
                    }
                ],
            },
        ),
        _session(),
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, evaluate_arguments, _context = guardrails.last_evaluate_call
    assert evaluate_arguments["source_artifacts"] == [
        {
            "artifact_id": "att_1",
            "filename": "photo.png",
            "mime_type": "image/png",
            "size_bytes": 9,
        }
    ]
    assert "content_b64" not in str(evaluate_arguments)
    assert executor.seen_call is not None
    assert executor.seen_call.arguments["source_artifacts"][0]["filename"] == "photo.png"
    assert (
        base64.b64decode(executor.seen_call.arguments["source_artifacts"][0]["content_b64"])
        == b"png-bytes"
    )


@pytest.mark.asyncio
async def test_tool_router_omits_document_asset_binary_payloads_from_guardrails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CapturingExecutor(_RemoteExecutor):
        def __init__(self) -> None:
            super().__init__(ToolResult(output="generated"))
            self.seen_call: ToolCall | None = None

        async def tool_execute(
            self,
            tool_call: ToolCall,
            timeout_seconds: int | None = None,
            output_chunk_callback: object | None = None,
        ) -> ToolResult:
            del timeout_seconds, output_chunk_callback
            self.seen_call = tool_call
            return self.result

    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            side_effect=[
                SimpleNamespace(
                    artifact_id="asset_1",
                    status="attached",
                    owner_email="user@example.com",
                    namespace="attachments",
                    object_id="asset_1",
                    filename="diagram.png",
                    mime_type="image/png",
                    size_bytes=11,
                ),
                SimpleNamespace(
                    artifact_id="asset_1",
                    status="attached",
                    owner_email="user@example.com",
                    namespace="attachments",
                    object_id="asset_1",
                    filename="diagram.png",
                    mime_type="image/png",
                    size_bytes=11,
                ),
            ]
        ),
    )

    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"image-bytes", "image/png"

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="document_generate",
                description="document generate",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )

    guardrails = _Guardrails()
    executor = _CapturingExecutor()
    router = ToolRouter(
        guardrails=guardrails,
        artifact_store=_Store(),
        session_factory=_session_factory(),
    )

    result = await router.execute(
        ToolCall(
            call_id="document-generate-1",
            name="document_generate",
            arguments={
                "content": "![diag](asset:diag)",
                "assets": [{"name": "diag", "artifact_id": "asset_1"}],
            },
        ),
        _session(),
        _agent({"*": Permission.EVALUATE}),
        registry,
        executor,
    )

    assert result.is_error is False
    assert guardrails.last_evaluate_call is not None
    _session_id, _tool_name, evaluate_arguments, _context = guardrails.last_evaluate_call
    assert evaluate_arguments["assets"] == [
        {
            "name": "diag",
            "artifact_id": "asset_1",
            "filename": "diagram.png",
            "mime_type": "image/png",
            "size_bytes": 11,
        }
    ]
    assert executor.seen_call is not None
    assert executor.seen_call.arguments["assets"][0]["filename"] == "diagram.png"
    assert executor.seen_call.arguments["assets"][0]["mime_type"] == "image/png"
    assert (
        base64.b64decode(executor.seen_call.arguments["assets"][0]["content_b64"]) == b"image-bytes"
    )


@pytest.mark.asyncio
async def test_tool_router_postprocesses_binary_read_with_attachment_analysis_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store(_ArtifactStore):
        async def async_load(
            self, namespace: str, object_id: str, filename: str
        ) -> tuple[bytes, str]:
            del namespace, object_id, filename
            return b"png-bytes", "image/png"

    class _Llm:
        async def get_model_info(self, model_id: str, provider_id: str | None = None) -> object:
            del provider_id
            if model_id == "gpt-5.4":
                return SimpleNamespace(
                    supports_vision=False,
                    supports_pdf_input=False,
                    supports_audio_input=False,
                    supports_file_input=False,
                )
            return SimpleNamespace(
                supports_vision=True,
                supports_pdf_input=False,
                supports_audio_input=False,
                supports_file_input=False,
            )

        async def generate(
            self, messages: list[dict[str, object]], **kwargs: object
        ) -> dict[str, object]:
            del messages, kwargs
            return {"choices": [{"message": {"content": "It is a generated banner."}}]}

    class _Session:
        async def commit(self) -> None:
            return None

        async def execute(self, stmt: object) -> object:
            del stmt

            class _Result:
                def scalar_one_or_none(self) -> object:
                    return SimpleNamespace(model="gpt-4o", provider_id="openai")

            return _Result()

    @asynccontextmanager
    async def session_factory() -> object:
        yield _Session()

    monkeypatch.setattr("cognis.core.tool_router.create_artifact_record", AsyncMock())
    monkeypatch.setattr(
        "cognis.core.tool_router.get_artifact_record",
        AsyncMock(
            return_value=SimpleNamespace(
                artifact_id="att_1",
                status="attached",
                owner_email="user@example.com",
                namespace="attachments",
                object_id="att_1",
                filename="photo.png",
            )
        ),
    )

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="read",
                description="read",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                timeout_seconds=1,
            )
        )
    )

    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=_Llm(),
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="read-1",
            name="read",
            arguments={"file_path": "/tmp/photo.png"},
            runtime_metadata={"resolved_model": "gpt-5.4"},
        ),
        _session(),
        _agent(),
        registry,
        _RemoteExecutor(
            ToolResult(
                output="Prepared binary file.",
                metadata={"attachment_analysis_request": {"source": "read"}},
                attachments=[
                    {
                        "filename": "photo.png",
                        "mime_type": "image/png",
                        "content_b64": base64.b64encode(b"png-bytes").decode("ascii"),
                    }
                ],
            )
        ),
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["_raw_output"] == "It is a generated banner."
    assert result.attachments is None

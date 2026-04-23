from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.tool_router import ToolRoute, ToolRouter
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.credential import CredentialAccessError
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


class _ArtifactStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, str, bytes, str, str | None]] = []

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

    async def async_get_public_url(self, namespace: str, object_id: str, filename: str) -> str:
        return f"https://cognis.example.com/{namespace}/{object_id}/{filename}"

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


def _readonly_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="memory_search",
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

    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=_Llm(),
        artifact_store=_Store(),
        session_factory=session_factory,
    )

    result = await router.execute(
        ToolCall(
            call_id="art-1",
            name="artifact_read",
            arguments={"artifact_id": "img_1"},
            runtime_metadata={"resolved_model": "gpt-4o-mini"},
        ),
        _session(),
        _agent(),
        ToolRegistry(),
        None,
    )

    assert result.is_error is False
    assert result.metadata is not None
    assert result.metadata["_raw_output"] == "It is a blue square."


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

    router = ToolRouter(
        guardrails=_Guardrails(),
        llm=_Llm(),
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
    assert result.attachments is None
    assert result.metadata is not None
    assert result.metadata["_raw_output"] == "It is a generated banner."
    assert result.metadata["analysis_model"] == "gpt-5.4"
    assert result.metadata["used_attachment_analysis_route"] is False
    assert session.attachment_analysis_lookups == 0


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
    assert '"namespace": "documents"' in result.metadata["_raw_output"]


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
        == "Current model 'gpt-4o-mini' returned no content while inspecting 'image.png'."
    )
    assert result.metadata["response_status"] == "completed"
    assert result.metadata["finish_reason"] == "stop"
    assert result.metadata["has_content"] is False
    assert result.metadata["analysis_model"] == "gpt-4o-mini"


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
async def test_tool_router_resolves_artifact_save_content_for_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CapturingExecutor(_RemoteExecutor):
        def __init__(self) -> None:
            super().__init__(ToolResult(output="saved"))
            self.seen_call: ToolCall | None = None

        async def tool_execute(
            self, tool_call: ToolCall, timeout_seconds: int | None = None
        ) -> ToolResult:
            del timeout_seconds
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
async def test_tool_router_omits_document_asset_binary_payloads_from_guardrails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CapturingExecutor(_RemoteExecutor):
        def __init__(self) -> None:
            super().__init__(ToolResult(output="generated"))
            self.seen_call: ToolCall | None = None

        async def tool_execute(
            self, tool_call: ToolCall, timeout_seconds: int | None = None
        ) -> ToolResult:
            del timeout_seconds
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

        async def get(self, model: object, key: str) -> object | None:
            del model
            if key == "attachment_analysis":
                return SimpleNamespace(model="gpt-4o", provider_id="openai")
            return None

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

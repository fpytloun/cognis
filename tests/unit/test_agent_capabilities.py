"""Unit tests for per-agent backend capabilities (spec 33)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cognis.models.agent import AgentCapabilities, AgentDefinition
from cognis.providers.backends import get_backend, list_backends

# ---------------------------------------------------------------------------
# AgentCapabilities model
# ---------------------------------------------------------------------------


class TestAgentCapabilities:
    def test_defaults(self) -> None:
        cap = AgentCapabilities()
        assert cap.memory_backend == "mnemory"
        assert cap.guardrails_backend == "intaris"
        assert cap.memory_enabled is True
        assert cap.guardrails_enabled is True

    def test_none_backends(self) -> None:
        cap = AgentCapabilities(memory_backend="none", guardrails_backend="none")
        assert cap.memory_enabled is False
        assert cap.guardrails_enabled is False

    def test_invalid_memory_backend(self) -> None:
        with pytest.raises(ValueError, match="Unknown memory_backend"):
            AgentCapabilities(memory_backend="invalid")

    def test_invalid_guardrails_backend(self) -> None:
        with pytest.raises(ValueError, match="Unknown guardrails_backend"):
            AgentCapabilities(guardrails_backend="invalid")

    def test_agent_definition_default_capabilities(self) -> None:
        agent = AgentDefinition(
            agent_id="test",
            owner_email="test@example.com",
            name="Test",
        )
        assert agent.capabilities.memory_backend == "mnemory"
        assert agent.capabilities.guardrails_backend == "intaris"

    def test_agent_definition_custom_capabilities(self) -> None:
        agent = AgentDefinition(
            agent_id="test",
            owner_email="test@example.com",
            name="Test",
            capabilities=AgentCapabilities(
                memory_backend="none",
                guardrails_backend="none",
            ),
        )
        assert agent.capabilities.memory_enabled is False
        assert agent.capabilities.guardrails_enabled is False


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------


class TestBackendRegistry:
    def test_list_memory_backends(self) -> None:
        backends = list_backends("memory")
        assert "mnemory" in backends
        assert "none" in backends

    def test_list_guardrails_backends(self) -> None:
        backends = list_backends("guardrails")
        assert "intaris" in backends
        assert "none" in backends

    def test_get_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown memory backend"):
            get_backend("memory", "unknown-backend")

    def test_mnemory_backend_returns_registry_memory(self) -> None:
        mock_config = MagicMock()
        mock_registry = MagicMock()
        mock_registry.memory = MagicMock(name="mnemory_provider")

        backend = get_backend("memory", "mnemory")
        result = backend.factory(mock_config, mock_registry)
        assert result is mock_registry.memory

    def test_null_memory_backend_returns_null_provider(self) -> None:
        from cognis.providers.backends.memory.null import NullMemoryProvider

        mock_config = MagicMock()
        mock_registry = MagicMock()

        backend = get_backend("memory", "none")
        result = backend.factory(mock_config, mock_registry)
        assert isinstance(result, NullMemoryProvider)

    def test_intaris_backend_returns_registry_guardrails(self) -> None:
        mock_config = MagicMock()
        mock_registry = MagicMock()
        mock_registry.guardrails = MagicMock(name="intaris_provider")

        backend = get_backend("guardrails", "intaris")
        result = backend.factory(mock_config, mock_registry)
        assert result is mock_registry.guardrails

    def test_null_guardrails_backend_returns_no_guardrails_provider(self) -> None:
        from cognis.providers.backends.guardrails.null import NoGuardrailsProvider

        mock_config = MagicMock()
        mock_registry = MagicMock()
        mock_registry.guardrails = MagicMock()

        backend = get_backend("guardrails", "none")
        result = backend.factory(mock_config, mock_registry)
        assert isinstance(result, NoGuardrailsProvider)


# ---------------------------------------------------------------------------
# NullMemoryProvider
# ---------------------------------------------------------------------------


class TestNullMemoryProvider:
    @pytest.fixture
    def provider(self) -> Any:
        from cognis.providers.backends.memory.null import NullMemoryProvider

        return NullMemoryProvider()

    @pytest.mark.asyncio
    async def test_recall_returns_empty(self, provider: Any) -> None:
        result = await provider.recall("test query")
        assert result["memories"] == []

    @pytest.mark.asyncio
    async def test_remember_is_noop(self, provider: Any) -> None:
        # Should not raise
        await provider.remember("sess_1", [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_load_session_identity_returns_empty(self, provider: Any) -> None:
        result = await provider.load_session_identity()
        assert result == {}

    @pytest.mark.asyncio
    async def test_bootstrap_agent_is_noop(self, provider: Any) -> None:
        await provider.bootstrap_agent(MagicMock())

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, provider: Any) -> None:
        health = await provider.health()
        assert health.status == "ok"


# ---------------------------------------------------------------------------
# NoGuardrailsProvider
# ---------------------------------------------------------------------------


class TestNoGuardrailsProvider:
    @pytest.fixture
    def intaris_mock(self) -> MagicMock:
        mock = MagicMock()
        mock.record_events = AsyncMock(return_value=MagicMock())
        mock.read_events = AsyncMock(return_value=MagicMock())
        mock.create_session = AsyncMock()
        mock.get_session = AsyncMock(return_value=MagicMock())
        mock.health = AsyncMock(return_value=MagicMock(status="ok"))
        return mock

    @pytest.fixture
    def provider(self, intaris_mock: MagicMock) -> Any:
        from cognis.providers.backends.guardrails.null import NoGuardrailsProvider

        return NoGuardrailsProvider(intaris_mock)

    @pytest.mark.asyncio
    async def test_evaluate_auto_approves(self, provider: Any) -> None:
        result = await provider.evaluate(
            session_id="sess_1",
            tool_name="bash",
            arguments={"command": "ls"},
        )
        assert result.decision == "approve"
        assert result.call_id == "capability-disabled"

    @pytest.mark.asyncio
    async def test_evaluate_approves_non_bypassable_too(self, provider: Any) -> None:
        """guardrails=none means no guardrails, including non-bypassable tools."""
        result = await provider.evaluate(
            session_id="sess_1",
            tool_name="some_critical_tool",
            arguments={},
        )
        assert result.decision == "approve"

    @pytest.mark.asyncio
    async def test_report_reasoning_is_noop(self, provider: Any) -> None:
        result = await provider.report_reasoning("sess_1", "some content")
        # Should return a valid ReasoningReportResult without calling LLM
        assert result.ok is True
        assert result.intention is None

    @pytest.mark.asyncio
    async def test_record_events_delegates_to_intaris(
        self, provider: Any, intaris_mock: MagicMock
    ) -> None:
        """Event store operations must still go through Intaris."""
        from cognis.models.session import SessionEvent

        events = [SessionEvent(type="user_message", data={"content": "hi"})]
        await provider.record_events("sess_1", events)
        intaris_mock.record_events.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_session_delegates_to_intaris(
        self, provider: Any, intaris_mock: MagicMock
    ) -> None:
        await provider.create_session("sess_1", "test", "agent_1")
        intaris_mock.create_session.assert_called_once()


# ---------------------------------------------------------------------------
# ToolRouter capability gating
# ---------------------------------------------------------------------------


class TestToolRouterCapabilityGating:
    def test_memory_tools_hidden_when_memory_backend_none(self) -> None:
        """memory_backend=none fully disables memory tools in the model tool list."""
        from cognis.api.runtime_support import select_static_tools

        agent = AgentDefinition(
            agent_id="test",
            owner_email="test@example.com",
            name="Test",
            capabilities=AgentCapabilities(memory_backend="none"),
            tools={
                "builtin_tools": ["*"],
                "allow_tools": ["memory_search", "builtin:memory_search"],
            },
        )

        selected = select_static_tools(agent)

        assert all(tool.category != "memory" for tool in selected)

    def test_memory_tools_visible_when_memory_backend_enabled(self) -> None:
        """The default mnemory backend keeps memory tools available."""
        from cognis.api.runtime_support import select_static_tools

        agent = AgentDefinition(
            agent_id="test",
            owner_email="test@example.com",
            name="Test",
        )

        selected = select_static_tools(agent)

        assert any(tool.category == "memory" for tool in selected)

    @pytest.mark.asyncio
    async def test_memory_tool_execution_rejected_when_memory_backend_none(self) -> None:
        """A stale or forged memory tool call is rejected when memory is disabled."""
        from cognis.core.tool_router import ToolRouter
        from cognis.models.tool import ToolCall
        from cognis.tools.registry import ToolRegistry

        memory = MagicMock()
        router = ToolRouter(guardrails=MagicMock(), memory=memory)
        agent = AgentDefinition(
            agent_id="test",
            owner_email="test@example.com",
            name="Test",
            capabilities=AgentCapabilities(memory_backend="none"),
        )
        session = MagicMock()
        session.session_id = "sess_1"
        session.user_email = "test@example.com"
        tool_call = ToolCall(
            call_id="call_1",
            name="memory_search",
            arguments={"query": "test"},
        )

        result = await router.execute(
            tool_call,
            session,
            agent,
            ToolRegistry(),
            executor=MagicMock(),
        )

        assert result.is_error is True
        assert "Memory backend is disabled" in result.output
        memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_skipped_when_guardrails_none(self) -> None:
        """When guardrails_backend=none, evaluate returns approve without calling Intaris."""
        from cognis.core.tool_router import ToolRouter

        mock_guardrails = MagicMock()
        mock_guardrails.evaluate = AsyncMock()

        router = ToolRouter(guardrails=mock_guardrails)

        agent = AgentDefinition(
            agent_id="test",
            owner_email="test@example.com",
            name="Test",
            capabilities=AgentCapabilities(guardrails_backend="none"),
        )
        session = MagicMock()
        session.session_id = "sess_1"
        session.intaris_session_id = "sess_1"

        # Create a mock tool call and registry
        tool_call = MagicMock()
        tool_call.name = "bash"
        tool_call.arguments = {"command": "ls"}

        registry = MagicMock()
        registered_tool = MagicMock()
        registered_tool.definition.name = "bash"
        registered_tool.definition.non_bypassable = False
        registered_tool.definition.read_only = False
        registry.get.return_value = registered_tool

        # Patch _evaluation_context to avoid DB calls
        with patch.object(router, "_evaluation_context", AsyncMock(return_value={})):
            result = await router.evaluate_tool_call(tool_call, agent, session, registry)

        assert result.decision == "approve"
        assert result.source == "capability-disabled"
        # Intaris evaluate must NOT have been called
        mock_guardrails.evaluate.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_called_when_guardrails_intaris(self) -> None:
        """When guardrails_backend=intaris (default), evaluate calls Intaris normally."""
        from cognis.core.tool_router import ToolRouter
        from cognis.models.tool import EvaluationResult

        mock_guardrails = MagicMock()
        mock_guardrails.evaluate = AsyncMock(
            return_value=EvaluationResult(
                decision="approve",
                reasoning="ok",
                risk=None,
                path=None,
                latency_ms=10,
                call_id="eval_1",
            )
        )

        router = ToolRouter(guardrails=mock_guardrails)

        agent = AgentDefinition(
            agent_id="test",
            owner_email="test@example.com",
            name="Test",
            # Default capabilities — intaris guardrails
        )
        session = MagicMock()
        session.session_id = "sess_1"
        session.intaris_session_id = "sess_1"

        tool_call = MagicMock()
        tool_call.name = "bash"
        tool_call.arguments = {"command": "ls"}

        registry = MagicMock()
        registered_tool = MagicMock()
        registered_tool.definition.name = "bash"
        registered_tool.definition.non_bypassable = False
        registered_tool.definition.read_only = False
        registry.get.return_value = registered_tool

        with (
            patch.object(router, "_evaluation_context", AsyncMock(return_value={})),
            patch.object(router, "_get_cached_decision", return_value=None),
        ):
            result = await router.evaluate_tool_call(tool_call, agent, session, registry)

        # Intaris evaluate MUST have been called
        mock_guardrails.evaluate.assert_called_once()
        assert result.decision == "approve"

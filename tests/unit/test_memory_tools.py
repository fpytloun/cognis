"""Unit tests for built-in memory tools."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.tools.builtin.memory import (
    ALL_MEMORY_TOOLS,
    handle_memory_tool,
    is_memory_tool,
    memory_tools,
)


class TestMemoryToolDefinitions:
    """Test memory tool definitions."""

    def test_memory_tools_count(self) -> None:
        defs = memory_tools()
        assert len(defs) == 14

    def test_all_have_builtin_source(self) -> None:
        for tool in ALL_MEMORY_TOOLS:
            assert tool.source.type == "builtin"

    def test_all_have_memory_category(self) -> None:
        for tool in ALL_MEMORY_TOOLS:
            assert tool.category == "memory"

    def test_tool_names(self) -> None:
        names = {t.name for t in ALL_MEMORY_TOOLS}
        assert names == {
            "memory_search",
            "memory_find",
            "memory_ask",
            "memory_add",
            "memory_add_batch",
            "memory_update",
            "memory_delete",
            "memory_list",
            "memory_categories",
            "memory_recent",
            "memory_save_artifact",
            "memory_get_artifact",
            "memory_list_artifacts",
            "memory_delete_artifact",
        }

    def test_is_memory_tool(self) -> None:
        assert is_memory_tool("memory_search") is True
        assert is_memory_tool("memory_add") is True
        assert is_memory_tool("bash") is False
        assert is_memory_tool("read") is False

    def test_read_only_tools(self) -> None:
        read_only = {t.name for t in ALL_MEMORY_TOOLS if t.read_only}
        assert read_only == {
            "memory_search",
            "memory_find",
            "memory_ask",
            "memory_list",
            "memory_categories",
            "memory_recent",
            "memory_get_artifact",
            "memory_list_artifacts",
        }

    def test_write_tools(self) -> None:
        write = {t.name for t in ALL_MEMORY_TOOLS if not t.read_only}
        assert write == {
            "memory_add",
            "memory_add_batch",
            "memory_update",
            "memory_delete",
            "memory_save_artifact",
            "memory_delete_artifact",
        }


class TestMemoryToolHandlers:
    """Test memory tool handler dispatch."""

    def _mock_provider(self, response_data: Any = None) -> MagicMock:
        """Create a mock Mnemory provider with an httpx-like client."""
        provider = MagicMock()
        provider._headers = MagicMock(return_value={"Authorization": "Bearer test"})

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=response_data or {})

        client = AsyncMock()
        client.post = AsyncMock(return_value=mock_response)
        client.get = AsyncMock(return_value=mock_response)
        client.put = AsyncMock(return_value=mock_response)
        client.delete = AsyncMock(return_value=mock_response)
        provider.client = client

        return provider

    @pytest.mark.asyncio()
    async def test_memory_search(self) -> None:
        provider = self._mock_provider({"results": [{"content": "test"}]})
        result = await handle_memory_tool(
            "memory_search", {"query": "test"}, provider, "agent1", "user@test.com"
        )
        assert not result.is_error
        provider.client.post.assert_called_once()
        call_args = provider.client.post.call_args
        assert call_args[0][0] == "/api/memories/search"

    @pytest.mark.asyncio()
    async def test_memory_add(self) -> None:
        provider = self._mock_provider({"memory_id": "mem_123"})
        result = await handle_memory_tool(
            "memory_add", {"content": "test fact"}, provider, "agent1", "user@test.com"
        )
        assert not result.is_error
        provider.client.post.assert_called_once()
        call_args = provider.client.post.call_args
        assert call_args[0][0] == "/api/memories"

    @pytest.mark.asyncio()
    async def test_memory_delete(self) -> None:
        provider = self._mock_provider()
        result = await handle_memory_tool(
            "memory_delete", {"memory_id": "mem_123"}, provider, "agent1", "user@test.com"
        )
        assert not result.is_error
        assert "deleted" in result.output
        provider.client.delete.assert_called_once()

    @pytest.mark.asyncio()
    async def test_memory_list(self) -> None:
        provider = self._mock_provider({"items": []})
        result = await handle_memory_tool(
            "memory_list", {"limit": 10}, provider, "agent1", "user@test.com"
        )
        assert not result.is_error
        provider.client.get.assert_called_once()

    @pytest.mark.asyncio()
    async def test_memory_categories(self) -> None:
        provider = self._mock_provider({"categories": []})
        result = await handle_memory_tool(
            "memory_categories", {}, provider, "agent1", "user@test.com"
        )
        assert not result.is_error

    @pytest.mark.asyncio()
    async def test_memory_update(self) -> None:
        provider = self._mock_provider({"memory_id": "mem_123"})
        result = await handle_memory_tool(
            "memory_update",
            {"memory_id": "mem_123", "content": "updated"},
            provider,
            "agent1",
            "user@test.com",
        )
        assert not result.is_error
        provider.client.put.assert_called_once()

    @pytest.mark.asyncio()
    async def test_memory_save_artifact(self) -> None:
        provider = self._mock_provider({"artifact_id": "art_123"})
        result = await handle_memory_tool(
            "memory_save_artifact",
            {"memory_id": "mem_123", "content": "detailed report"},
            provider,
            "agent1",
            "user@test.com",
        )
        assert not result.is_error
        provider.client.post.assert_called_once()

    @pytest.mark.asyncio()
    async def test_memory_get_artifact(self) -> None:
        provider = self._mock_provider({"content": "report text"})
        result = await handle_memory_tool(
            "memory_get_artifact",
            {"memory_id": "mem_123", "artifact_id": "art_123"},
            provider,
            "agent1",
            "user@test.com",
        )
        assert not result.is_error

    @pytest.mark.asyncio()
    async def test_unknown_memory_tool(self) -> None:
        provider = self._mock_provider()
        result = await handle_memory_tool(
            "memory_nonexistent", {}, provider, "agent1", "user@test.com"
        )
        assert result.is_error
        assert "Unknown memory tool" in result.output

    @pytest.mark.asyncio()
    async def test_provider_error_handled(self) -> None:
        provider = self._mock_provider()
        provider.client.post = AsyncMock(side_effect=Exception("Connection refused"))
        result = await handle_memory_tool(
            "memory_search", {"query": "test"}, provider, "agent1", "user@test.com"
        )
        assert result.is_error
        assert "failed" in result.output.lower()


class TestWorkflowToolDefinitions:
    """Test workflow tool definitions."""

    def test_workflow_tools_count(self) -> None:
        from cognis.tools.builtin.workflow import workflow_tools

        defs = workflow_tools()
        assert len(defs) == 4

    def test_workflow_tool_names(self) -> None:
        from cognis.tools.builtin.workflow import workflow_tools

        names = {t.name for t in workflow_tools()}
        assert names == {
            "step_complete",
            "step_request_input",
            "step_todo_write",
            "step_todo_list",
        }

    def test_all_have_workflow_category(self) -> None:
        from cognis.tools.builtin.workflow import workflow_tools

        for tool in workflow_tools():
            assert tool.category == "workflow"
            assert tool.source.type == "builtin"


class TestStaticToolDefinitionsComplete:
    """Test that static_tool_definitions includes all tool sources."""

    def test_includes_all_sources(self) -> None:
        from cognis.api.runtime_support import static_tool_definitions

        defs = static_tool_definitions()
        categories = {t.category for t in defs}
        assert "system" in categories
        assert "orchestration" in categories
        assert "workflow" in categories
        assert "memory" in categories
        assert "filesystem" in categories
        assert "search" in categories
        assert "shell" in categories
        assert "web" in categories

    def test_total_count(self) -> None:
        from cognis.api.runtime_support import static_tool_definitions

        defs = static_tool_definitions()
        # 2 system + 9 orchestration + 4 workflow + 14 memory + 10 executor = 39
        assert len(defs) == 39


class TestToolRouterMemoryClassification:
    """Test that the tool router classifies memory tools correctly."""

    def test_memory_tools_classified(self) -> None:
        from cognis.core.tool_router import ToolRoute, ToolRouter
        from cognis.tools.registry import ToolRegistry

        router = ToolRouter(guardrails=None)
        registry = ToolRegistry()

        assert router.classify("memory_search", registry) == ToolRoute.MEMORY
        assert router.classify("memory_add", registry) == ToolRoute.MEMORY
        assert router.classify("memory_delete", registry) == ToolRoute.MEMORY
        assert router.classify("bash", registry) == ToolRoute.UNKNOWN  # not in registry

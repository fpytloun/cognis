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
        assert len(defs) == 15

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
            "memory_get_artifact_url",
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
            "memory_get_artifact_url",
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
    async def test_memory_get_artifact_url(self) -> None:
        provider = self._mock_provider({"url": "https://example.com/dl", "expires_in": 3600})
        result = await handle_memory_tool(
            "memory_get_artifact_url",
            {"memory_id": "mem_123", "artifact_id": "art_456"},
            provider,
            "agent1",
            "user@test.com",
        )
        assert not result.is_error
        provider.client.post.assert_called_once()
        call_args = provider.client.post.call_args
        assert "/download-token" in call_args[0][0]

    @pytest.mark.asyncio()
    async def test_memory_get_artifact_url_with_ttl(self) -> None:
        provider = self._mock_provider({"url": "https://example.com/dl", "expires_in": 7200})
        result = await handle_memory_tool(
            "memory_get_artifact_url",
            {"memory_id": "mem_123", "artifact_id": "art_456", "ttl": 7200},
            provider,
            "agent1",
            "user@test.com",
        )
        assert not result.is_error
        call_args = provider.client.post.call_args
        assert call_args[1]["json"]["ttl"] == 7200

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
        assert len(defs) == 7

    def test_workflow_tool_names(self) -> None:
        from cognis.tools.builtin.workflow import workflow_tools

        names = {t.name for t in workflow_tools()}
        assert {
            "step_complete",
            "step_request_input",
            "step_todo_write",
            "step_todo_list",
        }.issubset(names)

    def test_all_have_workflow_category(self) -> None:
        from cognis.tools.builtin.workflow import workflow_tools

        for tool in workflow_tools():
            assert tool.category == "workflow"
            assert tool.source.type == "builtin"

    def test_step_todo_write_uses_completed_status(self) -> None:
        from cognis.tools.builtin.workflow import STEP_TODO_WRITE_TOOL

        statuses = STEP_TODO_WRITE_TOOL.parameters["properties"]["todos"]["items"]["properties"][
            "status"
        ]["enum"]
        assert statuses == ["pending", "in_progress", "completed", "cancelled"]


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
        assert "shell" in categories
        assert "web" in categories

    def test_total_count(self) -> None:
        from cognis.api.runtime_support import static_tool_definitions

        defs = static_tool_definitions()
        assert len(defs) >= 66


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
        assert router.classify("memory_get_artifact_url", registry) == ToolRoute.MEMORY
        assert router.classify("bash", registry) == ToolRoute.UNKNOWN  # not in registry


class TestAdaptMemoryInstructions:
    """Test MCP-to-Cognis tool name adaptation in instructions."""

    def test_basic_replacements(self) -> None:
        from cognis.core.context import _adapt_memory_instructions

        text = "Use search_memories to find things. Call add_memory to store."
        result = _adapt_memory_instructions(text)
        assert "memory_search" in result
        assert "memory_add" in result
        assert "search_memories" not in result
        assert "add_memory" not in result

    def test_add_memories_before_add_memory(self) -> None:
        """add_memories must be replaced before add_memory to avoid partial match."""
        from cognis.core.context import _adapt_memory_instructions

        text = "Use add_memories for batch and add_memory for single."
        result = _adapt_memory_instructions(text)
        assert "memory_add_batch" in result
        assert "memory_add" in result
        # Ensure add_memories didn't become "memory_add_batch" via partial
        assert "memory_addies" not in result

    def test_get_artifact_url_before_get_artifact(self) -> None:
        """get_artifact_url must be replaced before get_artifact."""
        from cognis.core.context import _adapt_memory_instructions

        text = "Use get_artifact_url for downloads and get_artifact for content."
        result = _adapt_memory_instructions(text)
        assert "memory_get_artifact_url" in result
        assert "memory_get_artifact" in result
        # get_artifact_url should not become "memory_get_artifact_url"
        # via partial match of get_artifact
        assert "memory_get_artifact_url" in result

    def test_nonexistent_tools_annotated(self) -> None:
        from cognis.core.context import _adapt_memory_instructions

        text = "Do NOT call initialize_memory or get_core_memories — already done"
        result = _adapt_memory_instructions(text)
        assert "initialize_memory (not available)" in result
        assert "get_core_memories (not available)" in result

    def test_all_tool_names_replaced(self) -> None:
        from cognis.core.context import _adapt_memory_instructions

        text = (
            "search_memories find_memories ask_memories add_memory add_memories "
            "update_memory delete_memory list_memories list_categories "
            "get_recent_memories save_artifact get_artifact get_artifact_url "
            "list_artifacts delete_artifact"
        )
        result = _adapt_memory_instructions(text)
        assert "memory_search" in result
        assert "memory_find" in result
        assert "memory_ask" in result
        assert "memory_add " in result  # space to distinguish from memory_add_batch
        assert "memory_add_batch" in result
        assert "memory_update" in result
        assert "memory_delete" in result
        assert "memory_list " in result  # space to distinguish from memory_list_artifacts
        assert "memory_categories" in result
        assert "memory_recent" in result
        assert "memory_save_artifact" in result
        assert "memory_get_artifact " in result  # space to distinguish from _url
        assert "memory_get_artifact_url" in result
        assert "memory_list_artifacts" in result
        assert "memory_delete_artifact" in result

    def test_passthrough_no_tool_names(self) -> None:
        from cognis.core.context import _adapt_memory_instructions

        text = "This text has no MCP tool names at all."
        result = _adapt_memory_instructions(text)
        assert result == text

    def test_word_boundary_prevents_partial_match(self) -> None:
        from cognis.core.context import _adapt_memory_instructions

        # "my_add_memory_function" should not be affected
        text = "my_add_memory_function is custom"
        result = _adapt_memory_instructions(text)
        # Word boundary \b matches between \w and \W, but _ is \w,
        # so add_memory inside my_add_memory_function won't match
        # because there's no boundary before 'add' (preceded by '_')
        # Actually \b is between \w and \W. Since _ is \w and the
        # preceding char is also \w, there's no boundary. Good.
        assert "my_add_memory_function" in result

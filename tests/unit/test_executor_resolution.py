"""Unit tests for executor resolution and tool enablement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cognis.core.executor_resolution import (
    filter_tools_by_executor,
    is_tool_enabled,
    labels_match,
    select_executor_for_agent,
)
from cognis.models.tool import ToolDefinition, ToolSource


def _tool(name: str, category: str = "general") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Test tool {name}",
        parameters={},
        source=ToolSource(type="executor"),
        category=category,
    )


@dataclass
class FakeExecutor:
    executor_id: str
    name: str = "test"
    executor_type: str = "in_process"
    labels: dict[str, Any] | None = None
    enabled_tools: list[str] | None = field(default_factory=list)
    enabled_tool_groups: list[str] | None = field(default_factory=list)
    status: str = "active"
    is_default: bool = False
    owner_email: str | None = None


class TestIsToolEnabled:
    """Test tool enablement resolution."""

    def test_wildcard_enables_all(self) -> None:
        tool = _tool("bash", "shell")
        assert is_tool_enabled(tool, ["*"], []) is True

    def test_explicit_name_enables(self) -> None:
        tool = _tool("read", "filesystem")
        assert is_tool_enabled(tool, ["read", "glob"], []) is True

    def test_group_enables(self) -> None:
        tool = _tool("read", "filesystem")
        assert is_tool_enabled(tool, [], ["filesystem"]) is True

    def test_not_enabled(self) -> None:
        tool = _tool("bash", "shell")
        assert is_tool_enabled(tool, ["read"], ["filesystem"]) is False

    def test_empty_lists(self) -> None:
        tool = _tool("bash", "shell")
        assert is_tool_enabled(tool, [], []) is False

    def test_none_lists(self) -> None:
        tool = _tool("bash", "shell")
        assert is_tool_enabled(tool, None, None) is False

    def test_name_and_group_both_work(self) -> None:
        tool = _tool("grep", "search")
        assert is_tool_enabled(tool, ["grep"], []) is True
        assert is_tool_enabled(tool, [], ["search"]) is True
        assert is_tool_enabled(tool, ["grep"], ["search"]) is True


class TestFilterToolsByExecutor:
    """Test filtering tool lists by executor config."""

    def test_filter_by_name(self) -> None:
        tools = [_tool("read", "filesystem"), _tool("bash", "shell"), _tool("glob", "search")]
        result = filter_tools_by_executor(tools, ["read", "glob"], [])
        assert len(result) == 2
        assert {t.name for t in result} == {"read", "glob"}

    def test_filter_by_group(self) -> None:
        tools = [_tool("read", "filesystem"), _tool("write", "filesystem"), _tool("bash", "shell")]
        result = filter_tools_by_executor(tools, [], ["filesystem"])
        assert len(result) == 2
        assert {t.name for t in result} == {"read", "write"}

    def test_wildcard_returns_all(self) -> None:
        tools = [_tool("read"), _tool("bash"), _tool("glob")]
        result = filter_tools_by_executor(tools, ["*"], [])
        assert len(result) == 3

    def test_empty_returns_none(self) -> None:
        tools = [_tool("read"), _tool("bash")]
        result = filter_tools_by_executor(tools, [], [])
        assert len(result) == 0


class TestLabelsMatch:
    """Test k8s-style label matching."""

    def test_empty_selector_matches_any(self) -> None:
        assert labels_match({"tier": "standard"}, {}) is True
        assert labels_match(None, {}) is True
        assert labels_match({}, {}) is True

    def test_exact_match(self) -> None:
        assert labels_match({"tier": "standard", "gpu": "true"}, {"tier": "standard"}) is True

    def test_all_must_match(self) -> None:
        assert labels_match({"tier": "standard"}, {"tier": "standard", "gpu": "true"}) is False

    def test_no_labels_fails_non_empty_selector(self) -> None:
        assert labels_match(None, {"tier": "standard"}) is False
        assert labels_match({}, {"tier": "standard"}) is False

    def test_value_mismatch(self) -> None:
        assert labels_match({"tier": "premium"}, {"tier": "standard"}) is False


class TestSelectExecutorForAgent:
    """Test executor selection based on agent config."""

    def test_explicit_executor_id(self) -> None:
        executors = [
            FakeExecutor(executor_id="exec_a", is_default=True),
            FakeExecutor(executor_id="exec_b"),
        ]
        result = select_executor_for_agent(executors, {"executor_id": "exec_b"})
        assert result is not None
        assert result.executor_id == "exec_b"

    def test_explicit_id_not_found(self) -> None:
        executors = [FakeExecutor(executor_id="exec_a", is_default=True)]
        result = select_executor_for_agent(executors, {"executor_id": "nonexistent"})
        assert result is None

    def test_label_selector(self) -> None:
        executors = [
            FakeExecutor(executor_id="exec_a", labels={"tier": "standard"}, is_default=True),
            FakeExecutor(executor_id="exec_b", labels={"tier": "premium", "gpu": "true"}),
        ]
        result = select_executor_for_agent(
            executors, {"executor_selector": {"tier": "premium", "gpu": "true"}}
        )
        assert result is not None
        assert result.executor_id == "exec_b"

    def test_label_selector_no_match(self) -> None:
        executors = [
            FakeExecutor(executor_id="exec_a", labels={"tier": "standard"}, is_default=True),
        ]
        result = select_executor_for_agent(executors, {"executor_selector": {"tier": "premium"}})
        assert result is None

    def test_default_executor(self) -> None:
        executors = [
            FakeExecutor(executor_id="exec_a"),
            FakeExecutor(executor_id="exec_b", is_default=True),
        ]
        result = select_executor_for_agent(executors, {})
        assert result is not None
        assert result.executor_id == "exec_b"

    def test_private_default_preferred_over_shared_default(self) -> None:
        executors = [
            FakeExecutor(executor_id="shared", is_default=True, owner_email=None),
            FakeExecutor(
                executor_id="private",
                is_default=True,
                owner_email="user@example.com",
            ),
        ]
        result = select_executor_for_agent(executors, {}, owner_email="user@example.com")
        assert result is not None
        assert result.executor_id == "private"

    def test_shared_default_used_when_no_private_default_exists(self) -> None:
        executors = [
            FakeExecutor(executor_id="shared", is_default=True, owner_email=None),
            FakeExecutor(executor_id="private", owner_email="user@example.com"),
        ]
        result = select_executor_for_agent(executors, {}, owner_email="user@example.com")
        assert result is not None
        assert result.executor_id == "shared"

    def test_no_config_uses_default(self) -> None:
        executors = [FakeExecutor(executor_id="exec_a", is_default=True)]
        result = select_executor_for_agent(executors, None)
        assert result is not None
        assert result.executor_id == "exec_a"

    def test_inactive_executor_skipped(self) -> None:
        executors = [
            FakeExecutor(executor_id="exec_a", is_default=True, status="inactive"),
            FakeExecutor(executor_id="exec_b", status="active"),
        ]
        result = select_executor_for_agent(executors, {})
        assert result is not None
        assert result.executor_id == "exec_b"

    def test_fallback_to_first_active(self) -> None:
        executors = [
            FakeExecutor(executor_id="exec_a", status="active"),
            FakeExecutor(executor_id="exec_b", status="active"),
        ]
        result = select_executor_for_agent(executors, {})
        assert result is not None
        assert result.executor_id == "exec_a"

    def test_empty_executors(self) -> None:
        result = select_executor_for_agent([], {})
        assert result is None


class TestBuildRegistryWithHandlers:
    """Test that the registry builder attaches handlers."""

    def test_registry_has_handlers(self) -> None:
        from cognis.api.runtime_support import build_registry_with_handlers

        tools = [_tool("read", "filesystem"), _tool("bash", "shell")]

        async def fake_handler(args: dict, ctx: Any) -> Any:
            pass

        handler_map = {"read": fake_handler, "bash": fake_handler}
        registry = build_registry_with_handlers(tools, handler_map)

        read_tool = registry.get("read")
        assert read_tool is not None
        assert read_tool.handler is not None

        bash_tool = registry.get("bash")
        assert bash_tool is not None
        assert bash_tool.handler is not None

    def test_registry_missing_handler_is_none(self) -> None:
        from cognis.api.runtime_support import build_registry_with_handlers

        tools = [_tool("read", "filesystem")]
        registry = build_registry_with_handlers(tools, {})

        read_tool = registry.get("read")
        assert read_tool is not None
        assert read_tool.handler is None

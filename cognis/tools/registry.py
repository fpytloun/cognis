"""Tool registry and runtime handler metadata."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from cognis.logging import get_logger
from cognis.models.tool import ExecutorHandle, ToolDefinition, ToolResult

logger = get_logger(__name__)

ToolOutputChunkCallback = Callable[[str, str | None], Coroutine[Any, Any, None]]

SOURCE_PRIORITIES: dict[str, int] = {
    "builtin": 500,
    "executor": 400,
    "skill": 300,
    "local_mcp": 200,
    "intaris_mcp": 100,
}


@dataclass(slots=True)
class ToolExecutionContext:
    """Execution-time context passed to tool handlers."""

    executor_handle: ExecutorHandle
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    shared_runtime_metadata: dict[str, Any] | None = None
    execution_scope_id: str | None = None
    output_chunk_callback: ToolOutputChunkCallback | None = None


type ToolHandlerResult = str | dict[str, Any] | list[Any] | ToolResult


class ToolHandler(Protocol):
    """Async tool handler interface used by the in-process executor."""

    async def __call__(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolHandlerResult: ...


@dataclass(slots=True)
class RegisteredTool:
    """Tool definition plus optional runtime handler."""

    definition: ToolDefinition
    handler: ToolHandler | None = None


class ToolRegistry:
    """Registry for tool definitions and optional runtime handlers."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        """Register a tool by its fully-qualified name."""

        if tool.definition.name in self._tools:
            raise ValueError(f"Duplicate tool registration: {tool.definition.name}")
        self._tools[tool.definition.name] = tool

    def register_many(self, tools: list[RegisteredTool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, tool_name: str) -> RegisteredTool | None:
        return self._tools.get(tool_name)

    def items(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def list_tools(self) -> list[ToolDefinition]:
        return [item.definition for item in self._tools.values()]

    def export(self) -> list[dict[str, Any]]:
        return [item.definition.model_dump(mode="json") for item in self._tools.values()]

    def merge(self, *others: ToolRegistry) -> ToolRegistry:
        """Merge registries by fully-qualified tool name.

        The dedup key is ``tool.definition.name``. Duplicates from the same
        source are treated as configuration errors. Cross-source duplicates are
        resolved by ``SOURCE_PRIORITIES``.
        """

        merged = ToolRegistry()
        for registry in (self, *others):
            for tool in registry.items():
                current = merged.get(tool.definition.name)
                if current is None:
                    merged.register(tool)
                    continue
                current_source = current.definition.source.type
                next_source = tool.definition.source.type
                if current_source == next_source:
                    raise ValueError(
                        f"Duplicate tool name from the same source: {tool.definition.name}"
                    )
                if _source_priority(next_source) > _source_priority(current_source):
                    logger.info(
                        "Tool registration shadowed by higher priority source",
                        extra={
                            "extra_data": {
                                "tool_name": tool.definition.name,
                                "preferred_source": next_source,
                            }
                        },
                    )
                    merged._tools[tool.definition.name] = tool
                    continue
                logger.info(
                    "Tool registration shadowed by existing source",
                    extra={
                        "extra_data": {
                            "tool_name": tool.definition.name,
                            "preferred_source": current_source,
                        }
                    },
                )
        return merged


def _source_priority(source_type: str) -> int:
    return SOURCE_PRIORITIES.get(source_type, 0)

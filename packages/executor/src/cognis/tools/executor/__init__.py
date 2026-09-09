"""Executor-native tool definitions and handlers.

These tools execute directly in the executor process without MCP overhead.
They are available to all agents by default (opt-out model).
"""

from __future__ import annotations

from pkgutil import extend_path
from typing import Any

__path__ = extend_path(__path__, __name__)

from cognis.tools.executor.definitions import executor_tool_definitions


def executor_tool_handlers() -> dict[str, Any]:
    """Load runtime handlers without creating an executor package import cycle."""
    from cognis.executor.tool_definitions_runtime import executor_tool_handlers as load_handlers

    return load_handlers()


__all__ = ["executor_tool_definitions", "executor_tool_handlers"]

"""Executor-native tool definitions and handlers.

These tools execute directly in the executor process without MCP overhead.
They are available to all agents by default (opt-out model).
"""

from __future__ import annotations

from cognis.tools.executor.definitions import executor_tool_definitions, executor_tool_handlers

__all__ = ["executor_tool_definitions", "executor_tool_handlers"]

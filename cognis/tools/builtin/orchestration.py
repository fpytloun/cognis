"""Built-in orchestration tool definitions and stage-4 stubs."""

from __future__ import annotations

import json

from cognis.models.tool import ToolCall, ToolDefinition, ToolResult, ToolSource

ORCHESTRATION_TOOL_NAMES = {"delegate", "spawn_worker", "fork"}

DELEGATE_TOOL = ToolDefinition(
    name="delegate",
    description="Delegate a task to a specialized agent.",
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID or 'auto'."},
            "task": {"type": "string", "description": "Task description."},
            "context": {"type": "string", "description": "Background context."},
            "expected_output": {"type": "string", "description": "Expected result."},
        },
        "required": ["task"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

SPAWN_WORKER_TOOL = ToolDefinition(
    name="spawn_worker",
    description="Spawn a lightweight worker for focused tasks.",
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "worker_type": {
                "type": "string",
                "enum": ["research", "summarize", "code", "general"],
            },
        },
        "required": ["task"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

FORK_TOOL = ToolDefinition(
    name="fork",
    description="Fork into an isolated child session for exploration.",
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "context_summary": {"type": "string"},
        },
        "required": ["reason"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)


def orchestration_tools() -> list[ToolDefinition]:
    """Return all built-in orchestration tool definitions."""

    return [DELEGATE_TOOL, SPAWN_WORKER_TOOL, FORK_TOOL]


def is_orchestration_tool(tool_name: str) -> bool:
    """Return True when the tool is handled as a controller directive."""

    return tool_name in ORCHESTRATION_TOOL_NAMES


async def handle_orchestration_tool_call(tool_call: ToolCall) -> ToolResult:
    """Return a stage-4 orchestration stub response."""

    return ToolResult(
        output=json.dumps(
            {
                "status": "accepted",
                "mode": tool_call.name,
                "call_id": tool_call.call_id,
                "message": "Orchestration flow will be wired in Stage 6.",
            },
            sort_keys=True,
        ),
        metadata={"orchestration": True, "mode": tool_call.name},
    )

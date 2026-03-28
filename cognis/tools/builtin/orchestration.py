"""Built-in orchestration tool definitions.

These tools are intercepted by the agent loop as controller directives.
They create child sessions for delegation. The executor never sees them.
"""

from __future__ import annotations

import json
from typing import Any

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


async def handle_orchestration_tool_call(
    tool_call: ToolCall,
    *,
    session_manager: Any | None = None,
    session: Any | None = None,
    agent: Any | None = None,
) -> ToolResult:
    """Handle orchestration tool calls as controller directives.

    Creates child sessions for delegation when session_manager is provided.
    Falls back to an accepted-response stub when the session layer is not
    available (e.g., direct invocation outside the agent loop).
    """
    mode = tool_call.name
    args = tool_call.arguments

    if session_manager is not None and session is not None and agent is not None:
        # Real delegation — create a child session
        try:
            child_session = await session_manager.create_child_session(
                parent_session=session,
                mode=mode,
                task_description=args.get("task") or args.get("reason", ""),
                agent_id=args.get("agent_id") or getattr(agent, "agent_id", ""),
                effective_agent_id=args.get("agent_id") or getattr(agent, "agent_id", ""),
                expected_output=args.get("expected_output"),
            )
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "accepted",
                        "mode": mode,
                        "call_id": tool_call.call_id,
                        "session_id": child_session.session_id,
                        "message": f"Delegation ({mode}) created. Working in background.",
                    },
                    sort_keys=True,
                ),
                metadata={"orchestration": True, "mode": mode},
            )
        except Exception as exc:
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "error",
                        "mode": mode,
                        "call_id": tool_call.call_id,
                        "message": f"Delegation failed: {type(exc).__name__}",
                    },
                    sort_keys=True,
                ),
                is_error=True,
                metadata={"orchestration": True, "mode": mode},
            )

    # Stub response when session layer is not available
    return ToolResult(
        output=json.dumps(
            {
                "status": "accepted",
                "mode": mode,
                "call_id": tool_call.call_id,
                "message": "Delegation request accepted.",
            },
            sort_keys=True,
        ),
        metadata={"orchestration": True, "mode": mode},
    )

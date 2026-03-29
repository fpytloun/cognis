"""Built-in orchestration tool definitions.

These tools are intercepted by the agent loop as controller directives.
They create child sessions for delegation. The executor never sees them.
"""

from __future__ import annotations

import json
from typing import Any

from cognis.models.session import SessionModel
from cognis.models.tool import ToolCall, ToolDefinition, ToolResult, ToolSource

ORCHESTRATION_TOOL_NAMES = {"delegate", "fork"}

DELEGATE_TOOL = ToolDefinition(
    name="delegate",
    description=(
        "Delegate a task to a background sub-session. The sub-session runs "
        "independently while the main chat stays responsive. Use for research, "
        "summarization, code generation, or any work that benefits from focused "
        "execution. Optionally specify a different agent_id for specialist "
        "delegation; omit it to use the current agent. The parent turn ends "
        "immediately; a follow-up turn is triggered when the sub-session completes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Clear description of what the sub-session should do.",
            },
            "agent_id": {
                "type": "string",
                "description": (
                    "Optional agent ID for specialist delegation. Omit to use "
                    "the current agent (same persona and tools)."
                ),
            },
            "context": {
                "type": "string",
                "description": "Background context the sub-session needs.",
            },
            "expected_output": {
                "type": "string",
                "description": "What the result should look like.",
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
    description=(
        "Fork into an isolated child session for parallel exploration. Uses the "
        "same agent and inherits the current context. Use when you want to "
        "explore an approach without affecting the main session. The parent "
        "turn ends immediately; a follow-up turn is triggered when the fork "
        "completes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why you are forking — what you want to explore.",
            },
            "context_summary": {
                "type": "string",
                "description": "Summary of relevant context to carry into the fork.",
            },
        },
        "required": ["reason"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)


def orchestration_tools() -> list[ToolDefinition]:
    """Return all built-in orchestration tool definitions."""

    return [DELEGATE_TOOL, FORK_TOOL]


def is_orchestration_tool(tool_name: str) -> bool:
    """Return True when the tool is handled as a controller directive."""

    return tool_name in ORCHESTRATION_TOOL_NAMES


async def handle_orchestration_tool_call(
    tool_call: ToolCall,
    *,
    session_manager: Any | None = None,
    session: Any | None = None,
    agent: Any | None = None,
) -> tuple[ToolResult, SessionModel | None]:
    """Handle orchestration tool calls as controller directives.

    Creates child sessions for delegation when session_manager is provided.
    Falls back to an accepted-response stub when the session layer is not
    available (e.g., direct invocation outside the agent loop).

    Returns a ``(ToolResult, child_session)`` tuple.  *child_session* is
    ``None`` when the session layer is unavailable or creation failed.
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
            return (
                ToolResult(
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
                ),
                child_session,
            )
        except Exception as exc:
            return (
                ToolResult(
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
                ),
                None,
            )

    # Stub response when session layer is not available
    return (
        ToolResult(
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
        ),
        None,
    )

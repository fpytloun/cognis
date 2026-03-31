"""Built-in orchestration and task tool definitions.

These tools are intercepted by the agent loop as controller directives.
They manage sub-sessions (delegate) and tasks (create/list/update/cancel).
The executor never sees them.

Tool taxonomy:
  Sub-session tools: delegate, list_subsessions, get_subsession, cancel_subsession
  Task tools: create_task, list_tasks, get_task, update_task, cancel_task
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from cognis.models.session import SessionModel
from cognis.models.tool import ToolCall, ToolDefinition, ToolResult, ToolSource


class OrchestrationMode(StrEnum):
    """Controls which orchestration tools are available in a given context.

    FULL: Main interactive session — all tools available.
    DELEGATE_SYNC_ONLY: Task step — only sync delegate allowed.
    NONE: Sub-session — no orchestration tools.
    """

    FULL = "full"
    DELEGATE_SYNC_ONLY = "delegate_sync_only"
    NONE = "none"


# All orchestration tool names (for interception in agent loop)
SUBSESSION_TOOL_NAMES = {"delegate", "list_subsessions", "get_subsession", "cancel_subsession"}
TASK_TOOL_NAMES = {"create_task", "list_tasks", "get_task", "update_task", "cancel_task"}
ORCHESTRATION_TOOL_NAMES = SUBSESSION_TOOL_NAMES | TASK_TOOL_NAMES

# ---------------------------------------------------------------------------
# Sub-session tool definitions
# ---------------------------------------------------------------------------

DELEGATE_TOOL = ToolDefinition(
    name="delegate",
    description=(
        "Delegate work to a sub-session. The sub-session runs a focused task "
        "and returns a result. By default (wait=false) the sub-session runs in "
        "the background while the main chat stays responsive — you will receive "
        "a follow-up with the result. Set wait=true to block until the sub-session "
        "completes and receive its output directly as the tool result. Use wait=true "
        "when you need results from one or more parallel sub-sessions before "
        "continuing (e.g. joining parallel research). Use wait=false (default) for "
        "anything that may take more than a few seconds — prefer async behavior "
        "to keep the chat responsive. Optionally specify agent_id for specialist "
        "delegation."
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
                    "Optional agent ID for specialist delegation. Omit to use the current agent."
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
            "wait": {
                "type": "boolean",
                "description": (
                    "When true, blocks until the sub-session completes and returns "
                    "its output directly. When false (default), the sub-session runs "
                    "in the background. Prefer false for anything longer than a few "
                    "seconds."
                ),
                "default": False,
            },
        },
        "required": ["task"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

LIST_SUBSESSIONS_TOOL = ToolDefinition(
    name="list_subsessions",
    description=(
        "List child sub-sessions of the current session. Shows status, agent, "
        "task description, and result summary for each. Use to check on "
        "background delegations."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "completed", "failed", "all"],
                "description": "Filter by status. Default: all.",
                "default": "all",
            },
        },
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

GET_SUBSESSION_TOOL = ToolDefinition(
    name="get_subsession",
    description=(
        "Get detailed status and output of a child sub-session. Returns the "
        "sub-session's current status, task description, result summary, and "
        "any output produced so far."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "ID of the child sub-session to inspect.",
            },
        },
        "required": ["session_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

CANCEL_SUBSESSION_TOOL = ToolDefinition(
    name="cancel_subsession",
    description=(
        "Cancel a running child sub-session. The sub-session will be interrupted "
        "and marked as failed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "ID of the child sub-session to cancel.",
            },
        },
        "required": ["session_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

# ---------------------------------------------------------------------------
# Task tool definitions
# ---------------------------------------------------------------------------

CREATE_TASK_TOOL = ToolDefinition(
    name="create_task",
    description=(
        "Create an autonomous task. Tasks are independent work units that run "
        "in the background through the workflow engine with multiple steps, "
        "evaluation, and review. Use for substantial work: implementing features, "
        "deep research, multi-step analysis. The task runs independently — you "
        "define it and the result is delivered to the conversation when complete. "
        "Tasks can spawn their own sub-sessions internally."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (shown in task board).",
            },
            "description": {
                "type": "string",
                "description": "Detailed description of what the task should accomplish.",
            },
            "agent_id": {
                "type": "string",
                "description": "Optional agent ID. Omit to use the current agent.",
            },
            "workflow_id": {
                "type": "string",
                "description": (
                    "Optional workflow template ID. If omitted, the system "
                    "auto-selects based on the task description."
                ),
            },
            "priority": {
                "type": "integer",
                "description": "Priority (higher = more urgent). Default: 0.",
                "default": 0,
            },
            "expected_output": {
                "type": "string",
                "description": "What the final result should look like.",
            },
        },
        "required": ["title", "description"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

LIST_TASKS_TOOL = ToolDefinition(
    name="list_tasks",
    description=(
        "List tasks for the current agent. Shows task ID, title, status, "
        "priority, and workflow progress."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "draft",
                    "queued",
                    "ready",
                    "running",
                    "paused",
                    "completed",
                    "failed",
                    "cancelled",
                    "all",
                ],
                "description": "Filter by status. Default: all.",
                "default": "all",
            },
        },
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

GET_TASK_TOOL = ToolDefinition(
    name="get_task",
    description=(
        "Get detailed status of a task including current workflow step, progress, and result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the task to inspect.",
            },
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

UPDATE_TASK_TOOL = ToolDefinition(
    name="update_task",
    description=(
        "Update a task that has not yet started executing (draft or queued). "
        "Can modify title, description, priority, or workflow."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the task to update.",
            },
            "title": {
                "type": "string",
                "description": "New title.",
            },
            "description": {
                "type": "string",
                "description": "New description.",
            },
            "priority": {
                "type": "integer",
                "description": "New priority.",
            },
            "workflow_id": {
                "type": "string",
                "description": "New workflow template ID.",
            },
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

CANCEL_TASK_TOOL = ToolDefinition(
    name="cancel_task",
    description="Cancel a task. Running tasks will be stopped; queued tasks will be removed.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the task to cancel.",
            },
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)


GET_TASK_OUTPUT_TOOL = ToolDefinition(
    name="get_task_output",
    description=(
        "Get the full output of a completed task. Returns the content "
        "produced by the final workflow step including full text, claims, "
        "and structured outputs. Use get_task first to check task status."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the task.",
            },
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

GET_TASK_STEP_OUTPUT_TOOL = ToolDefinition(
    name="get_task_step_output",
    description=(
        "Get the full output of a specific workflow step. Use get_task "
        "first to see available step names and their statuses."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the task.",
            },
            "step_name": {
                "type": "string",
                "description": "Name of the step (e.g., 'plan', 'research', 'synthesize').",
            },
        },
        "required": ["task_id", "step_name"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

RETRY_TASK_TOOL = ToolDefinition(
    name="retry_task",
    description=(
        "Retry a failed or paused task. Re-runs the current workflow step "
        "that failed or is waiting for input. The attempt counter is reset."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the task to retry.",
            },
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)


# ---------------------------------------------------------------------------
# Tool collections by orchestration mode
# ---------------------------------------------------------------------------

_ALL_SUBSESSION_TOOLS = [
    DELEGATE_TOOL,
    LIST_SUBSESSIONS_TOOL,
    GET_SUBSESSION_TOOL,
    CANCEL_SUBSESSION_TOOL,
]

_ALL_TASK_TOOLS = [
    CREATE_TASK_TOOL,
    LIST_TASKS_TOOL,
    GET_TASK_TOOL,
    GET_TASK_OUTPUT_TOOL,
    GET_TASK_STEP_OUTPUT_TOOL,
    UPDATE_TASK_TOOL,
    CANCEL_TASK_TOOL,
    RETRY_TASK_TOOL,
]

# Sync-only delegate for task steps (no wait parameter exposed — always sync)
_DELEGATE_SYNC_TOOL = DELEGATE_TOOL.model_copy(
    update={
        "name": "delegate",
        "description": (
            "Delegate work to a sub-session that runs synchronously. The sub-session "
            "executes the task and returns its output directly as the tool result. "
            "Use for focused sub-tasks within a workflow step. Multiple delegates "
            "can run in parallel via parallel tool calls. Optionally specify "
            "agent_id for specialist delegation."
        ),
        "parameters": {
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
                        "the current agent."
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
    }
)


def orchestration_tools(mode: OrchestrationMode = OrchestrationMode.FULL) -> list[ToolDefinition]:
    """Return orchestration tool definitions for the given mode.

    FULL: All sub-session + task tools (main interactive session).
    DELEGATE_SYNC_ONLY: Only sync delegate (task workflow steps).
    NONE: No orchestration tools (sub-sessions).
    """
    if mode == OrchestrationMode.NONE:
        return []
    if mode == OrchestrationMode.DELEGATE_SYNC_ONLY:
        return [_DELEGATE_SYNC_TOOL]
    # FULL mode
    return _ALL_SUBSESSION_TOOLS + _ALL_TASK_TOOLS


def is_orchestration_tool(tool_name: str) -> bool:
    """Return True when the tool is handled as a controller directive."""
    return tool_name in ORCHESTRATION_TOOL_NAMES


def is_subsession_tool(tool_name: str) -> bool:
    """Return True for sub-session management tools."""
    return tool_name in SUBSESSION_TOOL_NAMES


def is_task_tool(tool_name: str) -> bool:
    """Return True for task management tools."""
    return tool_name in TASK_TOOL_NAMES


# ---------------------------------------------------------------------------
# Delegate handler (creates child session)
# ---------------------------------------------------------------------------


async def handle_delegate_tool_call(
    tool_call: ToolCall,
    *,
    session_manager: Any | None = None,
    session: Any | None = None,
    agent: Any | None = None,
) -> tuple[ToolResult, SessionModel | None]:
    """Handle the delegate tool call — creates a child session.

    Returns a ``(ToolResult, child_session)`` tuple.  *child_session* is
    ``None`` when the session layer is unavailable or creation failed.
    """
    args = tool_call.arguments

    if session_manager is not None and session is not None and agent is not None:
        try:
            child_session = await session_manager.create_child_session(
                parent_session=session,
                mode="delegate",
                task_description=args.get("task", ""),
                agent_id=args.get("agent_id") or getattr(agent, "agent_id", ""),
                effective_agent_id=args.get("agent_id") or getattr(agent, "agent_id", ""),
                expected_output=args.get("expected_output"),
            )
            wait = args.get("wait", False)
            return (
                ToolResult(
                    output=json.dumps(
                        {
                            "status": "accepted",
                            "mode": "delegate",
                            "wait": wait,
                            "call_id": tool_call.call_id,
                            "session_id": child_session.session_id,
                            "message": (
                                "Delegation created. Waiting for completion."
                                if wait
                                else "Delegation created. Working in background."
                            ),
                        },
                        sort_keys=True,
                    ),
                    metadata={"orchestration": True, "mode": "delegate", "wait": wait},
                ),
                child_session,
            )
        except Exception as exc:
            return (
                ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "mode": "delegate",
                            "call_id": tool_call.call_id,
                            "message": f"Delegation failed: {type(exc).__name__}",
                        },
                        sort_keys=True,
                    ),
                    is_error=True,
                    metadata={"orchestration": True, "mode": "delegate"},
                ),
                None,
            )

    # Stub response when session layer is not available
    return (
        ToolResult(
            output=json.dumps(
                {
                    "status": "accepted",
                    "mode": "delegate",
                    "call_id": tool_call.call_id,
                    "message": "Delegation request accepted.",
                },
                sort_keys=True,
            ),
            metadata={"orchestration": True, "mode": "delegate"},
        ),
        None,
    )

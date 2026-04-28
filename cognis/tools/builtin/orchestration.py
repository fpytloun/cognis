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
TASK_TOOL_NAMES = {
    "create_task",
    "list_tasks",
    "get_task",
    "get_task_output",
    "get_task_step_output",
    "get_task_step_logs",
    "respond_task_input",
    "update_task",
    "cancel_task",
    "retry_task",
    "resolve_task_pause",
}
WORKFLOW_TOOL_NAMES = {
    "list_workflows",
    "get_workflow",
    "create_workflow",
    "update_workflow",
    "delete_workflow",
    "duplicate_workflow",
}
COMPOSITION_TOOL_NAMES = {"compose_and_run_workflow"}
ORCHESTRATION_TOOL_NAMES = (
    SUBSESSION_TOOL_NAMES | TASK_TOOL_NAMES | WORKFLOW_TOOL_NAMES | COMPOSITION_TOOL_NAMES
)

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
        "Tasks can spawn their own sub-sessions internally. Do not assign a project "
        "unless an exact project name, source path prefix, or remote URL match exists."
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
            "project_id": {
                "type": "string",
                "description": "Optional project ID. Only use when there is an exact project match.",
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
            "project_id": {
                "type": "string",
                "description": "Optional project ID filter.",
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

RESPOND_TASK_INPUT_TOOL = ToolDefinition(
    name="respond_task_input",
    description=(
        "Answer a paused task step question or step input request. Use get_task first to inspect "
        "the pending question and available context, then provide the response so the task can resume."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the paused task.",
            },
            "response": {
                "type": "string",
                "description": "Answer to the pending step question.",
            },
        },
        "required": ["task_id", "response"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
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
            "project_id": {
                "type": "string",
                "description": "Optional project ID. Only use when there is an exact project match.",
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
        "Get the final output of a completed task. Returns a compact anchored summary "
        "of the latest approved delivering step, including content, claims, and structured "
        "outputs. Use list_tool_output_anchors or read_tool_output_anchor on this tool call "
        "for deeper sections when needed."
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
        "Get the output of a specific workflow step attempt. Returns a compact anchored "
        "summary with the step output, evaluation, and todos. Use get_task first to inspect "
        "available steps and attempts, then use list_tool_output_anchors or read_tool_output_anchor "
        "on this tool call for deeper sections."
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
            "attempt": {
                "type": "integer",
                "description": "Optional attempt number. Omit to inspect the latest attempt.",
            },
        },
        "required": ["task_id", "step_name"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

GET_TASK_STEP_LOGS_TOOL = ToolDefinition(
    name="get_task_step_logs",
    description=(
        "Inspect the recorded execution log for a specific workflow step attempt. Returns a compact "
        "anchored event timeline including assistant messages, reasoning, tool calls, and tool results. "
        "Use read_tool_output_anchor on this tool call to drill into specific events, and use any call_id "
        "you find there with read_tool_output or search_tool_output to inspect full tool output."
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
                "description": "Name of the step to inspect.",
            },
            "attempt": {
                "type": "integer",
                "description": "Optional attempt number. Omit to inspect the latest attempt.",
            },
            "after_seq": {
                "type": "integer",
                "description": "Optional session event cursor. Use 0 for the start of the step session.",
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum session events to inspect. Default: 50.",
                "default": 50,
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

RESOLVE_TASK_PAUSE_TOOL = ToolDefinition(
    name="resolve_task_pause",
    description=(
        "Resolve a paused workflow task gate. Use this when a task is paused and a human "
        "wants to retry the blocked step, continue anyway, or cancel the task. An optional "
        "note is passed into the next step as a one-shot operator instruction for retry or "
        "continue actions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the paused task.",
            },
            "action": {
                "type": "string",
                "description": (
                    "How to resolve the paused workflow gate. Use one of the currently offered "
                    "pause actions, or use 'retry' as a convenience alias for the gate's "
                    "revise(...) action when available."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional human instruction to carry into the next step when retrying or "
                    "continuing."
                ),
            },
        },
        "required": ["task_id", "action"],
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
    GET_TASK_STEP_LOGS_TOOL,
    RESPOND_TASK_INPUT_TOOL,
    UPDATE_TASK_TOOL,
    CANCEL_TASK_TOOL,
    RETRY_TASK_TOOL,
    RESOLVE_TASK_PAUSE_TOOL,
]

LIST_WORKFLOWS_TOOL = ToolDefinition(
    name="list_workflows",
    description=(
        "List workflows visible to the current user. Returns summary metadata for both system "
        "and user-owned workflows."
    ),
    parameters={"type": "object", "properties": {}},
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

GET_WORKFLOW_TOOL = ToolDefinition(
    name="get_workflow",
    description="Get the full definition and metadata for one workflow.",
    parameters={
        "type": "object",
        "properties": {"workflow_id": {"type": "string", "description": "Workflow ID to inspect."}},
        "required": ["workflow_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

CREATE_WORKFLOW_TOOL = ToolDefinition(
    name="create_workflow",
    description="Create a new user-owned workflow definition.",
    parameters={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "description": "Optional workflow ID override."},
            "name": {"type": "string", "description": "Workflow name."},
            "description": {"type": "string", "description": "Workflow description."},
            "version": {"type": "integer", "description": "Workflow version number."},
            "criteria": {"type": "string", "description": "Workflow selection criteria."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "interaction": {"type": "object", "description": "Workflow interaction config."},
            "defaults": {"type": "object", "description": "Default workflow completion config."},
            "steps": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Workflow step definitions. Run steps should usually include step_profile_id, "
                    "may set step_profile_mode, and may include inline step_profile with tool_overrides "
                    "and allow_tool_search."
                ),
            },
        },
        "required": ["name", "steps"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

UPDATE_WORKFLOW_TOOL = ToolDefinition(
    name="update_workflow",
    description="Update a user-owned workflow definition. Active workflow references are protected.",
    parameters={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "description": "Workflow ID to update."},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "version": {"type": "integer"},
            "criteria": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "interaction": {"type": "object"},
            "defaults": {"type": "object"},
            "steps": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Workflow step definitions. Run steps should usually include step_profile_id, "
                    "may set step_profile_mode, and may include inline step_profile with tool_overrides "
                    "and allow_tool_search."
                ),
            },
        },
        "required": ["workflow_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

DELETE_WORKFLOW_TOOL = ToolDefinition(
    name="delete_workflow",
    description="Delete a user-owned workflow. Active workflow references are protected.",
    parameters={
        "type": "object",
        "properties": {"workflow_id": {"type": "string", "description": "Workflow ID to delete."}},
        "required": ["workflow_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

DUPLICATE_WORKFLOW_TOOL = ToolDefinition(
    name="duplicate_workflow",
    description="Duplicate a visible workflow into a new user-owned workflow.",
    parameters={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "description": "Workflow ID to duplicate."}
        },
        "required": ["workflow_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

_ALL_WORKFLOW_TOOLS = [
    LIST_WORKFLOWS_TOOL,
    GET_WORKFLOW_TOOL,
    CREATE_WORKFLOW_TOOL,
    UPDATE_WORKFLOW_TOOL,
    DELETE_WORKFLOW_TOOL,
    DUPLICATE_WORKFLOW_TOOL,
]

COMPOSE_AND_RUN_WORKFLOW_TOOL = ToolDefinition(
    name="compose_and_run_workflow",
    description=(
        "Compose a proportional workflow from the current request, optionally reusing or "
        "adapting an existing workflow, and immediately create a task or schedule from it. "
        "Use this when the work is multi-step, recurring, deliverable-sensitive, or should "
        "be scheduled instead of handled inline."
    ),
    parameters={
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "Distilled statement of the user's goal."},
            "context": {
                "type": "string",
                "description": "Relevant conversation and memory context.",
            },
            "title": {"type": "string", "description": "Optional task or schedule title override."},
            "expected_output": {"type": "string", "description": "Expected final result shape."},
            "skill_hints": {"type": "array", "items": {"type": "string"}},
            "template_hints": {"type": "array", "items": {"type": "string"}},
            "base_workflow_id": {
                "type": "string",
                "description": "Explicit base workflow to reuse or adapt.",
            },
            "decompose_skills": {
                "type": "string",
                "enum": ["auto", "always", "never"],
                "default": "auto",
            },
            "schedule": {
                "type": "object",
                "description": "Optional schedule definition. When present, the composed workflow becomes persistent.",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "schedule_type": {"type": "string", "enum": ["cron", "interval", "one_shot"]},
                    "cron_expr": {"type": "string"},
                    "interval_seconds": {"type": "integer"},
                    "one_shot_at": {"type": "string"},
                    "timezone": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "max_concurrent_runs": {"type": "integer"},
                    "delete_after_run": {"type": "boolean"},
                },
                "required": ["schedule_type"],
            },
            "delivery": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string"},
                    "target": {"type": "string"},
                    "completion_mode_family": {"type": "string", "enum": ["default", "direct"]},
                    "allow_silent_completion": {"type": "boolean"},
                },
            },
            "persist": {"type": "boolean", "default": False},
            "agent_id": {
                "type": "string",
                "description": "Optional agent override. Omit to use the current agent.",
            },
            "priority": {"type": "integer", "default": 0},
        },
        "required": ["intent"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

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
    return (
        _ALL_SUBSESSION_TOOLS
        + _ALL_TASK_TOOLS
        + _ALL_WORKFLOW_TOOLS
        + [COMPOSE_AND_RUN_WORKFLOW_TOOL]
    )


def is_orchestration_tool(tool_name: str) -> bool:
    """Return True when the tool is handled as a controller directive."""
    return tool_name in ORCHESTRATION_TOOL_NAMES


def is_subsession_tool(tool_name: str) -> bool:
    """Return True for sub-session management tools."""
    return tool_name in SUBSESSION_TOOL_NAMES


def is_task_tool(tool_name: str) -> bool:
    """Return True for task management tools."""
    return tool_name in TASK_TOOL_NAMES


def is_workflow_tool(tool_name: str) -> bool:
    """Return True for workflow management tools."""
    return tool_name in WORKFLOW_TOOL_NAMES


def is_composition_tool(tool_name: str) -> bool:
    """Return True for workflow composition tools."""

    return tool_name in COMPOSITION_TOOL_NAMES


# ---------------------------------------------------------------------------
# Delegate handler (creates child session)
# ---------------------------------------------------------------------------


async def handle_delegate_tool_call(
    tool_call: ToolCall,
    *,
    session_manager: Any | None = None,
    session: Any | None = None,
    agent: Any | None = None,
    agent_registry: Any | None = None,
    wait: bool | None = None,
) -> tuple[ToolResult, SessionModel | None]:
    """Handle the delegate tool call — creates a child session.

    Returns a ``(ToolResult, child_session)`` tuple.  *child_session* is
    ``None`` when the session layer is unavailable or creation failed.
    """
    args = tool_call.arguments

    if session_manager is not None and session is not None and agent is not None:
        # Validate secondary agent binding if delegating to a different agent
        target_agent_id = args.get("agent_id") or getattr(agent, "agent_id", "")
        if target_agent_id and target_agent_id != getattr(agent, "agent_id", "") and agent_registry:
            target_agent = await agent_registry.get(
                target_agent_id, owner_email=getattr(session, "user_email", None)
            )
            if target_agent is None:
                return (
                    ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "mode": "delegate",
                                "call_id": tool_call.call_id,
                                "message": f"Agent '{target_agent_id}' not found.",
                            },
                            sort_keys=True,
                        ),
                        is_error=True,
                        metadata={"orchestration": True, "mode": "delegate"},
                    ),
                    None,
                )
            if target_agent.agent_type == "secondary":
                is_bound = await agent_registry.is_secondary_bound(
                    getattr(agent, "agent_id", ""), target_agent_id
                )
                if not is_bound:
                    return (
                        ToolResult(
                            output=json.dumps(
                                {
                                    "status": "error",
                                    "mode": "delegate",
                                    "call_id": tool_call.call_id,
                                    "message": (
                                        f"Secondary agent '{target_agent_id}' is not bound to "
                                        f"agent '{getattr(agent, 'agent_id', '')}'. "
                                        f"Add it to the agent's secondary bindings."
                                    ),
                                },
                                sort_keys=True,
                            ),
                            is_error=True,
                            metadata={"orchestration": True, "mode": "delegate"},
                        ),
                        None,
                    )

        try:
            effective_wait = bool(args.get("wait", False) if wait is None else wait)
            child_session = await session_manager.create_child_session(
                parent_session=session,
                mode="delegate_sync" if effective_wait else "delegate_async",
                task_description=args.get("task", ""),
                agent_id=args.get("agent_id") or getattr(agent, "agent_id", ""),
                effective_agent_id=args.get("agent_id") or getattr(agent, "agent_id", ""),
                expected_output=args.get("expected_output"),
            )
            return (
                ToolResult(
                    output=json.dumps(
                        {
                            "status": "accepted",
                            "mode": "delegate",
                            "wait": effective_wait,
                            "call_id": tool_call.call_id,
                            "session_id": child_session.session_id,
                            "message": (
                                "Delegation created. Waiting for completion."
                                if effective_wait
                                else "Delegation created. Working in background."
                            ),
                        },
                        sort_keys=True,
                    ),
                    metadata={
                        "orchestration": True,
                        "mode": "delegate",
                        "wait": effective_wait,
                    },
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

"""Built-in orchestration and task tool definitions.

These tools are intercepted by the agent loop as controller directives.
They manage sub-sessions (delegate) and tasks (create/list/update/cancel).
The executor never sees them.

Tool taxonomy:
  Sub-session tools: delegate, retry/follow-up/fork, list/get/cancel
  Agent work tools: agent_conversation_*
  Task tools: create_task, list_tasks, get_task, update_task, cancel_task
"""

from __future__ import annotations

import copy
import json
import logging
from enum import StrEnum
from typing import Any

from cognis.core.agent_profiles import (
    normalize_agent_profile_id,
    resolve_agent_profile,
)
from cognis.core.orchestration_targets import (
    OrchestrationTargetError,
    OrchestrationTargetMode,
    OrchestrationTargetService,
    OrchestrationTargetSnapshot,
)
from cognis.models.session import SessionModel
from cognis.models.tool import (
    NativeToolDefinition as ToolDefinition,
)
from cognis.models.tool import (
    ToolCall,
    ToolDynamicOption,
    ToolResult,
    ToolSource,
    tool_input_schema,
    tool_with_input_schema,
)

logger = logging.getLogger(__name__)


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
SUBSESSION_TOOL_NAMES = {
    "delegate",
    "retry_subsession",
    "follow_up_subsession",
    "fork_subsession",
    "list_subsessions",
    "get_subsession",
    "cancel_subsession",
}
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
MANAGED_CONVERSATION_TOOL_NAMES = {
    "agent_conversation_create",
    "agent_conversation_send",
    "agent_conversation_set_profile",
    "agent_conversation_wait",
    "agent_conversation_interrupt",
    "agent_conversation_retry",
    "agent_conversation_fork",
    "agent_conversation_close",
    "agent_conversation_list",
    "agent_conversation_get",
}
ORCHESTRATION_TOOL_NAMES = (
    SUBSESSION_TOOL_NAMES
    | MANAGED_CONVERSATION_TOOL_NAMES
    | TASK_TOOL_NAMES
    | WORKFLOW_TOOL_NAMES
    | COMPOSITION_TOOL_NAMES
)

# ---------------------------------------------------------------------------
# Sub-session tool definitions
# ---------------------------------------------------------------------------

DELEGATE_TOOL = ToolDefinition(
    name="delegate",
    description=(
        "Delegate work to a focused sub-session and receive the result.\n\n"
        "Treat the child as an isolated context. For substantial work, provide a "
        "proportional objective, context, scope, acceptance, and return contract; "
        "do not make the child rediscover verified context.\n\n"
        "agent_id is required and must identify an eligible secondary specialist "
        "from the current caller-scoped target catalog. Use managed conversations "
        "for primary user agents.\n\n"
        "Before creating a fresh child, inspect existing sub-sessions. If one already "
        "owns the same problem and has relevant context, use follow_up_subsession for "
        "the same line of work or fork_subsession for an independent branch from that "
        "context. Start fresh only for genuinely new scope, deliberate independence, "
        "incompatible execution requirements, or demonstrably stale/polluted context. "
        "This applies to implementation, research, debugging, review, and other delegated "
        "work—not only code review.\n\n"
        "## Wait behavior\n\n"
        "Use wait=true when you need the result before continuing (e.g. joining "
        "parallel explorations). Multiple wait=true calls in one turn execute in "
        "parallel. Some conversation surfaces expose async background delegation; "
        "follow the current context instructions and visible schema. When "
        "delegate(wait=false) is available, use it only for bounded, non-interactive "
        "worker-style lookup or analysis with clear output and one final report; do "
        "not use it for open-ended CI/build/deploy/debug/browser/external-system/"
        "polling loops. wait=false means fire-and-follow-up, not fire-and-duplicate: "
        "do not continue the same scoped work in parallel, and end the parent turn "
        "after a short acknowledgement if there is no independent work that can "
        "safely proceed without the child result. The parent conversation is resumed "
        "or notified when the background delegation finishes. Completed results may include "
        "result_anchors for individual assistant messages; use tool-output anchor "
        "tools with the delegate call ID to inspect sections when available."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Clear bounded objective. Include scope/non-goals and acceptance "
                    "criteria here when they are central to the delegated task."
                ),
            },
            "agent_id": {
                "type": "string",
                "description": (
                    "Required eligible secondary specialist ID from the dynamic target catalog."
                ),
            },
            "agent_profile_id": {
                "type": ["string", "null"],
                "description": (
                    "Optional runtime profile ID for the target agent. Profiles are "
                    "agent-local variants for provider/model/reasoning and prompt tuning; "
                    "they do not change identity, memory, permissions, or tools."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Curated confirmed context, exact source-of-truth references, "
                    "dependencies, relevant decisions, and explicitly marked assumptions. "
                    "Do not dump the parent transcript."
                ),
            },
            "expected_output": {
                "type": "string",
                "description": (
                    "Required return contract: status, summary, changes or findings, "
                    "verification evidence, risks/assumptions, and open questions as relevant."
                ),
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "When true, blocks until the sub-session completes and returns "
                    "its output directly as the tool result. Multiple wait=true calls "
                    "in one turn run in parallel. When false, the sub-session "
                    "runs in the background and you receive a follow-up/resume "
                    "notification. Use false only for bounded, non-interactive "
                    "worker-style lookup or analysis with one final report when the "
                    "current conversation context exposes async background delegation; "
                    "do not duplicate the same scoped work in the parent turn."
                ),
                "default": False,
            },
        },
        "required": ["task", "agent_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
    dynamic_options=[
        ToolDynamicOption(
            path="$.agent_id",
            source="orchestration.delegate_targets",
        )
    ],
)

LIST_SUBSESSIONS_TOOL = ToolDefinition(
    name="list_subsessions",
    description=(
        "List child sub-sessions of the current session. Shows status, agent, "
        "task description, and result summary for each, keeping full result "
        "content out of the compact list. Use to check on "
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
        "sub-session's current status, task description, result summary, durable "
        "result_content for completed children, and result_anchors for assistant "
        "message sections when available."
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

RETRY_SUBSESSION_TOOL = ToolDefinition(
    name="retry_subsession",
    description=(
        "Retry a failed, interrupted, or cancelled delegate task on a new derived "
        "child session. The original remains inspectable. This reruns the original "
        "task; use follow_up_subsession for a new instruction."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Terminal child session to retry."},
        },
        "required": ["session_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

FOLLOW_UP_SUBSESSION_TOOL = ToolDefinition(
    name="follow_up_subsession",
    description=(
        "Send a new instruction using a terminal delegate child's full prior context. "
        "Creates a derived child because delegate history and results are immutable. "
        "Prefer this over a fresh delegate when continuing the same problem, correcting "
        "prior work, requesting deeper analysis, or rechecking a result with context "
        "that remains relevant."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Terminal child context to continue."},
            "instruction": {"type": "string", "description": "New specialist instruction."},
        },
        "required": ["session_id", "instruction"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

FORK_SUBSESSION_TOOL = ToolDefinition(
    name="fork_subsession",
    description=(
        "Branch from a terminal delegate child's full prior context with a new "
        "instruction. Creates an independent derived child while preserving lineage. "
        "Use this when the prior context is relevant but the new work should explore an "
        "alternative, obtain an independent branch, or proceed without changing the "
        "original continuation line."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Terminal child context to branch."},
            "instruction": {"type": "string", "description": "Instruction for the branch."},
        },
        "required": ["session_id", "instruction"],
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
        "deep research, multi-step analysis. Use tasks when the work needs durable "
        "workflow-shaped lifecycle tracking, deliverables, evaluation/review, gates, "
        "or longer background persistence. The task runs independently — you "
        "define it and the result is delivered to the conversation when complete. "
        "Tasks can spawn their own sub-sessions internally. The task owner agent is "
        "the durable main agent for visibility, gates, logs, and delivery; workflow "
        "steps may still run on system specialist agents. Do not pass system:* "
        "specialist IDs such as system:implement here — use those with delegate() "
        "or as workflow step overrides. Do not assign a project unless an exact "
        "project name, source path prefix, or remote URL match exists."
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
                "description": (
                    "Optional durable workflow owner agent ID. Omit for normal workflow "
                    "tasks to use the current/main agent. Do not pass system:* specialist "
                    "agents such as system:implement; those execute delegated work or "
                    "workflow steps, but should not own persistent tasks."
                ),
            },
            "agent_profile_id": {
                "type": "string",
                "description": (
                    "Optional runtime profile ID for the task owner agent. Workflow steps "
                    "may override this with their own agent_profile_id."
                ),
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
            "status": {
                "type": "string",
                "enum": ["draft", "queued"],
                "description": "Use draft to create without starting; queued (default) starts normally.",
                "default": "queued",
            },
            "draft": {
                "type": "boolean",
                "description": "When true, create the task without enqueueing it.",
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
            "interaction_mode_override": {
                "type": "string",
                "enum": ["none", "explicit_gates", "step_requests"],
                "description": (
                    "Optional task-level interaction policy. Use 'none' only when the user "
                    "requested fully autonomous execution; otherwise omit to use the workflow default."
                ),
            },
            "session_policy": {
                "type": "object",
                "description": (
                    "Optional Intaris session policy with allow_policies and deny_policies. "
                    "Prefer plain English strings for policy entries."
                ),
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
        "Answer a paused task step question set. Use get_task first to inspect "
        "the pending questions and available context, then provide structured answers so the task can resume."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the paused task.",
            },
            "mode": {
                "type": "string",
                "enum": ["structured", "plain_text"],
                "description": "Reply mode. Rich clients should use structured.",
                "default": "structured",
            },
            "answers": {
                "type": "array",
                "description": "Answers to the pending question set.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question_id": {"type": "string"},
                        "selected_option_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                        "custom_answer": {"type": "string"},
                    },
                    "required": ["question_id"],
                },
            },
        },
        "required": ["task_id", "answers"],
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
            "session_policy": {
                "type": "object",
                "description": (
                    "Optional replacement Intaris session policy with allow_policies "
                    "and deny_policies."
                ),
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
    RETRY_SUBSESSION_TOOL,
    FOLLOW_UP_SUBSESSION_TOOL,
    FORK_SUBSESSION_TOOL,
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

_CHAT_MODE_PROPERTY = {
    "type": "string",
    "enum": ["default", "plan", "build"],
    "description": (
        "Optional one-shot chat mode for this turn. Use plan for planning-only "
        "analysis, build for implementation-oriented execution after plan/review "
        "or when explicitly safe, or default for normal behavior."
    ),
    "default": "default",
}

AGENT_CONVERSATION_CREATE_TOOL = ToolDefinition(
    name="agent_conversation_create",
    description=(
        "Create a managed agent conversation: a normal main Cognis/Intaris conversation "
        "for another agent, controlled by this interactive chat. Use this when work "
        "needs a visible, inspectable, iterative agent session rather than a terminal "
        "delegate result or a structured workflow task. Target agent IDs must be "
        "primary/user agents; use delegate() for system specialist agents (`system:*`) "
        "available in this agent session. Not available in tasks. "
        "Treat the new conversation as an isolated context. For substantial work, "
        "initial_message should include a proportional contract: objective, confirmed "
        "context/references, scope and non-goals, acceptance/verification, and required "
        "return status/evidence. Do not force rediscovery of known context. "
        "With wait=false, this is "
        "fire-and-follow-up: after starting the managed turn, do "
        "not continue the same scoped work in parallel; finish the parent turn unless "
        "there is independent work that can safely proceed. The parent conversation "
        "will be resumed or notified when the managed turn finishes. Before creating, "
        "prefer reusing an existing relevant managed conversation via "
        "agent_conversation_send; create only when no suitable same-problem "
        "conversation exists or intentional separation is needed. Use "
        "agent_conversation_create(wait=false) for new visible iterative work loops "
        "outside the live channel, especially CI/build/deploy/debug/browser/"
        "external-system/polling workflows where the user may need to inspect or "
        "interact. For new implementation/debugging style managed conversations, "
        'prefer creating the first turn with chat_mode="plan"; after user or '
        "main-agent review, continue the same managed conversation with "
        'agent_conversation_send and chat_mode="build" instead of creating a '
        "duplicate. Clearly small read-only diagnostics may use default mode. Build "
        "mode is acceptable when explicitly requested or obviously safe."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Target agent ID."},
            "agent_profile_id": {
                "type": "string",
                "description": (
                    "Optional runtime profile ID for the target agent. If omitted, the "
                    "managed conversation uses the target agent default profile and does "
                    "not inherit the controller conversation profile. Eligible profile IDs "
                    "and descriptions are included in the dynamic target catalog."
                ),
            },
            "title": {"type": "string", "description": "Conversation title."},
            "initial_message": {
                "type": "string",
                "description": (
                    "First instruction and full relevant task contract for this new "
                    "managed conversation."
                ),
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "When true, wait for the started turn to finish before returning. "
                    "When false, start the turn asynchronously and rely on the follow-up/"
                    "resume notification instead of duplicating the same work in the parent."
                ),
                "default": False,
            },
            "chat_mode": _CHAT_MODE_PROPERTY,
        },
        "required": ["agent_id", "title", "initial_message"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
    dynamic_options=[
        ToolDynamicOption(
            path="$.agent_id",
            source="orchestration.managed_targets",
        )
    ],
)

AGENT_CONVERSATION_SEND_TOOL = ToolDefinition(
    name="agent_conversation_send",
    description=(
        "Send a new turn into an existing managed agent conversation. Prefer this for "
        "same-problem continuation instead of creating a duplicate managed "
        "conversation, including plan/debug to implementation handoffs with "
        'chat_mode="build". Reuse context the target already owns: send the new '
        "instruction and changed context, decisions, or acceptance criteria rather than "
        "repeating stable history. With wait=false, this is fire-and-follow-up: do not "
        "continue the same scoped work in parallel after sending; finish the parent "
        "turn unless independent work can safely proceed. The parent conversation "
        "will be resumed or notified when the managed turn finishes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string", "description": "Agent work conversation ID."},
            "message": {
                "type": "string",
                "description": (
                    "New instruction plus the context delta needed for this continuation."
                ),
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "When true, wait for the submitted turn to finish. When false, "
                    "send asynchronously and rely on the follow-up/resume notification "
                    "instead of duplicating the same work in the parent."
                ),
                "default": False,
            },
            "chat_mode": _CHAT_MODE_PROPERTY,
        },
        "required": ["conversation_id", "message"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

AGENT_CONVERSATION_WAIT_TOOL = ToolDefinition(
    name="agent_conversation_wait",
    description=(
        "Wait for the currently running turn in a managed agent conversation. Returns "
        "immediately when no turn is in progress. The parent join is bounded to 3600 seconds "
        "by default; timing out never cancels the managed child."
    ),
    parameters={
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string", "description": "Agent work conversation ID."},
            "timeout_seconds": {
                "type": "integer",
                "description": "Optional wait timeout in seconds (default 3600).",
                "default": 3600,
            },
        },
        "required": ["conversation_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

AGENT_CONVERSATION_SET_PROFILE_TOOL = ToolDefinition(
    name="agent_conversation_set_profile",
    description=(
        "Change the runtime profile for an idle managed agent conversation. The target "
        "profile must be enabled and agent-switchable for the managed agent. Active or "
        "queued turns must finish before the profile can be changed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string", "description": "Agent work conversation ID."},
            "agent_profile_id": {
                "type": "string",
                "description": "Agent-switchable runtime profile ID for the target agent.",
            },
            "reason": {
                "type": "string",
                "description": "Concise operational reason for changing the target profile.",
            },
        },
        "required": ["conversation_id", "agent_profile_id", "reason"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

AGENT_CONVERSATION_INTERRUPT_TOOL = ToolDefinition(
    name="agent_conversation_interrupt",
    description="Interrupt the active turn in a managed agent conversation.",
    parameters={
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string", "description": "Agent work conversation ID."},
            "reason": {"type": "string", "description": "Optional interrupt reason."},
        },
        "required": ["conversation_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

AGENT_CONVERSATION_RETRY_TOOL = ToolDefinition(
    name="agent_conversation_retry",
    description=(
        "Retry the last failed or interrupted Agent work turn in the same normal conversation. "
        "Use agent_conversation_send, not retry, when providing new instructions or clarification."
    ),
    parameters={
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string", "description": "Agent work conversation ID."},
            "wait": {
                "type": "boolean",
                "description": (
                    "When true, wait for the retried turn to finish. When false, rely on "
                    "the follow-up/resume notification and do not duplicate the same work "
                    "in the parent turn."
                ),
                "default": False,
            },
        },
        "required": ["conversation_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

AGENT_CONVERSATION_FORK_TOOL = ToolDefinition(
    name="agent_conversation_fork",
    description=(
        "Fork a managed agent conversation and optionally start the fork with a message. "
        "Use this when existing context is relevant but the work needs an independent "
        "branch; use agent_conversation_send for ordinary same-problem continuation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string", "description": "Agent work conversation ID."},
            "message": {"type": "string", "description": "Optional first message in the fork."},
            "wait": {"type": "boolean", "default": False},
            "chat_mode": _CHAT_MODE_PROPERTY,
        },
        "required": ["conversation_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

AGENT_CONVERSATION_CLOSE_TOOL = ToolDefinition(
    name="agent_conversation_close",
    description="Close a managed agent conversation control link.",
    parameters={
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string", "description": "Agent work conversation ID."},
            "reason": {"type": "string", "description": "Optional close reason."},
        },
        "required": ["conversation_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=False,
)

AGENT_CONVERSATION_LIST_TOOL = ToolDefinition(
    name="agent_conversation_list",
    description="List managed agent conversations controlled by this user/chat.",
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Optional state filter, or all."},
            "limit": {"type": "integer", "default": 25},
        },
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

AGENT_CONVERSATION_GET_TOOL = ToolDefinition(
    name="agent_conversation_get",
    description="Get current state for one managed agent conversation.",
    parameters={
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string", "description": "Agent work conversation ID."},
        },
        "required": ["conversation_id"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)

_ALL_MANAGED_CONVERSATION_TOOLS = [
    AGENT_CONVERSATION_CREATE_TOOL,
    AGENT_CONVERSATION_SEND_TOOL,
    AGENT_CONVERSATION_SET_PROFILE_TOOL,
    AGENT_CONVERSATION_WAIT_TOOL,
    AGENT_CONVERSATION_INTERRUPT_TOOL,
    AGENT_CONVERSATION_RETRY_TOOL,
    AGENT_CONVERSATION_FORK_TOOL,
    AGENT_CONVERSATION_CLOSE_TOOL,
    AGENT_CONVERSATION_LIST_TOOL,
    AGENT_CONVERSATION_GET_TOOL,
]


_SYNC_MANAGED_CONVERSATION_DESCRIPTIONS = {
    "agent_conversation_create": (
        "Create a managed agent conversation: a normal main Cognis/Intaris conversation "
        "for another agent, controlled by this interactive chat. Use this when work "
        "needs a visible, inspectable, iterative agent session rather than a terminal "
        "delegate result or a structured workflow task. Target agent IDs must be "
        "primary/user agents; use delegate() for system specialist agents (`system:*`) "
        "available in this agent session. Not available in tasks. On this conversation "
        "surface, the started managed turn is joined before returning."
    ),
    "agent_conversation_send": (
        "Send a new turn into an existing managed agent conversation. On this "
        "conversation surface, the submitted managed turn is joined before returning."
    ),
    "agent_conversation_retry": (
        "Retry the last failed or interrupted Agent work turn in the same normal "
        "conversation. Use agent_conversation_send, not retry, when providing new "
        "instructions or clarification. On this conversation surface, the retried "
        "managed turn is joined before returning."
    ),
    "agent_conversation_fork": (
        "Fork a managed agent conversation and optionally start the fork with a "
        "message. On this conversation surface, a started managed turn in the fork "
        "is joined before returning."
    ),
}


def _managed_conversation_sync_tool(tool: ToolDefinition) -> ToolDefinition:
    """Return a managed-conversation tool schema without async wait controls."""

    parameters = copy.deepcopy(tool_input_schema(tool))
    properties = parameters.get("properties")
    if isinstance(properties, dict):
        properties.pop("wait", None)
    return tool_with_input_schema(
        tool,
        parameters,
        description=_SYNC_MANAGED_CONVERSATION_DESCRIPTIONS.get(
            tool.name,
            (
                f"{tool.description} On this conversation surface, started turns "
                "are joined before returning."
            ),
        ),
    )


def _managed_conversation_default_wait_tool(
    tool: ToolDefinition, *, wait_default: bool
) -> ToolDefinition:
    """Return a managed-conversation tool schema with a surface-specific wait default."""

    if wait_default is False:
        return tool
    parameters = copy.deepcopy(tool_input_schema(tool))
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or "wait" not in properties:
        return tool
    properties["wait"]["default"] = True
    return tool_with_input_schema(
        tool,
        parameters,
        description=(
            f"{tool.description} On this conversation surface, wait defaults to true; "
            "set wait=false only when independent parent work can safely proceed."
        ),
    )


_SYNC_MANAGED_CONVERSATION_TOOLS = [
    _managed_conversation_sync_tool(AGENT_CONVERSATION_CREATE_TOOL),
    _managed_conversation_sync_tool(AGENT_CONVERSATION_SEND_TOOL),
    AGENT_CONVERSATION_SET_PROFILE_TOOL,
    AGENT_CONVERSATION_WAIT_TOOL,
    AGENT_CONVERSATION_INTERRUPT_TOOL,
    _managed_conversation_sync_tool(AGENT_CONVERSATION_RETRY_TOOL),
    _managed_conversation_sync_tool(AGENT_CONVERSATION_FORK_TOOL),
    AGENT_CONVERSATION_CLOSE_TOOL,
    AGENT_CONVERSATION_LIST_TOOL,
    AGENT_CONVERSATION_GET_TOOL,
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
        "Use this rarely: only when the work needs custom multi-step structure, strict "
        "deliverables, or an adapted reusable workflow. For ordinary timed or recurring "
        "tasks, use manage_schedules instead. When creating a task, omit agent_id for "
        "normal workflow ownership by the current/main agent; system:* specialist "
        "agents should execute workflow steps or delegated sub-sessions, not own the "
        "persistent task."
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
                    "interaction_mode_override": {
                        "type": "string",
                        "enum": ["none", "explicit_gates", "step_requests"],
                        "description": "Optional interaction policy for tasks created by this schedule. Defaults to none for scheduled tasks.",
                    },
                    "session_policy": {
                        "type": "object",
                        "description": "Optional Intaris session policy for tasks created by this schedule.",
                    },
                },
                "required": ["schedule_type"],
            },
            "delivery": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": [
                            "same_conversation",
                            "preferred_channel",
                            "latest_active_for_agent",
                            "specific_conversation",
                            "silent",
                        ],
                        "description": (
                            "Use same_conversation for work tied to this chat, "
                            "or preferred_channel for out-of-band task results."
                        ),
                    },
                    "target": {"type": "string"},
                    "completion_mode_family": {"type": "string", "enum": ["default", "direct"]},
                    "allow_silent_completion": {"type": "boolean"},
                },
            },
            "interaction_mode_override": {
                "type": "string",
                "enum": ["none", "explicit_gates", "step_requests"],
                "description": "Optional interaction policy for the created task. Omit to use the workflow default; use none only for fully autonomous work.",
            },
            "session_policy": {
                "type": "object",
                "description": "Optional Intaris session policy with allow_policies and deny_policies.",
            },
            "persist": {"type": "boolean", "default": False},
            "agent_id": {
                "type": "string",
                "description": (
                    "Optional durable task owner override. Omit for normal workflow "
                    "tasks to use the current/main agent. Do not pass system:* "
                    "specialist agents such as system:implement; those execute "
                    "delegated work or workflow steps, but should not own persistent tasks."
                ),
            },
            "agent_profile_id": {
                "type": "string",
                "description": (
                    "Optional runtime profile for the selected task/schedule agent. "
                    "Omit or pass null to use that agent's default profile. Workflow "
                    "steps may override this with their own agent_profile_id."
                ),
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
_DELEGATE_SYNC_TOOL = tool_with_input_schema(
    DELEGATE_TOOL,
    {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Clear description of what the sub-session should do.",
            },
            "agent_id": {
                "type": "string",
                "description": "Required eligible secondary specialist ID.",
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
        "required": ["task", "agent_id"],
    },
    name="delegate",
    description=(
        "Delegate work to a sub-session that runs synchronously. The sub-session "
        "executes the task and returns its output directly as the tool result. "
        "Use for focused sub-tasks within a workflow step. Multiple delegates "
        "can run in parallel via parallel tool calls. Target an eligible secondary "
        "specialist from the dynamic catalog."
    ),
)


def enrich_orchestration_target_catalog(
    tool: ToolDefinition,
    snapshot: OrchestrationTargetSnapshot | None,
) -> ToolDefinition:
    """Overlay one turn's target IDs and canonical metadata onto target tools."""

    if snapshot is None:
        return tool
    if tool.name == DELEGATE_TOOL.name:
        source = "orchestration.delegate_targets"
        targets = snapshot.delegate
    elif tool.name == AGENT_CONVERSATION_CREATE_TOOL.name:
        source = "orchestration.managed_targets"
        targets = snapshot.managed
    else:
        return tool

    schema = copy.deepcopy(tool_input_schema(tool))
    agent_field = schema.setdefault("properties", {}).setdefault("agent_id", {"type": "string"})
    if targets:
        agent_field["enum"] = [target.agent_id for target in targets]
    else:
        agent_field.pop("enum", None)
        schema["not"] = {}
    catalog_lines = []
    for target in targets:
        profiles = ", ".join(
            f"{profile['profile_id']}: {profile['description']}" for profile in target.profiles
        )
        summary = f"{target.agent_id} — {target.name}"
        if target.description:
            summary += f": {target.description}"
        if profiles:
            summary += f" [profiles: {profiles}]"
        catalog_lines.append(summary)
    base_description = str(agent_field.get("description") or "").rstrip()
    catalog = "\n".join(f"- {line}" for line in catalog_lines) or "- none"
    agent_field["description"] = f"{base_description}\n\nEligible targets for this turn:\n{catalog}"
    enriched = tool_with_input_schema(tool, schema)
    operations = []
    for operation in enriched.native_operations or []:
        options = [
            option.model_copy(update={"values": [target.as_dynamic_option() for target in targets]})
            if option.source == source
            else option
            for option in operation.dynamic_options
        ]
        operations.append(operation.model_copy(update={"dynamic_options": options}))
    if not operations:
        return enriched
    payload = enriched.model_dump(mode="python")
    payload["descriptor"] = None
    payload["native_operations"] = operations
    return ToolDefinition.model_validate(payload)


def orchestration_target_tool_available(
    tool: ToolDefinition,
    snapshot: OrchestrationTargetSnapshot | None,
) -> bool:
    """Return whether a target-creating tool has at least one discoverable target."""

    if snapshot is None:
        return True
    if tool.name == DELEGATE_TOOL.name:
        return bool(snapshot.delegate)
    if tool.name == AGENT_CONVERSATION_CREATE_TOOL.name:
        return bool(snapshot.managed)
    return True


def orchestration_tools(
    mode: OrchestrationMode = OrchestrationMode.FULL,
    *,
    expose_delegate_wait_option: bool = True,
    expose_managed_conversation_tools: bool = True,
    expose_managed_conversation_wait_option: bool = True,
    managed_conversation_wait_default: bool = False,
    expose_task_tools: bool = True,
    expose_workflow_tools: bool = True,
    expose_compose_workflow_tool: bool = True,
) -> list[ToolDefinition]:
    """Return orchestration tool definitions for the given mode.

    FULL: Sub-session tools plus optional managed-conversation, task, and
    workflow tools according to the conversation-surface policy.
    DELEGATE_SYNC_ONLY: Only sync delegate (task workflow steps).
    NONE: No orchestration tools (sub-sessions).
    """
    if mode == OrchestrationMode.NONE:
        return []
    if mode == OrchestrationMode.DELEGATE_SYNC_ONLY:
        return [_DELEGATE_SYNC_TOOL]
    # FULL mode. The delegate schema and managed-conversation tools can be
    # narrowed by conversation-surface policy while preserving the same
    # OrchestrationMode for runtime authorization.
    subsession_tools = list(_ALL_SUBSESSION_TOOLS)
    if not expose_delegate_wait_option:
        subsession_tools[0] = _DELEGATE_SYNC_TOOL
    tools = subsession_tools
    if expose_managed_conversation_tools:
        tools += (
            [
                _managed_conversation_default_wait_tool(
                    tool,
                    wait_default=managed_conversation_wait_default,
                )
                for tool in _ALL_MANAGED_CONVERSATION_TOOLS
            ]
            if expose_managed_conversation_wait_option
            else _SYNC_MANAGED_CONVERSATION_TOOLS
        )
    if expose_task_tools:
        tools += _ALL_TASK_TOOLS
    if expose_workflow_tools:
        tools += _ALL_WORKFLOW_TOOLS
    if expose_compose_workflow_tool:
        tools.append(COMPOSE_AND_RUN_WORKFLOW_TOOL)
    return tools


def is_orchestration_tool(tool_name: str) -> bool:
    """Return True when the tool is handled as a controller directive."""
    return tool_name in ORCHESTRATION_TOOL_NAMES


def is_subsession_tool(tool_name: str) -> bool:
    """Return True for sub-session management tools."""
    return tool_name in SUBSESSION_TOOL_NAMES


def is_task_tool(tool_name: str) -> bool:
    """Return True for task management tools."""
    return tool_name in TASK_TOOL_NAMES


def is_managed_conversation_tool(tool_name: str) -> bool:
    """Return True for agent work control tools."""

    return tool_name in MANAGED_CONVERSATION_TOOL_NAMES


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
    workspace_root: str | None = None,
    working_directory: str | None = None,
) -> tuple[ToolResult, SessionModel | None]:
    """Handle the delegate tool call — creates a child session.

    Returns a ``(ToolResult, child_session)`` tuple.  *child_session* is
    ``None`` when the session layer is unavailable or creation failed.
    """
    args = tool_call.arguments

    if session_manager is not None and session is not None and agent is not None:
        target_agent_id = str(args.get("agent_id") or "").strip()
        if agent_registry is None:
            target_error = OrchestrationTargetError(
                code="delegate_target_validation_unavailable",
                message="Delegate target validation is unavailable.",
            )
        else:
            try:
                target_agent = await OrchestrationTargetService(agent_registry).require(
                    OrchestrationTargetMode.DELEGATE,
                    target_agent_id=target_agent_id,
                    controller_agent=agent,
                    user_email=getattr(session, "user_email", ""),
                )
                target_error = None
            except OrchestrationTargetError as exc:
                target_error = exc
        if target_error is not None:
            return (
                ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "code": target_error.code,
                            "mode": "delegate",
                            "call_id": tool_call.call_id,
                            "message": str(target_error),
                        },
                        sort_keys=True,
                    ),
                    is_error=True,
                    metadata={"orchestration": True, "mode": "delegate"},
                ),
                None,
            )
        explicit_profile_id = normalize_agent_profile_id(args.get("agent_profile_id"))
        child_agent_profile_id = explicit_profile_id
        if child_agent_profile_id is not None:
            try:
                resolve_agent_profile(
                    target_agent,
                    child_agent_profile_id,
                    source="delegate_explicit",
                )
            except ValueError as exc:
                return (
                    ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "mode": "delegate",
                                "call_id": tool_call.call_id,
                                "message": str(exc),
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
                agent_id=target_agent_id,
                effective_agent_id=target_agent_id,
                agent_profile_id=child_agent_profile_id,
                expected_output=args.get("expected_output"),
                delegation_metadata=tool_call.runtime_metadata.get("delegate_lineage"),
                workspace_root=workspace_root,
                working_directory=working_directory,
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
                            "agent_profile_id": child_session.agent_profile_id,
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
            requested_agent_id = args.get("agent_id") or getattr(agent, "agent_id", "")
            if isinstance(exc, ValueError) and str(exc).startswith("Unknown agent:"):
                error_code = "delegate_agent_not_found"
                message = f"Agent '{requested_agent_id}' not found."
            elif isinstance(exc, ValueError):
                error_code = "delegate_child_session_invalid"
                message = "Delegated child session request is invalid."
            else:
                error_code = "delegate_child_session_creation_failed"
                message = "Unable to create delegated child session."
            logger.exception(
                "Delegate child session creation failed: call_id=%s parent_session_id=%s "
                "requested_agent_id=%s",
                tool_call.call_id,
                getattr(session, "session_id", None),
                requested_agent_id,
            )
            return (
                ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "code": error_code,
                            "mode": "delegate",
                            "call_id": tool_call.call_id,
                            "message": message,
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

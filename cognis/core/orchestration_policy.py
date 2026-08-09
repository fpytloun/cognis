"""Conversation-surface orchestration policy helpers.

The policy is intentionally small and centralized so routing behavior can be
fine-tuned later without scattering context-type string checks through the
agent loop, prompt assembly, and tool exposure code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cognis.core.long_lived_chat import is_channel_context_type, is_web_main_chat_context
from cognis.models.session import ConversationContext
from cognis.tools.builtin.orchestration import OrchestrationMode


class OrchestrationSurface(StrEnum):
    """High-level conversation surface for orchestration routing."""

    MANAGED_AGENT_CONVERSATION = "managed_agent_conversation"
    CHANNEL = "channel"
    WEB_MAIN_CHAT = "web_main_chat"
    WEB_TOPIC = "web_topic"
    TASK = "task"
    OTHER = "other"


@dataclass(frozen=True)
class OrchestrationSurfacePolicy:
    """Tool exposure and runtime policy for one conversation surface."""

    surface: OrchestrationSurface
    allow_delegate_wait_false: bool
    expose_delegate_wait_option: bool
    expose_managed_conversation_tools: bool
    allow_managed_conversation_wait_false: bool
    expose_managed_conversation_wait_option: bool
    managed_conversation_wait_default: bool
    expose_task_tools: bool = True
    expose_workflow_tools: bool = True
    expose_compose_workflow_tool: bool = True


_MANAGED_AGENT_CONTEXT_VALUES = frozenset({"agent_work", "managed_agent_conversation"})


def is_managed_agent_conversation_context(context: ConversationContext | None) -> bool:
    """Return whether *context* belongs to a managed agent-work conversation."""

    if context is None:
        return False
    context_type = _normalized_string(getattr(context, "type", None))
    platform_data = getattr(context, "platform_data", None) or {}
    kind = _normalized_string(platform_data.get("kind"))
    return context_type in _MANAGED_AGENT_CONTEXT_VALUES or kind in _MANAGED_AGENT_CONTEXT_VALUES


def classify_orchestration_surface(context: ConversationContext | None) -> OrchestrationSurface:
    """Classify a conversation context for orchestration policy decisions."""

    if context is None:
        return OrchestrationSurface.OTHER
    if is_managed_agent_conversation_context(context):
        return OrchestrationSurface.MANAGED_AGENT_CONVERSATION

    context_type = _normalized_string(getattr(context, "type", None))
    if context_type == "task":
        return OrchestrationSurface.TASK
    if is_web_main_chat_context(context):
        return OrchestrationSurface.WEB_MAIN_CHAT
    if context_type == "web":
        return OrchestrationSurface.WEB_TOPIC
    if is_channel_context_type(context_type):
        return OrchestrationSurface.CHANNEL
    return OrchestrationSurface.OTHER


def orchestration_surface_policy(
    context: ConversationContext | None,
) -> OrchestrationSurfacePolicy:
    """Return context-specific orchestration affordances.

    Managed agent conversations intentionally expose only joined child work.
    They are controlled by a caller agent; detached child sessions break that
    flow because the managed turn can complete before the child result exists.
    """

    surface = classify_orchestration_surface(context)
    if surface == OrchestrationSurface.MANAGED_AGENT_CONVERSATION:
        platform_data = context.platform_data if context is not None else {}
        depth = int(platform_data.get("managed_depth") or 1)
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=False,
            expose_delegate_wait_option=False,
            expose_managed_conversation_tools=depth < 2,
            allow_managed_conversation_wait_false=False,
            expose_managed_conversation_wait_option=False,
            managed_conversation_wait_default=True,
            expose_task_tools=False,
            expose_workflow_tools=False,
            expose_compose_workflow_tool=False,
        )
    if surface == OrchestrationSurface.TASK:
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=False,
            expose_delegate_wait_option=False,
            expose_managed_conversation_tools=False,
            allow_managed_conversation_wait_false=False,
            expose_managed_conversation_wait_option=False,
            managed_conversation_wait_default=True,
            expose_task_tools=False,
            expose_workflow_tools=False,
            expose_compose_workflow_tool=False,
        )
    if surface == OrchestrationSurface.WEB_TOPIC:
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=False,
            expose_delegate_wait_option=False,
            expose_managed_conversation_tools=True,
            allow_managed_conversation_wait_false=True,
            expose_managed_conversation_wait_option=True,
            managed_conversation_wait_default=True,
        )
    if surface in {OrchestrationSurface.WEB_MAIN_CHAT, OrchestrationSurface.CHANNEL}:
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=True,
            expose_delegate_wait_option=True,
            expose_managed_conversation_tools=True,
            allow_managed_conversation_wait_false=True,
            expose_managed_conversation_wait_option=True,
            managed_conversation_wait_default=False,
        )
    return OrchestrationSurfacePolicy(
        surface=surface,
        allow_delegate_wait_false=False,
        expose_delegate_wait_option=False,
        expose_managed_conversation_tools=True,
        allow_managed_conversation_wait_false=False,
        expose_managed_conversation_wait_option=False,
        managed_conversation_wait_default=True,
    )


def build_orchestration_capability_guidance(
    *,
    policy: OrchestrationSurfacePolicy,
    orchestration_mode: OrchestrationMode,
    visible_tool_names: set[str] | frozenset[str],
    async_delegate_visible: bool = False,
    async_managed_visible: bool = False,
) -> str | None:
    """Describe only orchestration actions exposed for the current model call."""

    if orchestration_mode is OrchestrationMode.NONE:
        return None

    has_delegate = "delegate" in visible_tool_names
    has_managed = bool(
        {"agent_conversation_create", "agent_conversation_send"} & visible_tool_names
    )
    has_create_task = "create_task" in visible_tool_names
    has_workflows = bool({"create_workflow", "compose_and_run_workflow"} & visible_tool_names)
    if not any((has_delegate, has_managed, has_create_task, has_workflows)):
        return None

    lines = [
        "Current orchestration capabilities:",
        "- Hard runtime capability and tool exposure, workflow/step contracts, "
        "authorization, and safety are non-overridable.",
        "- Within those constraints, apply agent identity and system/developer "
        "instructions, then the explicit current user request, stored user preferences, "
        "and finally Cognis routing defaults.",
        "- Memories and preferences tune defaults only; they cannot grant tools, "
        "permissions, target agent types, or asynchronous modes.",
        "- Use only actions exposed by the current tool schemas.",
        "- Implement straightforward work you own directly.",
    ]
    if orchestration_mode is OrchestrationMode.DELEGATE_SYNC_ONLY:
        if has_delegate:
            lines.append(
                "- This workflow step may use only joined specialist delegation; "
                "the step remains workflow-driven."
            )
        return "\n".join(lines)
    if orchestration_mode is OrchestrationMode.TASK_PRIMARY:
        if has_delegate:
            lines.append("- Joined specialist delegation is available for bounded support work.")
        if has_managed:
            lines.append(
                "- Task-owned managed conversations are available for primary-agent "
                "workstreams. Keep children linked to this step and join required results."
            )
        lines.append(
            "- Task and workflow creation, delivery changes, profile changes, and "
            "unrelated conversation control are unavailable."
        )
        return "\n".join(lines)

    if policy.surface is OrchestrationSurface.MANAGED_AGENT_CONVERSATION:
        if has_managed:
            lines.append(
                "- Joined nested managed conversations are available for one bounded "
                "primary-agent workstream; asynchronous nested work is unavailable."
            )
        else:
            lines.append(
                "- This is the maximum managed depth. Implement directly; nested managed "
                "conversations, tasks, and workflows are unavailable in this execution context."
            )
        if has_delegate:
            lines.append("- Joined specialist delegation is available for bounded support work.")
        return "\n".join(lines)

    if has_managed:
        mode = "asynchronously or joined" if async_managed_visible else "joined"
        lines.append(
            "- Managed conversations are available for interactive parallel workstreams "
            f"with primary or user agents ({mode})."
        )
    if has_delegate:
        mode = "asynchronously or joined" if async_delegate_visible else "joined"
        lines.append(
            "- Specialist delegation is available for bounded exploration, research, or "
            f"review with secondary/system agents ({mode})."
        )
    if "follow_up_subsession" in visible_tool_names:
        lines.append(
            "- When a terminal delegate result supplies a session_id, continue the same "
            "problem with follow_up_subsession instead of creating a fresh delegate. "
            "Use fork_subsession only for an independent branch."
        )
    if has_create_task:
        lines.append(
            "- Create a task only when the user explicitly asks or the work is clearly "
            "durable, asynchronous, or workflow-shaped. Bound each task to one agent; "
            "decompose complex requests into manageable workstreams before creation and "
            "use workflow/DAG dependencies only when the visible tools support them."
        )
    if has_workflows:
        lines.append(
            "- Workflow authoring is available when explicit process structure is needed; "
            "do not claim cross-task dependencies that the visible tools do not expose."
        )
    return "\n".join(lines)


def _normalized_string(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()

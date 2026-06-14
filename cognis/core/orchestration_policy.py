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
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=False,
            expose_delegate_wait_option=False,
            expose_managed_conversation_tools=False,
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
        )
    if surface in {OrchestrationSurface.WEB_MAIN_CHAT, OrchestrationSurface.CHANNEL}:
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=True,
            expose_delegate_wait_option=True,
            expose_managed_conversation_tools=True,
        )
    return OrchestrationSurfacePolicy(
        surface=surface,
        allow_delegate_wait_false=False,
        expose_delegate_wait_option=False,
        expose_managed_conversation_tools=True,
    )


def _normalized_string(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()

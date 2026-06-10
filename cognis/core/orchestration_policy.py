"""Conversation-surface orchestration policy helpers.

The policy is intentionally small and centralized so routing behavior can be
fine-tuned later without scattering context-type string checks through the
agent loop, prompt assembly, and tool exposure code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cognis.core.agent_direct import is_agent_direct_context
from cognis.core.long_lived_chat import is_channel_context_type
from cognis.models.session import ConversationContext


class OrchestrationSurface(StrEnum):
    """High-level conversation surface for orchestration routing."""

    MANAGED_AGENT_CONVERSATION = "managed_agent_conversation"
    CHANNEL = "channel"
    AGENT_DIRECT = "agent_direct"
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
    platform_data = getattr(context, "platform_data", None) or {}
    if context_type == "task":
        return OrchestrationSurface.TASK
    if is_agent_direct_context(context_type, platform_data):
        return OrchestrationSurface.AGENT_DIRECT
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
        )
    if surface == OrchestrationSurface.TASK:
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=False,
            expose_delegate_wait_option=False,
            expose_managed_conversation_tools=False,
        )
    if surface in {OrchestrationSurface.AGENT_DIRECT, OrchestrationSurface.WEB_TOPIC}:
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=False,
            expose_delegate_wait_option=False,
            expose_managed_conversation_tools=True,
        )
    return OrchestrationSurfacePolicy(
        surface=surface,
        allow_delegate_wait_false=True,
        expose_delegate_wait_option=True,
        expose_managed_conversation_tools=True,
    )


def _normalized_string(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()

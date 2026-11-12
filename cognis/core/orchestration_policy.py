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

_AGENT_BRIEFING_GUIDANCE = """\
## Agent briefs

Delegate when a specialist or a separate context clearly helps; do small,
well-understood work yourself.

Brief the agent like a capable colleague who has not seen this conversation. A
fresh child does not inherit the parent conversation or prior decisions. State
the outcome you need and why, with the relevant facts, exact references,
decisions, and real constraints, and mark assumptions as assumptions. Reference
source material rather than copying the parent transcript. Never include
secrets or hidden reasoning.

Scale the brief to the work. A simple lookup needs a question and the expected
answer. An investigation needs the question and known facts, not steps built on
an unverified hypothesis. Implementation needs a coherent write scope, expected
behavior, authority, and completion checks; let the implementer choose the
approach. Start an initial independent review in a fresh child, and give the
reviewer the original objective and acceptance criteria, the exact diff or
artifact, verification evidence, and relevant invariants, never an
implementation reasoning transcript. You keep complex diagnosis, trade-offs, and
final decisions.

Write briefs in normal sentences and short paragraphs separated by blank lines,
with short Markdown headings only for multi-part handoffs. Preserve identifiers,
numbers, paths, quoted text, and restrictions exactly. Ask for the result, its
evidence, and any blocker, not a report form. Check consequential claims before
building on them, and do not expand the assignment into unrelated work."""


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
        return OrchestrationSurfacePolicy(
            surface=surface,
            allow_delegate_wait_false=False,
            expose_delegate_wait_option=False,
            # Target filtering removes create/fork at the inherited depth
            # boundary while preserving control of existing links.
            expose_managed_conversation_tools=True,
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
        "Current orchestration capabilities (use only actions exposed by the current tool schemas):"
    ]
    lines.extend(
        _capability_lines(
            policy=policy,
            orchestration_mode=orchestration_mode,
            has_delegate=has_delegate,
            has_managed=has_managed,
            has_create_task=has_create_task,
            has_workflows=has_workflows,
            async_delegate_visible=async_delegate_visible,
            async_managed_visible=async_managed_visible,
        )
    )
    if has_delegate or has_managed:
        lines.extend(["", _AGENT_BRIEFING_GUIDANCE])
    return "\n".join(lines)


def _capability_lines(
    *,
    policy: OrchestrationSurfacePolicy,
    orchestration_mode: OrchestrationMode,
    has_delegate: bool,
    has_managed: bool,
    has_create_task: bool,
    has_workflows: bool,
    async_delegate_visible: bool,
    async_managed_visible: bool,
) -> list[str]:
    lines: list[str] = []
    if orchestration_mode is OrchestrationMode.DELEGATE_SYNC_ONLY:
        if has_delegate:
            lines.append(
                "- This workflow step may use only joined specialist delegation; "
                "the step remains workflow-driven."
            )
        return lines
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
        return lines

    if policy.surface is OrchestrationSurface.MANAGED_AGENT_CONVERSATION:
        if has_managed:
            lines.append(
                "- Joined nested managed conversations are available for one bounded "
                "primary-agent workstream; asynchronous nested work is unavailable."
            )
        else:
            lines.append(
                "- This is the maximum managed depth. Nested managed "
                "conversations, tasks, and workflows are unavailable in this execution context."
            )
        if has_delegate:
            lines.append("- Joined specialist delegation is available for bounded support work.")
        return lines

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
    if has_create_task:
        lines.append(
            "- Create a task only when the user explicitly asks or the work is clearly "
            "durable, asynchronous, or workflow-shaped. Bound each task to one agent and "
            "use workflow dependencies only when the visible tools support them."
        )
    if has_workflows:
        lines.append(
            "- Workflow authoring is available when explicit process structure is needed; "
            "do not claim cross-task dependencies that the visible tools do not expose."
        )
    return lines


def _normalized_string(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()

"""Chat-mode parsing and resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel
from cognis.models.tool import ToolCapability, ToolDefinition, tool_capabilities

type ChatMode = Literal["default", "plan", "build"]
type ChatModeSource = Literal[
    "one_shot",
    "conversation_override",
    "agent_default",
    "system_default",
]

CHAT_MODE_CONTEXT_KEY = "chat_mode"
CHAT_MODES: tuple[ChatMode, ...] = ("default", "plan", "build")


@dataclass(frozen=True, slots=True)
class ChatModeDirective:
    """A leading slash chat-mode directive parsed from user input."""

    mode: ChatMode
    one_shot: bool
    remaining_content: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedChatMode:
    """Effective chat mode for one turn."""

    mode: ChatMode
    source: ChatModeSource

    @property
    def read_only_required(self) -> bool:
        return self.mode == "plan"


def normalize_chat_mode(value: object, *, default: ChatMode = "default") -> ChatMode:
    """Return a supported chat mode, falling back to *default*."""

    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in CHAT_MODES:
            return candidate  # type: ignore[return-value]
    return default


def parse_chat_mode_directive(content: str) -> ChatModeDirective | None:
    """Parse leading /plan, /build, or /default chat-mode directives."""

    stripped = content.strip()
    if not stripped.startswith("/"):
        return None
    normalized = f"/{stripped[1:].lstrip()}"
    for mode in CHAT_MODES:
        command = f"/{mode}"
        if normalized == command:
            return ChatModeDirective(mode=mode, one_shot=False)
        if normalized.startswith(f"{command} "):
            remaining = normalized[len(command) :].strip()
            if remaining:
                return ChatModeDirective(mode=mode, one_shot=True, remaining_content=remaining)
    return None


def conversation_chat_mode_override(conversation: ConversationModel) -> ChatMode | None:
    """Return the persisted conversation chat-mode override, if any."""

    context = getattr(conversation, "context", None)
    platform_data = getattr(context, "platform_data", None) or {}
    value = platform_data.get(CHAT_MODE_CONTEXT_KEY)
    if isinstance(value, str):
        mode = normalize_chat_mode(value)
        if mode != "default":
            return mode
    return None


def agent_default_chat_mode(agent: AgentDefinition) -> ChatMode | None:
    """Return the configured default chat mode for an agent, if explicit."""

    raw_execution = getattr(agent, "execution", None)
    execution = raw_execution if isinstance(raw_execution, dict) else {}
    value = execution.get("default_chat_mode")
    if isinstance(value, str):
        mode = normalize_chat_mode(value)
        if mode != "default":
            return mode
    return None


def resolve_chat_mode(
    *,
    conversation: ConversationModel,
    agent: AgentDefinition,
    one_shot_mode: ChatMode | None = None,
) -> ResolvedChatMode:
    """Resolve effective chat mode for a turn."""

    if one_shot_mode is not None:
        return ResolvedChatMode(mode=one_shot_mode, source="one_shot")
    override = conversation_chat_mode_override(conversation)
    if override is not None:
        return ResolvedChatMode(mode=override, source="conversation_override")
    agent_default = agent_default_chat_mode(agent)
    if agent_default is not None:
        return ResolvedChatMode(mode=agent_default, source="agent_default")
    return ResolvedChatMode(mode="default", source="system_default")


def chat_mode_system_message(mode: ChatMode) -> str:
    """Return a user-facing confirmation for persistent chat-mode changes."""

    if mode == "plan":
        return "Plan mode enabled for this conversation. New turns will stay read-only until the mode changes."
    if mode == "build":
        return "Build mode enabled for this conversation. New turns may implement changes normally."
    return "Default chat mode restored for this conversation."


def plan_mode_reminder(*, source: ChatModeSource) -> str:
    """Return the controller reminder injected for plan-mode turns."""

    scope = "this turn" if source == "one_shot" else "this conversation"
    return (
        "<system-reminder>\n"
        f"Plan mode is active for {scope}. The user wants analysis, exploration, "
        "design, or planning only.\n\n"
        "You must remain read-only by behavior:\n"
        "- Do not edit, write, patch, delete, move, format, install, commit, "
        "push, deploy, or change external systems.\n"
        "- You may inspect code, search, read files, run read-only diagnostics, "
        "reason about architecture, ask clarifying questions, and produce plans.\n"
        "- Bash may be available, but only read-only commands are allowed.\n"
        "- If implementation is needed, explain that plan mode prevents changes "
        "in this turn.\n"
        "- Use subagents only for read-only exploration, research, architecture, "
        "or review; do not delegate mutating implementation work.\n"
        "</system-reminder>"
    )


def is_plan_hidden_tool(tool: ToolDefinition) -> bool:
    """Return whether a tool is clearly mutating and should be hidden in plan mode."""

    capabilities = tool_capabilities(tool)
    if ToolCapability.DESTRUCTIVE in capabilities or ToolCapability.PRIVILEGED in capabilities:
        return True
    if ToolCapability.WRITE in capabilities and tool.classification_status == "classified":
        return True
    if tool.source.type != "executor" or tool.category != "filesystem":
        return tool.name in {
            "memory_save_artifact",
            "memory_update",
            "memory_delete",
            "create_task",
            "update_task",
            "cancel_task",
            "retry_task",
            "respond_task_input",
            "resolve_task_pause",
            "create_workflow",
            "update_workflow",
            "delete_workflow",
            "duplicate_workflow",
            "compose_and_run_workflow",
            "manage_schedules",
            "image_generate",
            "image_edit",
            "document_generate",
            "artifact_publish",
        }
    return tool.name in {
        "write",
        "edit",
        "multiedit",
        "apply_patch",
        "artifact_save",
    }

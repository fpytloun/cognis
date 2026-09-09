"""Temporary default-deferral policy for low-frequency built-in tools."""

from __future__ import annotations

from collections.abc import Iterable

DEFAULT_DEFERRED_BUILTIN_FAMILIES: dict[str, frozenset[str]] = {
    "artifacts": frozenset({"artifact_list_recent", "artifact_search"}),
    "channels/conversations": frozenset(
        {
            "get_channel_delivery",
            "list_channel_accounts",
            "list_conversations",
            "read_channel_messages",
            "search_channel_targets",
            "send_channel_message",
            "summarize_conversation",
        }
    ),
    "datetime": frozenset({"convert_timezone", "format_datetime"}),
    "documents/images": frozenset({"image_edit", "image_generate"}),
    "knowledge bases": frozenset(
        {
            "knowledgebase_diagnostics",
            "knowledgebase_get",
            "knowledgebase_list",
            "knowledgebase_list_artifacts",
            "knowledgebase_list_jobs",
            "knowledgebase_status",
        }
    ),
    "memory": frozenset(
        {
            "memory_add_batch",
            "memory_ask",
            "memory_categories",
            "memory_delete",
            "memory_delete_artifact",
            "memory_find",
            "memory_get_artifact",
            "memory_get_artifact_url",
            "memory_list",
            "memory_list_artifacts",
            "memory_save_artifact",
            "memory_update",
        }
    ),
    "skills": frozenset({"skill_export", "skill_list", "skill_versions"}),
    "tasks": frozenset({"read_task_deliverable"}),
    "web research": frozenset({"web_crawl", "web_map"}),
}

DEFAULT_DEFERRED_BUILTIN_NAMES = frozenset(
    tool_name
    for tool_names in DEFAULT_DEFERRED_BUILTIN_FAMILIES.values()
    for tool_name in tool_names
)


def is_default_deferred_builtin(tool_name: str) -> bool:
    """Return whether a built-in tool is hidden from the default direct surface."""

    return tool_name in DEFAULT_DEFERRED_BUILTIN_NAMES


def deferred_builtin_family_names(tool_names: Iterable[str]) -> set[str]:
    """Return families represented by authorized hidden built-in tool names."""

    hidden_names = set(tool_names)
    return {
        family
        for family, family_tool_names in DEFAULT_DEFERRED_BUILTIN_FAMILIES.items()
        if hidden_names & family_tool_names
    }

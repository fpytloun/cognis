from __future__ import annotations

from cognis.core.tool_deferral import (
    DEFAULT_DEFERRED_BUILTIN_FAMILIES,
    DEFAULT_DEFERRED_BUILTIN_NAMES,
    deferred_builtin_family_names,
)


def test_default_deferred_builtin_policy_matches_reviewed_list() -> None:
    expected_families = {
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
    assert expected_families == DEFAULT_DEFERRED_BUILTIN_FAMILIES
    assert len(DEFAULT_DEFERRED_BUILTIN_NAMES) == 37


def test_deferred_family_lookup_is_sorted_by_caller_and_deduplicated() -> None:
    names = deferred_builtin_family_names(
        ["memory_delete", "memory_list", "artifact_search", "memory_delete", "web_search"]
    )

    assert names == {"artifacts", "memory"}

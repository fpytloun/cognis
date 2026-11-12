from __future__ import annotations

import pytest
from jsonschema import Draft7Validator

from cognis.core.tool_retrieval import retrieve_relevant_skills, retrieve_relevant_tools
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import (
    NativeToolOperation,
    ToolMutationKind,
    ToolSource,
    declared_default_semantics,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL
from cognis.tools.builtin.tool_search import (
    CALL_TOOL_TOOL,
    DESCRIBE_TOOL_TOOL,
    SEARCH_TOOLS_TOOL,
    resolve_inventory_tool,
    search_inventory,
)


def _tool(name: str, description: str, category: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category=category,
        read_only=True,
    )


def _mcp_tool(name: str, raw_name: str, server_id: str = "slack-example") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"MCP tool {raw_name}",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(
            type="local_mcp",
            server_id=server_id,
            server_name=server_id,
            raw_tool_name=raw_name,
        ),
        category="mcp",
        read_only=True,
    )


def test_search_inventory_returns_ranked_permission_filtered_matches() -> None:
    tools = [
        SEARCH_TOOLS_TOOL,
        _tool("read", "Read a file from disk", "filesystem"),
        _tool("glob", "Find files by pattern", "filesystem"),
        _tool("bash", "Run shell commands", "shell"),
    ]

    matches = search_inventory(tools, "read file", category="filesystem", limit=5)

    assert [match["name"] for match in matches] == ["read", "glob"]
    assert all(match["category"] == "filesystem" for match in matches)
    assert all(match["name"] != SEARCH_TOOLS_TOOL.name for match in matches)
    assert matches[0]["handle"]["tool_id"] == "builtin:read"
    assert matches[0]["handle"]["callable_name"] == "read"
    assert matches[0]["handle"]["scope"] == "session"
    assert matches[0]["handle"]["permission_scope"] == "current_session_effective_inventory"
    assert matches[0]["operation_count"] == 1
    assert "operations" not in matches[0]


def test_named_tool_rejection_parser_and_repair_are_bounded_and_inventory_scoped() -> None:
    from cognis.core.tool_rejection_recovery import (
        MAX_REJECTIONS_PER_TURN,
        plan_rejection_repair,
        rejected_tool_name,
    )

    read = _tool("read", "Read a file", "filesystem")
    bash = _tool("bash", "Run shell commands", "shell")
    assert (
        rejected_tool_name(
            "MidStreamFallbackError: Attempted to call tool 'bash' which was not in request.tools"
        )
        == "bash"
    )
    assert (
        rejected_tool_name(
            "Attempted to call tool 'repo_browser.list_directory' which was not in request.tools"
        )
        == "repo_browser.list_directory"
    )
    assert rejected_tool_name("invalid_request: malformed arguments for bash") is None

    hidden = plan_rejection_repair(
        "bash",
        inventory=[read, bash],
        aliases={},
        visible_tool_ids={"builtin:read"},
        seen_names=set(),
    )
    assert hidden is not None
    assert hidden.promoted_tool_id == "builtin:bash"
    assert "rejected call" in hidden.note

    unknown = plan_rejection_repair(
        "repo_browser.list_directory",
        inventory=[read, bash],
        aliases={},
        visible_tool_ids={"builtin:read", "builtin:bash"},
        seen_names=set(),
    )
    assert unknown is not None
    assert unknown.promoted_tool_id is None
    assert "search_tools" in unknown.note
    assert "repo_browser.list_directory" not in unknown.note or "authorized" in unknown.note

    assert (
        plan_rejection_repair(
            "bash",
            inventory=[read, bash],
            aliases={},
            visible_tool_ids={"builtin:read"},
            seen_names={"bash"},
        )
        is None
    )
    assert MAX_REJECTIONS_PER_TURN == 4
    assert (
        plan_rejection_repair(
            "read",
            inventory=[read, bash],
            aliases={},
            visible_tool_ids=set(),
            seen_names={"a", "b", "c", "d"},
        )
        is None
    )
    excluded = plan_rejection_repair(
        "bash",
        inventory=[read],  # The current effective inventory excludes bash.
        aliases={},
        visible_tool_ids={"builtin:read"},
        seen_names=set(),
    )
    assert excluded is not None
    assert excluded.promoted_tool_id is None


def test_named_tool_rejection_alias_must_resolve_uniquely_and_never_suffix_matches() -> None:
    from cognis.core.tool_rejection_recovery import plan_rejection_repair

    tool = _tool("list_directory", "List files", "filesystem")
    alias_repair = plan_rejection_repair(
        "visible_list_dir",
        inventory=[tool],
        aliases={"visible_list_dir": "list_directory"},
        visible_tool_ids=set(),
        seen_names=set(),
    )
    assert alias_repair is not None
    assert alias_repair.promoted_tool_id == "builtin:list_directory"
    assert (
        plan_rejection_repair(
            "repo_browser.list_directory",
            inventory=[tool],
            aliases={},
            visible_tool_ids=set(),
            seen_names=set(),
        ).promoted_tool_id
        is None
    )


def test_tool_rejection_recovery_state_persists_within_turn_and_resets_for_new_turn() -> None:
    from cognis.core.tool_rejection_recovery import ToolRejectionRecoveryState

    state = ToolRejectionRecoveryState.for_turn("turn-a")
    state.seen_names.update({"bash", "read_file"})

    # Same-turn profile switch or compaction re-entry retains the owner state.
    state.reset_for_new_turn("turn-a")
    assert state.seen_names == {"bash", "read_file"}

    state.seen_names.update({"mcp:calendar:list", "shell_exec"})
    assert len(state.seen_names) == 4
    state.reset_for_new_turn("turn-b")
    assert state.turn_id == "turn-b"
    assert state.seen_names == set()


def test_native_project_management_and_kb_discovery_are_searchable() -> None:
    from cognis.core.tool_arguments import validate_tool_arguments
    from cognis.tools.builtin.conversations import conversation_tools
    from cognis.tools.builtin.knowledgebase import knowledgebase_tools
    from cognis.tools.builtin.projects import project_tools

    tools = [*project_tools(), *knowledgebase_tools(), *conversation_tools()]
    calls = {
        "find_project_share_recipients": {"project_id": "proj_home", "query": "Romana"},
        "list_project_grants": {"project_id": "proj_home"},
        "create_project_grant": {
            "project_id": "proj_home",
            "grantee_user_email": "recipient@example.com",
            "permission": "member",
        },
        "update_project_grant": {
            "project_id": "proj_home",
            "grant_id": "pgrant_active",
            "permission": "viewer",
        },
        "revoke_project_grant": {"project_id": "proj_home", "grant_id": "pgrant_active"},
        "set_project_avatar": {"project_id": "proj_home", "content_ref": "img_generated"},
        "knowledgebase_list": {"query": "Egypt"},
        "knowledgebase_list_owned": {"query": "Egypt"},
        "list_project_knowledgebases": {"project_id": "proj_home", "query": "Egypt"},
        "assign_knowledgebase_to_project": {
            "project_id": "proj_home",
            "knowledgebase_id": "kb_egypt",
        },
        "unassign_knowledgebase_from_project": {
            "project_id": "proj_home",
            "knowledgebase_id": "kb_egypt",
        },
        "list_project_conversations": {"project_id": "proj_home"},
        "list_project_tasks": {"project_id": "proj_home", "limit": 1},
        "restore_project": {"project_id": "proj_home"},
        "leave_project": {"project_id": "proj_home", "make_private": True},
        "set_conversation_project": {"conversation_id": "conv_owned", "project_id": None},
    }
    for name, arguments in calls.items():
        matches = search_inventory(tools, name, limit=20)
        assert name in {match["name"] for match in matches}
        tool = resolve_inventory_tool(tools, f"builtin:{name}")
        assert tool is not None
        assert tool.descriptor.operations[0].operation == name
        assert validate_tool_arguments(name, arguments, schema=tool.parameters) is None
    tool = resolve_inventory_tool(tools, "create_project_grant")
    assert validate_tool_arguments(
        tool.name,
        {
            "project_id": "proj_home",
            "grantee_user_email": "recipient@example.com",
            "permission": "owner",
        },
        schema=tool.parameters,
    )
    # Searching a narrower allowed inventory never leaks the omitted capability.
    assert (
        search_inventory(
            [tool for tool in tools if tool.category == "knowledgebase"],
            "set_project_avatar",
            category="project",
        )
        == []
    )


def test_search_inventory_adds_compact_multi_operation_index() -> None:
    matches = search_inventory([MANAGE_AGENTS_TOOL], "manage agents", limit=5)

    assert len(matches) == 1
    result = matches[0]
    assert result["operation_count"] == len(MANAGE_AGENTS_TOOL.descriptor.operations)
    assert result["operations"] == [
        {
            "operation": operation.operation,
            "summary": operation.summary,
            "mutation_kind": operation.mutation_kind.value,
        }
        for operation in MANAGE_AGENTS_TOOL.descriptor.operations[:25]
    ]
    assert all("input_schema" not in operation for operation in result["operations"])
    assert result["operations_truncated"] is True


def test_schedule_project_operations_preserve_null_and_omission_contract() -> None:
    from cognis.core.tool_arguments import validate_tool_arguments
    from cognis.tools.argument_normalization import strip_empty_optional_values
    from cognis.tools.builtin.orchestration import UPDATE_TASK_TOOL
    from cognis.tools.builtin.schedule import MANAGE_SCHEDULES_TOOL

    for arguments in (
        {"action": "list", "project_id": "proj_home"},
        {
            "action": "create",
            "name": "Home",
            "schedule_type": "interval",
            "interval_seconds": 60,
            "project_id": "proj_home",
        },
        {"action": "update", "schedule_id": "sched_owned", "project_id": None},
    ):
        normalized = strip_empty_optional_values(arguments, MANAGE_SCHEDULES_TOOL.parameters)
        assert "project_id" in normalized and normalized["project_id"] == arguments["project_id"]
        assert (
            validate_tool_arguments(
                "manage_schedules", normalized, schema=MANAGE_SCHEDULES_TOOL.parameters
            )
            is None
        )
    assert (
        validate_tool_arguments(
            "update_task",
            {"task_id": "task_owned", "project_id": None},
            schema=UPDATE_TASK_TOOL.parameters,
        )
        is None
    )


def test_search_inventory_caps_operation_index_at_25() -> None:
    operations = [
        NativeToolOperation(
            operation=f"read_{index:02d}",
            summary=f"Read item {index}.",
            mutation_kind=ToolMutationKind.READ,
            input_schema={
                "type": "object",
                "properties": {"action": {"const": f"read_{index:02d}"}},
                "required": ["action"],
            },
            semantics=declared_default_semantics(ToolMutationKind.READ),
        )
        for index in range(26)
    ]
    tool = ToolDefinition(
        name="large_reader",
        description="Read many item types.",
        parameters={},
        source=ToolSource(type="builtin"),
        category="system",
        read_only=True,
        native_operations=operations,
    )

    result = search_inventory([tool], "many item types", limit=5)[0]

    assert result["operation_count"] == 26
    assert len(result["operations"]) == 25
    assert result["operations_truncated"] is True
    assert result["operations"][-1]["operation"] == "read_24"


def test_search_inventory_limits_results() -> None:
    tools = [_tool(f"tool_{index}", "Search helper", "system") for index in range(30)]

    matches = search_inventory(tools, "search", limit=50)

    assert len(matches) == 20


def test_call_tool_definition_uses_closed_envelope_schema() -> None:
    assert CALL_TOOL_TOOL.parameters["required"] == ["tool", "arguments"]
    assert CALL_TOOL_TOOL.parameters["additionalProperties"] is False
    assert CALL_TOOL_TOOL.parameters["properties"]["arguments"]["type"] == "object"


def test_describe_tool_operation_selector_rejects_empty_string() -> None:
    validator = Draft7Validator(DESCRIBE_TOOL_TOOL.parameters)

    errors = list(validator.iter_errors({"tool": "manage_agents", "operation": ""}))

    assert DESCRIBE_TOOL_TOOL.parameters["properties"]["operation"]["minLength"] == 1
    assert len(errors) == 1
    assert errors[0].validator == "minLength"


def test_resolve_inventory_tool_prefers_stable_id_and_requires_unique_name() -> None:
    first = _mcp_tool("shared_name", "search_messages", server_id="gmail")
    second = _mcp_tool("shared_name", "search_messages", server_id="archive")

    assert resolve_inventory_tool([first, second], "mcp:gmail:search_messages") is first
    assert resolve_inventory_tool([first, second], "shared_name") is None


def _slack_tool_with_opaque_id(server_id: str = "mcp_111111111111") -> ToolDefinition:
    tool = _mcp_tool(
        "mcp_slack-example__conversations_search_messages",
        "conversations_search_messages",
        server_id=server_id,
    )
    return tool.model_copy(
        update={"source": tool.source.model_copy(update={"server_name": "slack-example"})}
    )


@pytest.mark.parametrize(
    "reference",
    [
        "mcp:mcp_111111111111:conversations_search_messages",
        "mcp:slack-example:conversations_search_messages",
        "mcp_slack-example__conversations_search_messages",
        "conversations_search_messages",
        "mcp__cognis__mcp__slack-example__conversations_search_messages",
    ],
)
def test_inventory_reference_resolution_and_selection_share_aliases(reference: str) -> None:
    tool = _slack_tool_with_opaque_id()
    assert resolve_inventory_tool([tool], reference) is tool
    matches = search_inventory([tool], f"select:{reference}")
    assert [match["tool_id"] for match in matches] == [
        "mcp:mcp_111111111111:conversations_search_messages"
    ]
    assert resolve_inventory_tool([], reference) is None


@pytest.mark.parametrize(
    "reference",
    [
        "mcp:slack-example:conversations_search_messages",
        "conversations_search_messages",
        "mcp_slack-example__conversations_search_messages",
    ],
)
def test_inventory_reference_ambiguity_never_selects_first_match(reference: str) -> None:
    first = _slack_tool_with_opaque_id()
    second = _slack_tool_with_opaque_id("mcp_second_workspace")
    for tools in ([first, second], [second, first]):
        assert resolve_inventory_tool(tools, reference) is None
        assert search_inventory(tools, f"select:{reference}") == []
        assert search_inventory(tools, reference, limit=1) == []
        assert (
            resolve_inventory_tool(tools, "mcp:mcp_111111111111:conversations_search_messages")
            is first
        )


@pytest.mark.parametrize(
    "query",
    [
        "+slack-example conversations_search_messages",
        "slack-example conversations_search_messages",
        "mcp:slack-example:conversations_search_messages",
        "mcp_slack-example__conversations_search_messages",
        "conversations_search_messages",
    ],
)
@pytest.mark.parametrize("already_visible", [False, True])
def test_exact_tool_search_returns_target_even_if_visible(
    query: str, already_visible: bool
) -> None:
    target = _slack_tool_with_opaque_id()
    unrelated = _mcp_tool("mcp_slack-example__usergroups_create", "usergroups_create")
    unrelated.description = "slack example " * 30
    matches = search_inventory(
        [unrelated, target],
        query,
        category="mcp",
        limit=1,
        already_visible_tool_ids=(
            {"mcp:mcp_111111111111:conversations_search_messages"} if already_visible else set()
        ),
    )
    assert len(matches) == 1
    assert matches[0]["tool_id"] == "mcp:mcp_111111111111:conversations_search_messages"
    assert matches[0]["already_visible"] is already_visible


def test_search_inventory_omits_already_visible_tools() -> None:
    tools = [
        _tool("get_cart", "Get current cart contents", "mcp"),
        _tool("repeat_order", "Repeat a previous order", "mcp"),
    ]

    matches = search_inventory(
        tools,
        "repeat order",
        category="mcp",
        already_visible_tool_ids={"builtin:get_cart"},
        limit=5,
    )

    assert [match["name"] for match in matches] == ["repeat_order"]


def test_search_inventory_accepts_profile_group_alias_categories() -> None:
    tools = [
        _tool("skill_load", "Load a named skill", "skill"),
        _tool("read_tool_output", "Read saved tool output by call id", "context"),
        _tool("bash", "Run shell commands", "shell"),
    ]

    skill_matches = search_inventory(tools, "load skill", category="system", limit=5)
    output_matches = search_inventory(
        tools,
        "read saved output",
        category="system",
        limit=5,
    )

    assert skill_matches[0]["name"] == "skill_load"
    assert output_matches[0]["name"] == "read_tool_output"
    assert skill_matches[0]["profile_group"] == "system"
    assert output_matches[0]["profile_group"] == "system"


def test_search_inventory_treats_category_as_hint_not_hard_filter() -> None:
    tools = [
        _tool("bash", "Run shell commands in a terminal", "shell"),
        _tool("read_tool_output", "Read saved tool output", "context"),
    ]

    matches = search_inventory(
        tools,
        "bash shell command execution terminal tool",
        category="system",  # wrong hint from the model
        limit=5,
    )

    assert matches
    assert matches[0]["name"] == "bash"


def test_search_inventory_category_hint_still_boosts_matching_tools() -> None:
    tools = [
        _tool("bash", "Run shell commands in a terminal", "shell"),
        _tool("task_list", "List system tasks and runtime state", "system"),
    ]

    matches = search_inventory(
        tools,
        "tasks",
        category="system",
        limit=5,
    )

    assert matches
    assert matches[0]["name"] == "task_list"


def test_search_inventory_uses_bm25_for_multi_term_mcp_queries() -> None:
    tools = [
        _tool(
            "mcp_googleworkspace__search_messages",
            "Search Gmail messages and calendar events in Google Workspace",
            "mcp",
        ),
        _tool(
            "mcp_rohlik__fetch_orders",
            "Retrieve delivered and upcoming grocery orders",
            "mcp",
        ),
        _tool(
            "mcp_todoist__find_tasks",
            "Find Todoist tasks by text and date",
            "mcp",
        ),
    ]

    matches = search_inventory(
        tools,
        "Google Workspace tools for calendar events and Gmail search content retrieval",
        category="mcp",
        limit=5,
    )

    assert matches[0]["name"] == "mcp_googleworkspace__search_messages"


def test_search_inventory_selects_claude_mcp_prefixed_tool_names() -> None:
    tools = [
        _tool("skill_load", "Load a skill", "skill"),
        _mcp_tool(
            "mcp_slack-example__conversations_search_messages",
            "conversations_search_messages",
        ),
        _mcp_tool("mcp_slack-example__channels_list", "channels_list"),
    ]

    matches = search_inventory(
        tools,
        (
            "select:mcp__cognis__skill_load,"
            "mcp__cognis__mcp_slack-example__conversations_search_messages,"
            "mcp__cognis__mcp_slack-example__channels_list"
        ),
        limit=5,
    )

    assert [match["handle"]["callable_name"] for match in matches] == [
        "skill_load",
        "mcp_slack-example__conversations_search_messages",
        "mcp_slack-example__channels_list",
    ]
    assert all(match["handle"]["confidence"] == 100.0 for match in matches)


def test_search_inventory_select_accepts_stable_ids_and_raw_names() -> None:
    tools = [
        _mcp_tool(
            "mcp_slack-example__conversations_search_messages",
            "conversations_search_messages",
        ),
        _mcp_tool("mcp_slack-example__channels_list", "channels_list"),
    ]

    matches = search_inventory(
        tools,
        "select:mcp:slack-example:conversations_search_messages,channels_list,missing",
        limit=5,
    )

    assert [match["handle"]["callable_name"] for match in matches] == [
        "mcp_slack-example__conversations_search_messages",
        "mcp_slack-example__channels_list",
    ]


def test_search_inventory_select_accepts_double_underscore_mcp_aliases() -> None:
    tools = [
        _mcp_tool(
            "mcp_slack-example__conversations_search_messages",
            "conversations_search_messages",
        ),
        _mcp_tool("mcp_slack-example__channels_list", "channels_list"),
    ]

    matches = search_inventory(
        tools,
        (
            "select:mcp__cognis__mcp__slack-example__conversations_search_messages,"
            "mcp__cognis__mcp__slack-example__channels_list"
        ),
        limit=5,
    )

    assert [match["handle"]["callable_name"] for match in matches] == [
        "mcp_slack-example__conversations_search_messages",
        "mcp_slack-example__channels_list",
    ]


def test_search_inventory_select_returns_already_visible_tools() -> None:
    tools = [
        SEARCH_TOOLS_TOOL,
        _tool("skill_load", "Load a skill", "skill"),
        _mcp_tool("mcp_slack-example__channels_list", "channels_list"),
    ]

    matches = search_inventory(
        tools,
        "select:mcp__cognis__search_tools,mcp__cognis__skill_load,mcp__cognis__mcp_slack-example__channels_list",
        already_visible_tool_ids={"builtin:skill_load"},
        limit=5,
    )

    assert [match["handle"]["callable_name"] for match in matches] == [
        "skill_load",
        "mcp_slack-example__channels_list",
    ]
    assert matches[0]["already_visible"] is True
    assert matches[1]["already_visible"] is False


def test_retrieve_relevant_skills_returns_exact_skill_match() -> None:
    skills = [
        {
            "skill_id": "skill_daily_brief",
            "name": "daily-brief",
            "description": "Build a Czech morning briefing with agenda, news, markets, and weather.",
            "tags": ["briefing", "news"],
        },
        {
            "skill_id": "skill_shopping",
            "name": "rohlik-smart-shopping",
            "description": "Handle smart grocery shopping tasks.",
            "tags": ["shopping"],
        },
    ]

    matches = retrieve_relevant_skills(
        "Load and run the daily-brief skill for today's morning briefing",
        skills,
        loaded_skill_ids=set(),
    )

    assert len(matches) == 1
    assert matches[0].skill_id == "skill_daily_brief"


def test_retrieve_relevant_tools_drops_already_visible_tools() -> None:
    tools = [
        _tool("image_edit", "Edit image artifacts using a text prompt", "image"),
        _tool("web_search", "Search the web for information", "web"),
    ]

    matches = retrieve_relevant_tools(
        "edit this image",
        tools,
        already_visible_tool_ids={"builtin:image_edit"},
    )

    assert matches == []

from __future__ import annotations

from cognis.core.tool_exposure import (
    LLMApiMode,
    ToolDiscoveryMode,
    ToolExposureContract,
    prepare_tool_exposure,
)
from cognis.models.config import ModelInfo
from cognis.models.tool import ToolDefinition, ToolSource, sanitize_mcp_tool_name, stable_tool_id
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL


def _tool(name: str, *, source_type: str = "builtin", category: str = "system") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type=source_type),
        category=category,
        read_only=True,
    )


def _write_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="executor"),
        category="filesystem",
        read_only=False,
    )


def _mcp(server: str, raw_name: str, *, category: str = "mcp") -> ToolDefinition:
    return ToolDefinition(
        name=sanitize_mcp_tool_name(server, raw_name),
        description=f"{server} {raw_name}",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name=server, raw_tool_name=raw_name),
        category=category,
    )


def _contract(
    *,
    llm_api: LLMApiMode = LLMApiMode.CHAT_COMPLETIONS,
    discovery_mode: ToolDiscoveryMode = ToolDiscoveryMode.CONTROLLER_SEARCH,
    native_apply_patch: bool = False,
    anthropic_defer_loading: bool = True,
) -> ToolExposureContract:
    return ToolExposureContract(
        llm_api=llm_api,
        discovery_mode=discovery_mode,
        native_apply_patch=native_apply_patch,
        anthropic_defer_loading=anthropic_defer_loading,
    )


def _search_schema() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": SEARCH_TOOLS_TOOL.name,
            "description": SEARCH_TOOLS_TOOL.description,
            "parameters": SEARCH_TOOLS_TOOL.parameters,
        },
    }


def test_prepare_tool_exposure_uses_anthropic_deferred_loading_with_controller_search() -> None:
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        _mcp("github", "search/issues"),
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="claude-sonnet-4", supports_defer_loading=True),
        contract=_contract(),
        promoted_tool_ids=set(),
    )

    tool_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert result.debug_metadata["strategy"] == "anthropic_defer_loading"
    assert result.debug_metadata["discovery_mode"] == "controller_search"
    assert tool_names == ["search_tools", "read", sanitize_mcp_tool_name("github", "search/issues")]
    assert tool_names != sorted(tool_names, key=str.casefold)
    read_schema = next(
        tool for tool in result.tools if tool.get("function", {}).get("name") == "read"
    )
    assert read_schema["function"]["cache_control"] == {"type": "ephemeral"}
    deferred = [
        tool for tool in result.tools if tool.get("function", {}).get("name", "").startswith("mcp_")
    ]
    assert deferred[0]["function"]["defer_loading"] is True


def test_prepare_tool_exposure_can_disable_anthropic_deferred_loading() -> None:
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        _mcp("github", "search/issues"),
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="claude-sonnet-4", supports_defer_loading=True),
        contract=_contract(anthropic_defer_loading=False),
        promoted_tool_ids=set(),
    )

    tool_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert result.debug_metadata["strategy"] == "generic_search_tools"
    assert "search_tools" in tool_names
    assert not any(tool.get("function", {}).get("defer_loading") is True for tool in result.tools)


def test_prepare_tool_exposure_uses_generic_search_fallback_with_promoted_tools() -> None:
    mcp_tool = _mcp("github", "search/issues")
    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-4o-mini", max_tools=3),
        contract=_contract(),
        promoted_tool_ids={stable_tool_id(mcp_tool)},
    )

    tool_names = [tool["function"]["name"] for tool in result.tools if tool["type"] == "function"]
    assert result.debug_metadata["strategy"] == "generic_search_tools"
    assert "search_tools" in tool_names
    assert sanitize_mcp_tool_name("github", "search/issues") in tool_names


def test_prepare_tool_exposure_uses_responses_controller_search_for_openai() -> None:
    mcp_tool = _mcp("github", "search/issues")

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids=set(),
    )

    assert result.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
    assert result.debug_metadata["llm_api"] == "responses"
    assert result.request_kwargs["tool_choice"] == "auto"
    assert all(tool["type"] != "namespace" for tool in result.tools)
    assert all(tool["type"] != "tool_search" for tool in result.tools)
    assert any(tool.get("function", {}).get("name") == "search_tools" for tool in result.tools)


def test_prepare_tool_exposure_responses_visible_only_when_search_disabled() -> None:
    mcp_tool = _mcp("github", "search/issues")

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES, discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        allow_tool_search=False,
    )

    function_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert result.debug_metadata["strategy"] == "openai_responses_visible_only"
    assert "search_tools" not in function_names
    assert sanitize_mcp_tool_name("github", "search/issues") not in function_names
    assert function_names == ["read"]


def test_prepare_tool_exposure_sorts_final_responses_tools_by_visible_name() -> None:
    result = prepare_tool_exposure(
        inventory_tools=[
            _tool("web_search", category="web"),
            _tool("bash", source_type="executor", category="shell"),
            _tool("read", source_type="executor", category="filesystem"),
        ],
        controller_tool_schemas=[
            _search_schema(),
            {
                "type": "function",
                "function": {
                    "name": "step_complete",
                    "description": "complete",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids=set(),
    )

    function_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert function_names == sorted(function_names, key=str.casefold)


def test_prepare_tool_exposure_orders_native_apply_patch_alphabetically() -> None:
    result = prepare_tool_exposure(
        inventory_tools=[
            _tool("read", source_type="executor", category="filesystem"),
            _write_tool("apply_patch"),
            _tool("bash", source_type="executor", category="shell"),
        ],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="gpt-5.1-codex", supports_responses_api=True, max_tools=128),
        contract=_contract(
            llm_api=LLMApiMode.RESPONSES,
            discovery_mode=ToolDiscoveryMode.NONE,
            native_apply_patch=True,
        ),
        promoted_tool_ids=set(),
        allow_tool_search=False,
    )

    names = [
        tool.get("function", {}).get("name")
        if isinstance(tool.get("function"), dict)
        else tool.get("type")
        for tool in result.tools
    ]
    assert names == ["apply_patch", "bash", "read"]


def test_prepare_tool_exposure_keeps_skill_and_tool_output_helpers_visible_under_fallback_cap() -> (
    None
):
    deferred_mcp = _mcp("todoist", "find/tasks")
    inventory = [
        _tool("skill_load", category="skill"),
        _tool("read_tool_output", category="context"),
        _tool("search_tool_output", category="context"),
        _tool("list_tool_output_anchors", category="context"),
        _tool("read_tool_output_anchor", category="context"),
        _tool("bash", source_type="executor", category="shell"),
        _tool("web_search", category="web"),
        deferred_mcp,
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=6),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids=set(),
    )

    tool_names = [tool["function"]["name"] for tool in result.tools if tool["type"] == "function"]
    assert result.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
    assert set(tool_names) == {
        "search_tools",
        "skill_load",
        "read_tool_output",
        "search_tool_output",
        "list_tool_output_anchors",
        "read_tool_output_anchor",
    }
    assert tool_names == sorted(tool_names, key=str.casefold)


def test_promoted_tool_always_surfaces_next_turn_even_when_hidden() -> None:
    """Promoted tools (from search_tools or skill activation) must become visible
    on the next turn regardless of which hidden bucket they came from."""
    get_events = _mcp("googleworkspace", "get_events")
    get_form = _mcp("googleworkspace", "get_form")
    read = _tool("read", source_type="executor", category="filesystem")

    # get_events is hidden (not in default_visible_tool_ids), get_form is also hidden.
    # The model searched and found get_events — it should be promoted visible.
    result = prepare_tool_exposure(
        inventory_tools=[read, get_events, get_form],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids={stable_tool_id(get_events)},
        default_visible_tool_ids={stable_tool_id(read)},
    )

    assert result.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
    assert stable_tool_id(get_events) in result.visible_tool_ids
    assert stable_tool_id(get_form) not in result.visible_tool_ids
    assert stable_tool_id(get_events) not in result.hidden_searchable_tool_ids


def test_promoted_tool_wins_slots_over_irrelevant_mcp_under_cap() -> None:
    """Under a tight slot cap, promoted tools must win over arbitrary MCP tools."""
    get_events = _mcp("googleworkspace", "get_events")
    get_form = _mcp("googleworkspace", "get_form")
    get_drive = _mcp("googleworkspace", "get_drive_shareable_link")
    read = _tool("read", source_type="executor", category="filesystem")

    # Tight cap: only 2 inventory slots (controller schemas take the rest).
    # get_events is promoted; get_form and get_drive are not.
    result = prepare_tool_exposure(
        inventory_tools=[read, get_events, get_form, get_drive],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=3),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids={stable_tool_id(get_events)},
        default_visible_tool_ids={stable_tool_id(read)},
    )

    assert stable_tool_id(get_events) in result.visible_tool_ids
    assert stable_tool_id(get_form) not in result.visible_tool_ids
    assert stable_tool_id(get_drive) not in result.visible_tool_ids


def test_promoted_tool_surfaces_when_policy_visible_but_cap_hidden() -> None:
    """Regression: a tool that is policy-visible but excluded from the cap-limited
    visible set must still surface when promoted via search_tools.

    This was the bug behind the daily-brief failure: search_tools correctly
    found get_events among hidden policy-visible tools, added it to
    promoted_tool_ids, but prepare_tool_exposure filtered it out of
    promoted_visible because it was in visible_defaults. Under the OpenAI
    fallback slot cap the tool therefore never surfaced.
    """
    read = _tool("read", source_type="executor", category="filesystem")
    # Many policy-visible MCP tools saturate the slot cap.
    filler = [_mcp("ws", f"tool_{i:03d}") for i in range(20)]
    get_events = _mcp("googleworkspace", "get_events")
    all_policy = [read, *filler, get_events]

    controller_schema_count = 1  # just search_tools
    max_tools = controller_schema_count + 10  # 10 inventory slots available
    result = prepare_tool_exposure(
        inventory_tools=all_policy,
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=max_tools),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids={stable_tool_id(get_events)},
        # All of them are policy-visible — no hidden bucket.
        default_visible_tool_ids={stable_tool_id(tool) for tool in all_policy},
    )

    assert stable_tool_id(get_events) in result.visible_tool_ids
    assert result.debug_metadata["promoted_requested_count"] == 1
    assert result.debug_metadata["promoted_visible_count"] == 1
    # Cap-hidden policy tools are not reported as hidden_searchable — that set
    # only covers tools outside policy.  This keeps the semantics clean.
    assert stable_tool_id(get_events) not in result.hidden_searchable_tool_ids


def test_promoted_metric_exposes_cap_pressure_divergence() -> None:
    """When the slot cap drops some promoted tools, the metrics must show it."""
    read = _tool("read", source_type="executor", category="filesystem")
    filler = [_mcp("ws", f"filler_{i:03d}") for i in range(20)]
    get_events = _mcp("googleworkspace", "get_events")
    get_mail = _mcp("googleworkspace", "search_gmail_messages")
    # A tool that is not policy-visible — ensures we hit the controller-search
    # fallback path which actually enforces the slot cap.
    hidden_mcp = _mcp("hidden", "tool")
    all_tools = [read, *filler, get_events, get_mail, hidden_mcp]
    policy_visible_ids = {stable_tool_id(tool) for tool in all_tools if tool is not hidden_mcp}

    # Enough slots for both promoted tools.
    result_both_fit = prepare_tool_exposure(
        inventory_tools=all_tools,
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=20),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids={stable_tool_id(get_events), stable_tool_id(get_mail)},
        default_visible_tool_ids=policy_visible_ids,
    )
    assert (
        result_both_fit.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
    )
    assert result_both_fit.debug_metadata["promoted_requested_count"] == 2
    assert result_both_fit.debug_metadata["promoted_visible_count"] == 2

    # Tight cap — only 2 inventory slots (max_tools=3, controller=1).  Both
    # promoted tools still fit because they are prioritized over filler.
    result_under_pressure = prepare_tool_exposure(
        inventory_tools=all_tools,
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=3),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids={stable_tool_id(get_events), stable_tool_id(get_mail)},
        default_visible_tool_ids=policy_visible_ids,
    )
    assert result_under_pressure.debug_metadata["promoted_requested_count"] == 2
    # Both promoted tools survive because the packer prioritises promoted over
    # unrelated policy-visible filler tools.
    assert result_under_pressure.debug_metadata["promoted_visible_count"] == 2
    # And filler tools get dropped instead.
    for filler_tool in filler:
        assert stable_tool_id(filler_tool) not in result_under_pressure.visible_tool_ids


def test_hidden_searchable_tool_ids_excludes_visible_tools() -> None:
    """hidden_searchable_tool_ids must not overlap with visible_tool_ids."""
    get_events = _mcp("googleworkspace", "get_events")
    get_form = _mcp("googleworkspace", "get_form")
    read = _tool("read", source_type="executor", category="filesystem")

    result = prepare_tool_exposure(
        inventory_tools=[read, get_events, get_form],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(read)},
    )

    assert not result.visible_tool_ids & result.hidden_searchable_tool_ids


def test_prepare_tool_exposure_dedupes_visible_names() -> None:
    tool_a = ToolDefinition(
        name="same_name",
        description="a",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="local_mcp", server_name="srv-a", raw_tool_name="same/name"),
        category="mcp",
    )
    tool_b = ToolDefinition(
        name="same_name",
        description="b",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="srv-b", raw_tool_name="same.name"),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[tool_a, tool_b],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="claude", supports_defer_loading=True),
        contract=_contract(),
        promoted_tool_ids=set(),
    )

    visible_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert len(visible_names) == len(set(visible_names))


def test_prepare_tool_exposure_sanitizes_skill_visible_names() -> None:
    skill_tool = ToolDefinition(
        name="skill_git-release__run_release",
        description="release",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="skill", skill_id="git-release", raw_tool_name="run/release now"),
        category="skill",
    )

    result = prepare_tool_exposure(
        inventory_tools=[skill_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-4o-mini", max_tools=8),
        contract=_contract(),
        promoted_tool_ids={stable_tool_id(skill_tool)},
    )

    assert result.alias_map["run_release_now"] == "skill_git-release__run_release"


def test_prepare_tool_exposure_visible_only_chat_without_search() -> None:
    result = prepare_tool_exposure(
        inventory_tools=[
            _tool("read", source_type="executor", category="filesystem"),
            _tool("bash", source_type="executor", category="shell"),
        ],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="claude", max_tools=16),
        contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={
            stable_tool_id(_tool("read", source_type="executor", category="filesystem"))
        },
        allow_tool_search=False,
    )

    function_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert result.debug_metadata["strategy"] == "chat_visible_only"
    assert function_names == ["read"]


def test_gpt5_prefers_patch_over_exact_edit_tools() -> None:
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        _write_tool("write"),
        _write_tool("edit"),
        _write_tool("multiedit"),
        _write_tool("apply_patch"),
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES, discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        allow_tool_search=False,
    )

    function_names = [tool["function"]["name"] for tool in result.tools]
    assert "apply_patch" in function_names
    assert "write" not in function_names
    assert "edit" not in function_names
    assert "multiedit" not in function_names
    assert result.debug_metadata["edit_tool_family"] == "gpt5"
    assert result.debug_metadata["edit_tool_mode"] == "apply_patch"


def test_non_gpt5_models_prefer_exact_edit_tools() -> None:
    for model_id in [
        "claude-sonnet-4-5",
        "gemini-2.5-pro",
        "groq/llama-3.3-70b-versatile",
        "openai/gpt-oss-120b",
        "mistral-large",
    ]:
        inventory = [
            _tool("read", source_type="executor", category="filesystem"),
            _write_tool("write"),
            _write_tool("edit"),
            _write_tool("multiedit"),
            _write_tool("apply_patch"),
        ]

        result = prepare_tool_exposure(
            inventory_tools=inventory,
            controller_tool_schemas=[],
            model_info=ModelInfo(model_id=model_id, max_tools=128),
            contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
            promoted_tool_ids=set(),
            allow_tool_search=False,
        )

        function_names = [tool["function"]["name"] for tool in result.tools]
        assert "apply_patch" not in function_names
        assert {"write", "edit", "multiedit"} <= set(function_names)
        assert result.debug_metadata["edit_tool_mode"] == "exact"


def test_single_allowed_edit_surface_is_preserved_for_any_model() -> None:
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        _write_tool("edit"),
        _write_tool("write"),
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES, discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        allow_tool_search=False,
    )

    function_names = [tool["function"]["name"] for tool in result.tools]
    assert {"read", "edit", "write"} <= set(function_names)


def test_gpt5_preserves_exact_edit_tools_when_patch_is_not_default_visible() -> None:
    read = _tool("read", source_type="executor", category="filesystem")
    edit = _write_tool("edit")
    write = _write_tool("write")
    patch = _write_tool("apply_patch")

    result = prepare_tool_exposure(
        inventory_tools=[read, edit, write, patch],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES, discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={
            stable_tool_id(read),
            stable_tool_id(edit),
            stable_tool_id(write),
        },
        allow_tool_search=False,
    )

    function_names = [tool["function"]["name"] for tool in result.tools]
    assert "apply_patch" not in function_names
    assert {"edit", "write"} <= set(function_names)


def test_deferred_loading_still_uses_single_edit_surface() -> None:
    read = _tool("read", source_type="executor", category="filesystem")
    edit = _write_tool("edit")
    patch = _write_tool("apply_patch")

    result = prepare_tool_exposure(
        inventory_tools=[read, edit, patch],
        controller_tool_schemas=[],
        model_info=ModelInfo(
            model_id="claude-sonnet-4-5",
            max_tools=128,
            supports_defer_loading=True,
        ),
        contract=_contract(discovery_mode=ToolDiscoveryMode.CONTROLLER_SEARCH),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(read), stable_tool_id(patch)},
        allow_tool_search=True,
    )

    function_names = [tool["function"]["name"] for tool in result.tools]
    assert "edit" in function_names
    assert "apply_patch" not in function_names


def test_responses_native_apply_patch_replaces_function_schema() -> None:
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        _write_tool("apply_patch"),
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="gpt-5.1-codex", supports_responses_api=True, max_tools=128),
        contract=_contract(
            llm_api=LLMApiMode.RESPONSES,
            discovery_mode=ToolDiscoveryMode.NONE,
            native_apply_patch=True,
        ),
        promoted_tool_ids=set(),
        allow_tool_search=False,
    )

    assert {tool.get("type") for tool in result.tools} >= {"function", "apply_patch"}
    assert all(
        tool.get("function", {}).get("name") != "apply_patch"
        for tool in result.tools
        if isinstance(tool.get("function"), dict)
    )
    assert result.debug_metadata["native_apply_patch_exposed"] is True

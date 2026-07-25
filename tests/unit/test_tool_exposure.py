from __future__ import annotations

import hashlib

import pytest

from cognis.core.tool_exposure import (
    LLMApiMode,
    ToolDiscoveryMode,
    ToolExposureContract,
    prepare_tool_exposure,
    reverse_tool_argument_aliases,
)
from cognis.models.config import ModelInfo
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolSource, sanitize_mcp_tool_name, stable_tool_id
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
    anthropic_schema_compatible: bool = False,
    anthropic_native_tool_search: bool = False,
) -> ToolExposureContract:
    return ToolExposureContract(
        llm_api=llm_api,
        discovery_mode=discovery_mode,
        native_apply_patch=native_apply_patch,
        anthropic_defer_loading=anthropic_defer_loading,
        anthropic_schema_compatible=anthropic_schema_compatible,
        anthropic_native_tool_search=anthropic_native_tool_search,
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


def _native_anthropic_contract() -> ToolExposureContract:
    return _contract(
        discovery_mode=ToolDiscoveryMode.ANTHROPIC_NATIVE_SEARCH,
        anthropic_schema_compatible=True,
        anthropic_native_tool_search=True,
    )


def test_prepare_tool_exposure_uses_native_anthropic_search_with_full_deferred_inventory() -> None:
    hidden = _mcp("github", "search/issues")
    promoted = _mcp("slack", "search/messages")
    result = prepare_tool_exposure(
        inventory_tools=[
            _tool("read", source_type="executor", category="filesystem"),
            hidden,
            promoted,
        ],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(
            model_id="claude-sonnet-4-6",
            supports_tool_search=True,
            supports_native_tool_search=True,
            supports_defer_loading=True,
            supports_pause_turn=True,
            max_tools=4,
        ),
        contract=_native_anthropic_contract(),
        promoted_tool_ids={stable_tool_id(promoted)},
    )

    assert result.debug_metadata["strategy"] == "anthropic_native_tool_search"
    assert result.debug_metadata["native_anthropic_search_enabled"] is True
    assert [tool.get("name") for tool in result.tools[:1]] == ["tool_search_tool_bm25"]
    assert result.tools[0]["type"] == "tool_search_tool_bm25_20251119"
    functions = {tool["function"]["name"]: tool["function"] for tool in result.tools[1:]}
    assert "search_tools" not in functions
    assert functions[sanitize_mcp_tool_name("github", "search/issues")]["defer_loading"] is True
    assert (
        functions[sanitize_mcp_tool_name("slack", "search/messages")].get("defer_loading") is None
    )
    assert functions["read"].get("defer_loading") is None
    assert functions[sanitize_mcp_tool_name("slack", "search/messages")]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "5m",
    }


def test_prepare_tool_exposure_falls_back_before_native_payload_when_capability_or_limit_is_missing() -> (
    None
):
    inventory = [_tool("read"), _mcp("github", "search/issues")]
    for model_info, reason in (
        (
            ModelInfo(model_id="claude", supports_defer_loading=True),
            "model_tool_search_unsupported",
        ),
        (
            ModelInfo(
                model_id="claude",
                supports_tool_search=True,
                supports_defer_loading=True,
            ),
            "model_native_tool_search_unsupported",
        ),
        (
            ModelInfo(
                model_id="claude",
                supports_tool_search=True,
                supports_native_tool_search=True,
                supports_defer_loading=True,
                supports_pause_turn=True,
                max_tools=2,
            ),
            "provider_tool_limit",
        ),
        (
            ModelInfo(
                model_id="claude",
                supports_tool_search=True,
                supports_native_tool_search=True,
                supports_defer_loading=True,
            ),
            "model_pause_turn_unsupported",
        ),
    ):
        result = prepare_tool_exposure(
            inventory_tools=inventory,
            controller_tool_schemas=[_search_schema()],
            model_info=model_info,
            contract=_native_anthropic_contract(),
            promoted_tool_ids=set(),
        )
        assert result.debug_metadata["strategy"] == "generic_search_tools"
        assert result.debug_metadata["native_anthropic_search_reason"] == reason
        assert any(tool.get("function", {}).get("name") == "search_tools" for tool in result.tools)
        assert all(tool.get("type") != "tool_search_tool_bm25_20251119" for tool in result.tools)


def test_prepare_tool_exposure_rejects_native_server_name_collision() -> None:
    with pytest.raises(ValueError, match="collides"):
        prepare_tool_exposure(
            inventory_tools=[_tool("tool_search_tool_bm25")],
            controller_tool_schemas=[],
            model_info=ModelInfo(
                model_id="claude",
                supports_tool_search=True,
                supports_native_tool_search=True,
                supports_defer_loading=True,
                supports_pause_turn=True,
            ),
            contract=_native_anthropic_contract(),
            promoted_tool_ids=set(),
        )


def test_prepare_tool_exposure_counts_non_search_controller_tools_against_native_limit() -> None:
    controller = {
        "type": "function",
        "function": {
            "name": "controller_helper",
            "description": "controller helper",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    result = prepare_tool_exposure(
        inventory_tools=[_tool("read"), _mcp("github", "search/issues")],
        controller_tool_schemas=[_search_schema(), controller],
        model_info=ModelInfo(
            model_id="claude",
            supports_tool_search=True,
            supports_native_tool_search=True,
            supports_defer_loading=True,
            supports_pause_turn=True,
            max_tools=3,
        ),
        contract=_native_anthropic_contract(),
        promoted_tool_ids=set(),
    )

    assert result.debug_metadata["native_anthropic_search_reason"] == "provider_tool_limit"
    assert result.debug_metadata["native_anthropic_tool_count"] == 4


def test_prepare_tool_exposure_controller_search_never_uses_anthropic_deferred_loading() -> None:
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
    assert result.debug_metadata["strategy"] == "generic_search_tools"
    assert result.debug_metadata["discovery_mode"] == "controller_search"
    assert tool_names == ["read", "search_tools"]
    assert not any(tool.get("function", {}).get("defer_loading") is True for tool in result.tools)


def test_prepare_tool_exposure_strips_schema_metadata_recursively() -> None:
    inventory = [
        ToolDefinition(
            name="schema_tool",
            description="schema tool",
            parameters={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "query": {
                        "$id": "query",
                        "$comment": "drop me",
                        "type": "string",
                    }
                },
            },
            source=ToolSource(type="builtin"),
            category="system",
            read_only=True,
        )
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="gpt-5"),
        contract=_contract(),
        promoted_tool_ids=set(),
    )

    parameters = result.tools[0]["function"]["parameters"]
    assert "$schema" not in parameters
    assert "$id" not in parameters["properties"]["query"]
    assert "$comment" not in parameters["properties"]["query"]


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
    assert result.tools[-1]["function"]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


def test_prepare_tool_exposure_uses_configured_anthropic_cache_ttl() -> None:
    result = prepare_tool_exposure(
        inventory_tools=[_tool("bash"), _tool("read")],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="claude-sonnet-4-5", max_tools=2),
        contract=_contract(),
        promoted_tool_ids=set(),
        anthropic_cache_ttl="1h",
    )

    assert result.tools[-1]["function"]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


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


def test_prepare_tool_exposure_uses_generic_search_for_anthropic_proxy_without_defer_loading() -> (
    None
):
    mcp_tool = _mcp("github", "search/issues")
    result = prepare_tool_exposure(
        inventory_tools=[_tool("skill_load"), _tool("read", source_type="executor"), mcp_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="local-meridian-alias", supports_defer_loading=True),
        contract=_contract(
            anthropic_defer_loading=False,
            anthropic_schema_compatible=True,
        ),
        promoted_tool_ids=set(),
    )

    tool_names = [tool["function"]["name"] for tool in result.tools if tool["type"] == "function"]
    assert result.debug_metadata["strategy"] == "generic_search_tools"
    assert "search_tools" in tool_names
    assert "skill_load" in tool_names
    assert "read" in tool_names
    assert sanitize_mcp_tool_name("github", "search/issues") not in tool_names
    assert not any(tool.get("function", {}).get("defer_loading") is True for tool in result.tools)


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


def test_prepare_tool_exposure_appends_promoted_tools_after_stable_sorted_block() -> None:
    promoted = _tool("aaa_promoted", category="utility")
    bash = _tool("bash", source_type="executor", category="shell")
    read = _tool("read", source_type="executor", category="filesystem")

    result = prepare_tool_exposure(
        inventory_tools=[promoted, read, bash],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES, discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids={stable_tool_id(promoted)},
        default_visible_tool_ids={stable_tool_id(bash), stable_tool_id(read)},
        allow_tool_search=False,
    )

    function_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert function_names == ["bash", "read", "aaa_promoted"]


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


def test_anthropic_tool_exposure_aliases_invalid_argument_property_names() -> None:
    long_name = "segment." * 12
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("mfg-portal", "mimir.series"),
        description="mimir series",
        parameters={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "match[]": {"type": "array", "items": {"type": "string"}},
                "$top": {"type": "integer"},
                "@microsoft.graph.conflictBehavior": {"type": "string"},
                long_name: {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {
                        "settings[include_tax]": {"type": "boolean"},
                    },
                    "required": ["settings[include_tax]"],
                },
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"child[]": {"type": "string"}},
                        "required": ["child[]"],
                    },
                },
            },
            "required": ["match[]", "$top", "@microsoft.graph.conflictBehavior", long_name],
        },
        source=ToolSource(
            type="intaris_mcp",
            server_name="mfg-portal",
            raw_tool_name="mimir.series",
        ),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[mcp_tool],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="claude-sonnet-4-6", max_tools=128),
        contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(mcp_tool)},
        allow_tool_search=False,
    )

    schema = next(tool for tool in result.tools if tool["type"] == "function")
    parameters = schema["function"]["parameters"]
    properties = parameters["properties"]

    assert "$schema" not in parameters
    assert "match" in properties
    assert "top" in properties
    assert "microsoft.graph.conflictBehavior" in properties
    assert all("[" not in key and "]" not in key and "$" not in key for key in properties)
    assert all(len(key) <= 64 for key in properties)
    assert parameters["required"][:3] == [
        "match",
        "top",
        "microsoft.graph.conflictBehavior",
    ]
    assert properties["nested"]["required"] == ["settings_include_tax"]
    assert properties["items"]["items"]["required"] == ["child"]
    assert result.argument_alias_map[mcp_tool.name]["match"]["original"] == "match[]"


def test_anthropic_schema_compatible_contract_aliases_arbitrary_model_ids() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("mfg-portal", "mimir.series"),
        description="mimir series",
        parameters={
            "type": "object",
            "properties": {"match[]": {"type": "array", "items": {"type": "string"}}},
            "required": ["match[]"],
        },
        source=ToolSource(
            type="intaris_mcp",
            server_name="mfg-portal",
            raw_tool_name="mimir.series",
        ),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[mcp_tool],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="local-meridian-alias", max_tools=128),
        contract=_contract(
            discovery_mode=ToolDiscoveryMode.NONE,
            anthropic_schema_compatible=True,
        ),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(mcp_tool)},
        allow_tool_search=False,
    )

    schema = next(tool for tool in result.tools if tool["type"] == "function")

    assert schema["function"]["parameters"]["properties"].keys() == {"match"}
    assert result.argument_alias_map[mcp_tool.name]["match"]["original"] == "match[]"


def test_anthropic_argument_alias_dedup_handles_generated_alias_collision() -> None:
    generated_alias = f"foo__{hashlib.sha1(b'foo[]').hexdigest()[:8]}"
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("server", "tool"),
        description="tool",
        parameters={
            "type": "object",
            "properties": {
                "foo": {"type": "string"},
                generated_alias: {"type": "string"},
                "foo[]": {"type": "string"},
            },
        },
        source=ToolSource(type="intaris_mcp", server_name="server", raw_tool_name="tool"),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[mcp_tool],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="claude-sonnet-4-6", max_tools=128),
        contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(mcp_tool)},
        allow_tool_search=False,
    )

    schema = next(tool for tool in result.tools if tool["type"] == "function")
    properties = schema["function"]["parameters"]["properties"]

    assert len(properties) == 3
    assert "foo" in properties
    assert generated_alias in properties
    assert any(key.startswith("foo__") and key != generated_alias for key in properties)


def test_reverse_tool_argument_aliases_restores_canonical_mcp_arguments() -> None:
    alias_tree = {
        "match": {"original": "match[]", "properties": {}},
        "top": {"original": "$top", "properties": {}},
        "nested": {
            "original": "nested",
            "properties": {
                "settings_include_tax": {
                    "original": "settings[include_tax]",
                    "properties": {},
                }
            },
        },
        "items": {
            "original": "items",
            "properties": {
                "child": {
                    "original": "child[]",
                    "properties": {},
                }
            },
        },
    }

    assert reverse_tool_argument_aliases(
        {
            "match": ["up"],
            "top": 10,
            "nested": {"settings_include_tax": True},
            "items": [{"child": "a"}, {"child": "b"}],
            "unchanged": "value",
        },
        alias_tree,
    ) == {
        "match[]": ["up"],
        "$top": 10,
        "nested": {"settings[include_tax]": True},
        "items": [{"child[]": "a"}, {"child[]": "b"}],
        "unchanged": "value",
    }


def test_anthropic_argument_aliases_follow_local_schema_refs() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("mfg-portal", "mimir.labels"),
        description="mimir labels",
        parameters={
            "type": "object",
            "properties": {
                "filter": {"$ref": "#/$defs/Filter"},
            },
            "$defs": {
                "Filter": {
                    "type": "object",
                    "properties": {"match[]": {"type": "array", "items": {"type": "string"}}},
                    "required": ["match[]"],
                }
            },
        },
        source=ToolSource(
            type="intaris_mcp",
            server_name="mfg-portal",
            raw_tool_name="mimir.labels",
        ),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[mcp_tool],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="claude-sonnet-4-6", max_tools=128),
        contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(mcp_tool)},
        allow_tool_search=False,
    )

    schema = next(tool for tool in result.tools if tool["type"] == "function")
    filter_def = schema["function"]["parameters"]["$defs"]["Filter"]

    assert filter_def["properties"].keys() == {"match"}
    assert filter_def["required"] == ["match"]
    assert reverse_tool_argument_aliases(
        {"filter": {"match": ["up"]}},
        result.argument_alias_map[mcp_tool.name],
    ) == {"filter": {"match[]": ["up"]}}


def test_anthropic_argument_aliases_handle_recursive_local_schema_refs() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("recursive", "walk"),
        description="walk a recursive tree",
        parameters={
            "type": "object",
            "properties": {
                "root": {"$ref": "#/$defs/Node"},
            },
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "child[]": {
                            "anyOf": [
                                {"$ref": "#/$defs/Node"},
                                {"type": "null"},
                            ]
                        },
                        "value": {"type": "string"},
                    },
                }
            },
        },
        source=ToolSource(
            type="intaris_mcp",
            server_name="recursive",
            raw_tool_name="walk",
        ),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[mcp_tool],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="claude-opus-4-8", max_tools=128),
        contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(mcp_tool)},
        allow_tool_search=False,
    )

    schema = next(tool for tool in result.tools if tool["type"] == "function")
    node = schema["function"]["parameters"]["$defs"]["Node"]
    assert node["properties"].keys() == {"child", "value"}
    assert node["properties"]["child"]["anyOf"][0] == {"$ref": "#/$defs/Node"}
    assert result.argument_alias_map[mcp_tool.name]["root"]["properties"]["child"] == {
        "original": "child[]",
        "properties": {"$cognis_ref": "#/$defs/Node"},
    }
    assert reverse_tool_argument_aliases(
        {
            "root": {
                "child": {
                    "child": {
                        "child": None,
                        "value": "leaf",
                    },
                    "value": "middle",
                },
                "value": "root",
            }
        },
        result.argument_alias_map[mcp_tool.name],
    ) == {
        "root": {
            "child[]": {
                "child[]": {
                    "child[]": None,
                    "value": "leaf",
                },
                "value": "middle",
            },
            "value": "root",
        }
    }


def test_anthropic_argument_aliases_handle_mutual_and_shared_local_schema_refs() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("recursive", "mutual"),
        description="walk mutually recursive nodes",
        parameters={
            "type": "object",
            "properties": {
                "first": {"$ref": "#/$defs/A"},
                "second": {"$ref": "#/$defs/A"},
            },
            "$defs": {
                "A": {
                    "type": "object",
                    "properties": {"next[]": {"$ref": "#/$defs/B"}},
                },
                "B": {
                    "type": "object",
                    "properties": {"previous[]": {"$ref": "#/$defs/A"}},
                },
            },
        },
        source=ToolSource(
            type="intaris_mcp",
            server_name="recursive",
            raw_tool_name="mutual",
        ),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[mcp_tool],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="claude-opus-4-8", max_tools=128),
        contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(mcp_tool)},
        allow_tool_search=False,
    )

    assert reverse_tool_argument_aliases(
        {
            "first": {"next": {"previous": {"next": None}}},
            "second": {"next": None},
        },
        result.argument_alias_map[mcp_tool.name],
    ) == {
        "first": {"next[]": {"previous[]": {"next[]": None}}},
        "second": {"next[]": None},
    }


def test_anthropic_argument_alias_refs_do_not_leak_occurrence_aliases() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("recursive", "occurrences"),
        description="reuse a ref with occurrence-specific aliases",
        parameters={
            "type": "object",
            "properties": {
                "left": {
                    "$ref": "#/$defs/Base",
                    "properties": {
                        "nested": {
                            "type": "object",
                            "properties": {"left[]": {"type": "string"}},
                        }
                    },
                },
                "right": {
                    "$ref": "#/$defs/Base",
                    "properties": {
                        "nested": {
                            "type": "object",
                            "properties": {"right[]": {"type": "string"}},
                        }
                    },
                },
            },
            "$defs": {
                "Base": {
                    "type": "object",
                    "properties": {
                        "nested": {
                            "type": "object",
                            "properties": {"base[]": {"type": "string"}},
                        }
                    },
                }
            },
        },
        source=ToolSource(
            type="intaris_mcp",
            server_name="recursive",
            raw_tool_name="occurrences",
        ),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[mcp_tool],
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id="claude-opus-4-8", max_tools=128),
        contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(mcp_tool)},
        allow_tool_search=False,
    )

    assert reverse_tool_argument_aliases(
        {
            "left": {"nested": {"base": "b", "left": "l", "right": "literal"}},
            "right": {"nested": {"base": "b", "left": "literal", "right": "r"}},
        },
        result.argument_alias_map[mcp_tool.name],
    ) == {
        "left": {"nested": {"base[]": "b", "left[]": "l", "right": "literal"}},
        "right": {"nested": {"base[]": "b", "left": "literal", "right[]": "r"}},
    }


def test_reverse_tool_argument_aliases_handles_additional_property_values() -> None:
    alias_tree = {
        "*": {
            "original": "*",
            "properties": {
                "child": {
                    "original": "child[]",
                    "properties": {},
                }
            },
        }
    }

    assert reverse_tool_argument_aliases(
        {"first": {"child": "a"}, "second": {"child": "b"}},
        alias_tree,
    ) == {"first": {"child[]": "a"}, "second": {"child[]": "b"}}


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


def test_controller_search_preserves_currently_visible_edit_surface() -> None:
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
    assert "apply_patch" in function_names
    assert "edit" not in function_names


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

"""Controller authorization through direct-chat Responses discovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.agent_loop import (
    CHAT_POLICY,
    AgentLoop,
    StepContext,
    _controller_builtin_enabled,
    _filter_model_inventory_tools,
    _tool_visible_by_default,
)
from cognis.core.step_profiles import resolve_step_profile, step_profile_allows_tool
from cognis.core.tool_exposure import (
    LLMApiMode,
    ToolDiscoveryMode,
    ToolExposureContract,
    prepare_tool_exposure,
)
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.config import ModelInfo
from cognis.models.tool import NativeToolDefinition, Permission, ToolSource, stable_tool_id
from cognis.models.workflow import StepDefinition
from cognis.providers.llm.anthropic.tool_bundle import compile_anthropic_tool_bundle
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL, search_inventory
from cognis.tools.builtin.workflow import workflow_tools


def _agent(
    tools: dict[str, Any] | None, permissions: AgentPermissions | None = None
) -> AgentDefinition:
    return AgentDefinition(
        agent_id="dobby",
        owner_email="filip@pytloun.cz",
        name="Dobby",
        tools=tools,
        permissions=permissions,
    )


@pytest.mark.parametrize("identifier", ["search_tools", "builtin:search_tools", "*"])
def test_controller_gate_accepts_authorized_identifiers(identifier: str) -> None:
    assert _controller_builtin_enabled(_agent({"builtin_tools": [identifier]}), SEARCH_TOOLS_TOOL)


@pytest.mark.parametrize(
    "tools,permissions",
    [
        ({"builtin_tools": []}, None),
        ({"builtin_tools": ["builtin:read"]}, None),
        ({"builtin_tools": ["*"], "disabled_tools": ["builtin:search_tools"]}, None),
        ({"builtin_tools": ["*"], "disabled_tools": ["search_tools"]}, None),
        ({"builtin_tools": ["*"], "disabled_categories": [SEARCH_TOOLS_TOOL.category]}, None),
        ({"builtin_tools": ["*"], "deny_tools": ["builtin:search_tools"]}, None),
        ({"builtin_tools": ["*"], "deny_tools": ["search_tools"]}, None),
        (
            {"builtin_tools": ["builtin:search_tools"]},
            AgentPermissions(tool_permissions={"builtin:search_tools": Permission.DENY}),
        ),
        (None, AgentPermissions(tool_permissions={"*": Permission.DENY})),
    ],
)
def test_controller_gate_deny_wins(
    tools: dict[str, Any] | None, permissions: AgentPermissions | None
) -> None:
    assert not _controller_builtin_enabled(_agent(tools, permissions), SEARCH_TOOLS_TOOL)


@pytest.mark.parametrize("restriction", ["none", "bash_deny", "search_deny", "hard", "no_search"])
def test_controller_direct_responses_discovery_and_promotion(restriction: str) -> None:
    agent = _agent(
        {"builtin_tools": ["builtin:search_tools", "builtin:describe_tool"]},
        AgentPermissions(
            tool_permissions={
                "*": Permission.EVALUATE,
                **({"builtin:bash": Permission.DENY} if restriction == "bash_deny" else {}),
            }
        ),
    )
    if restriction == "search_deny":
        agent.tools["deny_tools"] = ["builtin:search_tools"]
    step = StepDefinition(
        name="direct",
        type="run",
        prompt="",
        step_profile_id="system:direct-default",
        **({"step_profile_mode": "hard"} if restriction == "hard" else {}),
        **({"step_profile": {"allow_tool_search": False}} if restriction == "no_search" else {}),
    )
    ctx = StepContext(
        step_definition=step,
        session=SimpleNamespace(session_id="discovery-test", intaris_session_id="discovery-test"),
        conversation=None,
        agent=agent,
        policy=CHAT_POLICY,
    )
    loop = object.__new__(AgentLoop)
    schemas = loop._build_controller_tool_schemas(ctx)
    profile = resolve_step_profile(step)
    bash = NativeToolDefinition(
        name="bash",
        description="Execute a shell command",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        source=ToolSource(type="executor"),
        category="shell",
        profile_group="shell",
        read_only=False,
    )
    inventory = [
        tool
        for tool in _filter_model_inventory_tools(agent, [bash])
        if step_profile_allows_tool(tool, profile)
    ]
    defaults = {
        stable_tool_id(tool)
        for tool in inventory
        if _tool_visible_by_default(tool, profile, activated_tool_ids=set())
    }
    assert stable_tool_id(bash) not in defaults

    def expose(promoted: set[str]) -> Any:
        return prepare_tool_exposure(
            inventory_tools=inventory,
            controller_tool_schemas=schemas,
            model_info=ModelInfo(model_id="gpt-6.1-sol", max_tools=128),
            contract=ToolExposureContract(
                llm_api=LLMApiMode.RESPONSES,
                discovery_mode=ToolDiscoveryMode.CONTROLLER_SEARCH,
            ),
            promoted_tool_ids=promoted,
            default_visible_tool_ids=defaults,
            allow_tool_search=profile.config.allow_tool_search,
        )

    initial = expose(set())
    names = {schema.get("function", {}).get("name") for schema in initial.tools}
    assert "bash" not in names
    if restriction in {"bash_deny", "hard"}:
        assert inventory == []
        assert search_inventory(inventory, "bash") == []
        assert stable_tool_id(bash) not in expose({stable_tool_id(bash)}).visible_tool_ids
    elif restriction in {"search_deny", "no_search"}:
        assert "search_tools" not in names
        assert initial.debug_metadata["strategy"] == "openai_responses_visible_only"
    else:
        assert "search_tools" in names
        assert initial.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
        matches = search_inventory(inventory, "bash")
        assert [match["tool_id"] for match in matches] == [stable_tool_id(bash)]
        promoted = expose({match["tool_id"] for match in matches})
        assert stable_tool_id(bash) in promoted.visible_tool_ids
        assert "bash" in {schema.get("function", {}).get("name") for schema in promoted.tools}


def test_task_step_registry_question_tools_do_not_duplicate_controller_schemas() -> None:
    """Registry copies of controller question tools must not reach the native bundle twice."""
    agent = _agent({"builtin_tools": ["*"]})
    step = StepDefinition(name="ingest", type="run", prompt="")
    ctx = StepContext(
        step_definition=step,
        session=SimpleNamespace(session_id="collision-test", intaris_session_id="collision-test"),
        conversation=None,
        agent=agent,
        policy=CHAT_POLICY,
    )
    loop = object.__new__(AgentLoop)
    controller = loop._build_controller_tool_exposure(ctx)
    registry_tools = workflow_tools()
    inventory = _filter_model_inventory_tools(agent, registry_tools)
    exposure = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=controller.schemas,
        model_info=ModelInfo(model_id="claude-opus-5-5", max_tools=128),
        contract=ToolExposureContract(
            llm_api=LLMApiMode.CHAT_COMPLETIONS,
            discovery_mode=ToolDiscoveryMode.CONTROLLER_SEARCH,
            anthropic_schema_compatible=True,
        ),
        promoted_tool_ids={stable_tool_id(tool) for tool in registry_tools},
        default_visible_tool_ids={stable_tool_id(tool) for tool in registry_tools},
    )
    alias_map = {**exposure.alias_map, **controller.alias_map}
    stable_ids = {
        name: f"builtin:{alias_map.get(name, name)}"
        for schema in exposure.tools
        if isinstance(name := schema.get("function", {}).get("name"), str)
    }

    bundle = compile_anthropic_tool_bundle(
        exposure.tools,
        alias_map=alias_map,
        stable_id_map=stable_ids,
        argument_alias_map=dict(exposure.argument_alias_map),
    )

    canonical = [binding.canonical_name for binding in bundle.bindings]
    assert canonical.count("answer_question") == 1
    assert canonical.count("forward_question") == 1

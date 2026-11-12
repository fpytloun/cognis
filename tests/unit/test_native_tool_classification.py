from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cognis.api.runtime_support import static_tool_definitions
from cognis.core.step_profiles import resolve_step_profile, step_profile_allows_tool
from cognis.core.tool_exposure import (
    LLMApiMode,
    ToolDiscoveryMode,
    ToolExposureContract,
    filter_edit_tools_for_model,
    prepare_tool_exposure,
)
from cognis.executor.tool_definitions_runtime import executor_tool_definitions
from cognis.models.config import ModelInfo
from cognis.models.tool import (
    NativeToolDefinition,
    ToolCapability,
    ToolDefinition,
    ToolMutationKind,
    ToolSource,
    native_tool_classification_error,
    stable_tool_id,
)
from cognis.models.workflow import StepDefinition
from cognis.tools import classification
from cognis.tools.executor.browser.definitions import browser_tool_definitions
from cognis.tools.executor.definitions import (
    APPLY_PATCH_TOOL,
    EDIT_TOOL,
    MULTIEDIT_TOOL,
    WRITE_TOOL,
)
from cognis.tools.executor.document import ARTIFACT_PUBLISH_TOOL, DOCUMENT_GENERATE_TOOL
from cognis.tools.executor.officecli.definitions import office_tool_definitions
from cognis.tools.executor.web.definitions import web_tool_definitions
from cognis.tools.introspection import audit_tool_descriptors, describe_available_tool
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _all_native_tools() -> list[ToolDefinition]:
    return [
        *static_tool_definitions(knowledgebase_enabled=True),
        *executor_tool_definitions(),
        *browser_tool_definitions(),
        ARTIFACT_PUBLISH_TOOL,
        DOCUMENT_GENERATE_TOOL,
        *office_tool_definitions(),
        *web_tool_definitions(["direct", "tavily", "browser", "brave", "searxng"]),
    ]


def test_every_native_tool_has_authoritative_classification_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_heuristics(_tool: ToolDefinition) -> None:
        pytest.fail("native tool reached a text classification heuristic")

    monkeypatch.setattr(classification, "_heuristic_profile_group", no_heuristics)
    monkeypatch.setattr(classification, "_heuristic_capabilities", no_heuristics)
    tools = _all_native_tools()
    assert audit_tool_descriptors(tools) == []
    for tool in tools:
        assert native_tool_classification_error(tool) is None, tool.name
        assert classification.classify_tool_definition(tool) is tool
        restored = ToolDefinition.model_validate(tool.model_dump(mode="json"))
        assert native_tool_classification_error(restored) is None, tool.name
        assert restored.capabilities == tool.capabilities
        assert restored.profile_group == tool.profile_group


@pytest.mark.parametrize("source_type", ["builtin", "controller", "executor"])
@pytest.mark.parametrize("read_only", [True, False])
def test_native_classification_uses_structured_metadata_not_names_or_descriptions(
    source_type: str, read_only: bool
) -> None:
    tool = NativeToolDefinition(
        name="remove_project_source",
        description="Delete, purge, reset, execute shell commands; unsupported operations are rejected.",
        source=ToolSource(type=source_type),
        category="filesystem",
        read_only=read_only,
    )
    assert tool.capabilities == [ToolCapability.READ if read_only else ToolCapability.WRITE]
    assert tool.profile_group == "filesystem"
    assert classification.classify_tool_definition(tool) is tool


@pytest.mark.parametrize(
    "category,group",
    [
        ("channels", "development"),
        ("document", "development"),
        ("image", "development"),
        ("office", "office"),
        ("communication", "communication"),
    ],
)
def test_native_category_mapping_is_shared_with_tool_contract(category: str, group: str) -> None:
    tool = NativeToolDefinition(
        name="list_channel_accounts",
        description="Inspect available resources.",
        source=ToolSource(type="builtin"),
        category=category,
        read_only=True,
    )
    assert tool.profile_group == group


def test_delete_operation_declares_destructive_without_text_inference() -> None:
    tool = NativeToolDefinition(
        name="remove_project_source",
        description="Change a project source.",
        source=ToolSource(type="builtin"),
        category="development",
        mutation_kind=ToolMutationKind.DELETE,
        read_only=False,
    )
    assert tool.capabilities == [ToolCapability.WRITE, ToolCapability.DESTRUCTIVE]


@pytest.mark.parametrize(
    "updates",
    [
        {"capabilities": []},
        {"profile_group": None},
        {"profile_group": "unregistered-group"},
        {"classification_source": "heuristic"},
        {"classification_status": "pending"},
        {"read_only": True},
    ],
)
def test_incomplete_or_inconsistent_native_metadata_fails_at_both_boundaries(
    updates: dict[str, object],
) -> None:
    tool = WRITE_TOOL.model_copy(update=updates)
    with pytest.raises(ValueError, match="native classification|native tool"):
        classification.classify_tool_definition(tool)
    with pytest.raises(ValueError, match="native classification"):
        ToolRegistry().register(RegisteredTool(tool))


def test_query_usage_declares_read_only_metadata_and_canonical_examples() -> None:
    from cognis.core.usage.query_models import UsageToolArguments
    from cognis.models.tool import ToolMutationKind, tool_input_schema
    from cognis.tools.builtin.workflow import QUERY_USAGE_TOOL
    from cognis.tools.introspection import audit_tool_registration, validate_available_tool_call

    tool = QUERY_USAGE_TOOL
    assert tool.read_only and tool.capabilities == [ToolCapability.READ]
    assert tool.profile_group == "system" and tool.category == "usage"
    assert native_tool_classification_error(tool) is None
    assert tool_input_schema(tool) == UsageToolArguments.model_json_schema()
    assert audit_tool_registration(tool) == []
    assert tool.native_operations
    for operation in tool.native_operations:
        assert operation.mutation_kind is ToolMutationKind.READ
        assert operation.examples
        for example in operation.examples:
            parsed = UsageToolArguments.model_validate(example)
            assert parsed.request.operation == "query"
            assert parsed.request.query.scope.mode == "personal"
            result = validate_available_tool_call([tool], tool.name, example)
            assert result["valid"], result


def test_query_usage_null_example_is_still_rejected_by_registry() -> None:
    from cognis.core.usage.query_models import UsageToolArguments

    tool = NativeToolDefinition(
        name="query_usage",
        description="Read authorized usage accounting.",
        parameters=UsageToolArguments.model_json_schema(),
        read_only=True,
        category="usage",
        profile_group="system",
        source=ToolSource(type="builtin"),
        examples=[{"request": None}],
    )
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="Invalid native tool contract"):
        registry.register(RegisteredTool(definition=tool))
    assert registry.get("query_usage") is None


def test_new_native_category_requires_an_explicit_group_before_registration() -> None:
    tool = NativeToolDefinition(
        name="list_channel_accounts",
        description="Inspect resources.",
        source=ToolSource(type="builtin"),
        category="unregistered-category",
        read_only=True,
    )
    with pytest.raises(ValueError, match="native classification"):
        ToolRegistry().register(RegisteredTool(tool))
    declared = tool.model_copy(update={"profile_group": "communication"})
    ToolRegistry().register(RegisteredTool(declared))


@pytest.mark.parametrize("source_type", ["intaris_mcp", "local_mcp", "skill"])
def test_external_tools_keep_dynamic_classification(source_type: str) -> None:
    tool = ToolDefinition(
        name="delete_workflow",
        description="Delete a workflow.",
        source=ToolSource(type=source_type),
        category="mcp" if source_type != "skill" else "skill",
        read_only=False,
    )
    assert tool.classification_source is None
    classified = classification.classify_tool_definition(tool)
    assert classified.classification_source == "heuristic"
    assert classified.capabilities == [ToolCapability.DESTRUCTIVE]
    assert classification.requires_background_classification(classified)


async def test_native_inventory_never_requests_database_llm_or_queue() -> None:
    factory = AsyncMock(side_effect=AssertionError("native classification accessed the database"))
    queue = AsyncMock()
    tools = _all_native_tools()
    resolved = await classification.resolve_tool_classifications(
        tools, session_factory=factory, owner_email=None, queue=queue
    )
    assert resolved == tools
    factory.assert_not_called()
    queue.enqueue_tools.assert_not_called()


@pytest.mark.parametrize("model_id", ["gpt-6.1-sol", "claude-sonnet-4-5"])
def test_hard_general_task_has_a_discoverable_describable_authorized_editor(model_id: str) -> None:
    profile = resolve_step_profile(
        StepDefinition(
            name="operate",
            type="run",
            step_profile_id="system:general-task",
            step_profile_mode="hard",
        )
    )
    tools = classification.classify_tool_definitions_sync(
        [APPLY_PATCH_TOOL, WRITE_TOOL, EDIT_TOOL, MULTIEDIT_TOOL]
    )
    authorized = [tool for tool in tools if step_profile_allows_tool(tool, profile)]
    assert APPLY_PATCH_TOOL in authorized
    inventory = filter_edit_tools_for_model(authorized, model_id)
    exposure = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[],
        model_info=ModelInfo(model_id=model_id, max_tools=128),
        contract=ToolExposureContract(
            llm_api=LLMApiMode.CHAT_COMPLETIONS,
            discovery_mode=ToolDiscoveryMode.CONTROLLER_SEARCH,
        ),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(tool) for tool in inventory},
        allow_tool_search=True,
    )
    assert exposure.tools
    editor = APPLY_PATCH_TOOL if model_id.startswith("gpt") else WRITE_TOOL
    assert describe_available_tool(inventory, stable_tool_id(editor))["valid"] is True
    if model_id.startswith("gpt"):
        assert inventory == [APPLY_PATCH_TOOL]
        assert editor.capabilities == [ToolCapability.WRITE, ToolCapability.DESTRUCTIVE]


def test_model_editor_preference_never_resurrects_tools_denied_by_read_only_step() -> None:
    profile = resolve_step_profile(
        StepDefinition(
            name="review", type="run", step_profile_id="system:review", step_profile_mode="hard"
        )
    )
    authorized = [
        tool
        for tool in [APPLY_PATCH_TOOL, WRITE_TOOL, EDIT_TOOL, MULTIEDIT_TOOL]
        if step_profile_allows_tool(tool, profile)
    ]
    assert filter_edit_tools_for_model(authorized, "gpt-6.1-sol") == []


def test_legacy_executor_payload_is_normalized_without_reclassifying_description() -> None:
    payload = WRITE_TOOL.model_dump(mode="json")
    for field in (
        "profile_group",
        "capabilities",
        "classification_status",
        "classification_source",
        "classification_confidence",
    ):
        payload.pop(field)
    payload["description"] = "Delete is unsupported. Use read before writing."
    restored = ToolDefinition.model_validate(payload)
    assert restored.capabilities == [ToolCapability.WRITE]
    assert restored.profile_group == "filesystem"
    assert classification.classify_tool_definition(restored) is restored

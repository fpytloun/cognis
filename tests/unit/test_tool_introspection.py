from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from jsonschema import Draft7Validator

from cognis.api.runtime_support import static_tool_definitions
from cognis.core.agent_loop import AgentLoop
from cognis.core.orchestration_targets import (
    OrchestrationTarget,
    OrchestrationTargetSnapshot,
)
from cognis.models.deliverable import (
    PULSE_DAILY_SKELETON,
    SUPPORTED_RICH_BLOCK_TYPES,
    RichPayloadValidationError,
    normalize_required_rich_payload,
)
from cognis.models.tool import (
    NativeToolDefinition as ToolDefinition,
)
from cognis.models.tool import (
    NativeToolOperation,
    ToolDynamicOption,
    ToolMutationKind,
    ToolSource,
    declared_default_semantics,
    derive_native_input_schema,
    stable_tool_id,
    tool_input_schema,
    tool_with_input_schema,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL
from cognis.tools.builtin.image import IMAGE_EDIT_TOOL, IMAGE_GENERATE_TOOL
from cognis.tools.builtin.orchestration import (
    AGENT_CONVERSATION_CREATE_TOOL,
    DELEGATE_TOOL,
    enrich_orchestration_target_catalog,
)
from cognis.tools.builtin.schedule import MANAGE_SCHEDULES_TOOL
from cognis.tools.builtin.workflow import WRITE_DELIVERABLE_TOOL
from cognis.tools.introspection import (
    audit_tool_descriptors,
    describe_available_tool,
    resolve_descriptor_dynamic_options,
    validate_available_tool_call,
    validate_available_tool_call_with_context,
)
from cognis.tools.mcp import mcp_tools_to_definitions


def test_all_native_tools_have_consistent_descriptors_and_examples() -> None:
    tools = static_tool_definitions(knowledgebase_enabled=True)

    assert len({tool.name for tool in tools}) == len(tools)
    assert audit_tool_descriptors(tools) == []
    for tool in tools:
        assert tool.descriptor is not None
        assert tool.descriptor.authority == "native"
        assert tool.descriptor.schema_version == "cognis.tool.v2"
        assert tool.descriptor.schema_hash.startswith("sha256:")
        assert tool_input_schema(tool) == tool.parameters


def _operation_with_definition(
    operation: str,
    definition_name: str,
    definition: dict[str, object],
) -> NativeToolOperation:
    schema = {
        "type": "object",
        "definitions": {definition_name: definition},
        "properties": {
            "action": {"const": operation},
            "payload": {"$ref": f"#/definitions/{definition_name}"},
        },
        "required": ["action", "payload"],
    }
    return NativeToolOperation(
        operation=operation,
        summary=operation,
        mutation_kind=ToolMutationKind.CREATE,
        input_schema=schema,
        semantics=declared_default_semantics(ToolMutationKind.CREATE),
    )


def test_native_schema_derivation_hoists_operation_definitions_for_refs() -> None:
    first = _operation_with_definition("first", "firstPayload", {"type": "string"})
    second = _operation_with_definition("second", "secondPayload", {"type": "integer"})

    schema = derive_native_input_schema([first, second])
    validator = Draft7Validator(schema)

    assert set(schema["definitions"]) == {"firstPayload", "secondPayload"}
    assert list(validator.iter_errors({"action": "first", "payload": "ok"})) == []
    assert list(validator.iter_errors({"action": "second", "payload": 2})) == []


def test_native_schema_derivation_rejects_conflicting_definitions() -> None:
    first = _operation_with_definition("first", "payload", {"type": "string"})
    second = _operation_with_definition("second", "payload", {"type": "integer"})

    with pytest.raises(ValueError, match="conflicting schema definition 'payload'"):
        derive_native_input_schema([first, second])


def test_describe_tool_resolves_every_available_static_tool() -> None:
    tools = static_tool_definitions(knowledgebase_enabled=True)

    for tool in tools:
        result = describe_available_tool(tools, stable_tool_id(tool))
        assert result["valid"] is True, tool.name
        assert result["callable_name"] == tool.name
        assert result["descriptor"]["schema_hash"] == tool.descriptor.schema_hash


def test_introspection_cannot_reveal_tool_absent_from_filtered_inventory() -> None:
    allowed = ToolDefinition(
        name="allowed_read",
        description="Read allowed state.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="executor"),
        read_only=True,
    )
    denied = ToolDefinition(
        name="denied_write",
        description="Mutate denied state.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="executor"),
    )

    assert describe_available_tool([allowed], stable_tool_id(denied))["error"] == (
        "tool_not_available"
    )
    assert validate_available_tool_call([allowed], denied.name, {})["error"] == (
        "tool_not_available"
    )


def test_validate_tool_call_uses_authoritative_operation_schema() -> None:
    invalid = validate_available_tool_call(
        [MANAGE_SCHEDULES_TOOL],
        MANAGE_SCHEDULES_TOOL.name,
        {"action": "removed-operation"},
    )
    valid = validate_available_tool_call(
        [MANAGE_SCHEDULES_TOOL],
        MANAGE_SCHEDULES_TOOL.name,
        {"action": "list"},
    )

    assert invalid["valid"] is False
    assert invalid["schema_hash"] == MANAGE_SCHEDULES_TOOL.descriptor.schema_hash
    assert valid["valid"] is True
    assert valid["operation"] == "list"


def test_validate_schedule_create_enforces_handler_required_fields() -> None:
    invalid = validate_available_tool_call(
        [MANAGE_SCHEDULES_TOOL],
        MANAGE_SCHEDULES_TOOL.name,
        {"action": "create"},
    )
    valid = validate_available_tool_call(
        [MANAGE_SCHEDULES_TOOL],
        MANAGE_SCHEDULES_TOOL.name,
        {
            "action": "create",
            "name": "Daily check",
            "schedule_type": "cron",
            "cron_expr": "0 8 * * *",
        },
    )

    assert invalid["valid"] is False
    assert valid["valid"] is True


def test_validate_schedule_create_defaults_to_cron_requirement() -> None:
    result = validate_available_tool_call(
        [MANAGE_SCHEDULES_TOOL],
        MANAGE_SCHEDULES_TOOL.name,
        {"action": "create", "name": "Daily check"},
    )

    assert result["valid"] is False
    assert any("cron_expr" in error for error in result["errors"])


def test_validate_agent_mutations_reject_empty_updates() -> None:
    settings = validate_available_tool_call(
        [MANAGE_AGENTS_TOOL],
        MANAGE_AGENTS_TOOL.name,
        {"action": "settings_update", "agent_id": "agent-id", "settings": {}},
    )
    update = validate_available_tool_call(
        [MANAGE_AGENTS_TOOL],
        MANAGE_AGENTS_TOOL.name,
        {"action": "update", "agent_id": "agent-id"},
    )
    false_avatar = validate_available_tool_call(
        [MANAGE_AGENTS_TOOL],
        MANAGE_AGENTS_TOOL.name,
        {
            "action": "update",
            "agent_id": "agent-id",
            "generate_avatar": False,
        },
    )

    assert settings["valid"] is False
    assert update["valid"] is False
    assert false_avatar["valid"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "action": "settings_update",
            "agent_id": "managed-agent",
            "settings": {"tools": {"builtin_tools": ["*"]}},
        },
        {
            "action": "settings_update",
            "agent_id": "managed-agent",
            "settings": {"builtin_tools": ["*"]},
        },
        {
            "action": "settings_update",
            "agent_id": "managed-agent",
            "settings": {"mcp_servers": [{"name": "privileged"}]},
        },
        {
            "action": "settings_update",
            "agent_id": "managed-agent",
            "settings": {"tool_permissions": {"*": "allow"}},
        },
        {
            "action": "settings_update",
            "agent_id": "managed-agent",
            "settings": {"allowed_secrets": ["production"]},
        },
        {
            "action": "settings_update",
            "agent_id": "managed-agent",
            "settings": {"allowed_credentials": ["admin"]},
        },
        {
            "action": "create",
            "name": "Escalated agent",
            "tools": {"builtin_tools": ["*"]},
        },
        {
            "action": "update",
            "agent_id": "managed-agent",
            "permissions": {"tool_permissions": {"*": "allow"}},
        },
        {
            "action": "update",
            "agent_id": "managed-agent",
            "execution": {"executor_id": "privileged-executor"},
        },
    ],
)
def test_agent_mutations_reject_raw_privilege_bearing_objects(
    arguments: dict[str, object],
) -> None:
    result = validate_available_tool_call(
        [MANAGE_AGENTS_TOOL],
        MANAGE_AGENTS_TOOL.name,
        arguments,
    )

    assert result["valid"] is False


def test_dynamic_option_resolution_populates_only_declared_authorized_values() -> None:
    resolved = resolve_descriptor_dynamic_options(
        [MANAGE_AGENTS_TOOL],
        {
            "agent_management.assignable_tools": ["builtin:list_agents"],
            "agent_management.tool_groups": ["conversations"],
        },
    )[0]
    assert resolved.descriptor is not None
    tools_set = next(
        operation
        for operation in resolved.descriptor.operations
        if operation.operation == "tools_set"
    )
    by_source = {option.source: option.values for option in tools_set.dynamic_options}

    assert by_source["agent_management.assignable_tools"] == ["builtin:list_agents"]
    assert by_source["agent_management.tool_groups"] == ["conversations"]


def test_orchestration_target_catalog_enriches_schema_descriptor_and_validation() -> None:
    delegate_target = OrchestrationTarget(
        agent_id="system:code-review",
        name="Code Review",
        description="Findings-first review",
        agent_type="secondary",
        is_system=True,
        profiles=(
            {
                "profile_id": "default",
                "description": "Default review profile",
                "is_default": True,
                "synthetic": True,
            },
        ),
    )
    managed_target = OrchestrationTarget(
        agent_id="lumi",
        name="Lumi",
        description="Lumilens primary agent",
        agent_type="primary",
        is_system=False,
        profiles=(
            {
                "profile_id": "ui-developer",
                "description": "Frontend implementation",
                "is_default": False,
                "synthetic": False,
            },
        ),
    )
    snapshot = OrchestrationTargetSnapshot(
        delegate=(delegate_target,),
        managed=(managed_target,),
    )

    delegate = enrich_orchestration_target_catalog(DELEGATE_TOOL, snapshot)
    managed = enrich_orchestration_target_catalog(
        AGENT_CONVERSATION_CREATE_TOOL,
        snapshot,
    )

    assert tool_input_schema(delegate)["properties"]["agent_id"]["enum"] == ["system:code-review"]
    assert tool_input_schema(managed)["properties"]["agent_id"]["enum"] == ["lumi"]
    assert (
        "Findings-first review"
        in tool_input_schema(delegate)["properties"]["agent_id"]["description"]
    )
    assert (
        "ui-developer: Frontend implementation"
        in tool_input_schema(managed)["properties"]["agent_id"]["description"]
    )

    described = describe_available_tool([delegate], "delegate")
    dynamic_options = described["descriptor"]["operations"][0]["dynamic_options"]
    assert dynamic_options[0]["source"] == "orchestration.delegate_targets"
    assert dynamic_options[0]["values"][0]["agent_id"] == "system:code-review"
    assert validate_available_tool_call(
        [delegate],
        "delegate",
        {"task": "Review the diff", "agent_id": "system:code-review"},
    )["valid"]
    assert not validate_available_tool_call(
        [delegate],
        "delegate",
        {"task": "Review the diff", "agent_id": "system:implement"},
    )["valid"]


def test_delegate_static_contract_has_no_manually_duplicated_specialist_catalog() -> None:
    assert "system:code-review" not in DELEGATE_TOOL.description
    assert "system:implement" not in DELEGATE_TOOL.description
    assert "system:explore" not in DELEGATE_TOOL.description


def test_schema_enrichment_rebuilds_descriptor_and_hash() -> None:
    original = ToolDefinition(
        name="dynamic_tool",
        description="Use a dynamic option.",
        parameters={
            "type": "object",
            "properties": {"model": {"type": "string"}},
        },
        source=ToolSource(type="executor"),
        read_only=True,
    )
    enriched_schema = {
        "type": "object",
        "properties": {"model": {"type": "string", "enum": ["model-a"]}},
    }

    enriched = tool_with_input_schema(original, enriched_schema)

    assert enriched.descriptor is not None
    assert original.descriptor is not None
    assert enriched.descriptor.schema_hash != original.descriptor.schema_hash
    assert tool_input_schema(enriched) == enriched_schema


def test_dynamic_enrichment_rebuilds_native_declaration_and_round_trip() -> None:
    enriched = tool_with_input_schema(
        IMAGE_GENERATE_TOOL,
        tool_input_schema(IMAGE_GENERATE_TOOL),
        dynamic_options=[
            ToolDynamicOption(
                path="$.model",
                source="image_generation_provider.list_models",
                values=["provider/model"],
            )
        ],
    )

    assert audit_tool_descriptors([enriched]) == []
    assert enriched.native_operations is not None
    assert enriched.native_operations[0].dynamic_options[0].values == ["provider/model"]


def test_describe_tool_exposes_multi_image_schemas() -> None:
    generated = describe_available_tool([IMAGE_GENERATE_TOOL], "image_generate")
    edited = describe_available_tool([IMAGE_EDIT_TOOL], "image_edit")

    generated_properties = generated["descriptor"]["input_schema"]["properties"]
    edited_schema = edited["descriptor"]["input_schema"]
    edited_properties = edited_schema["properties"]

    assert "references" in generated_properties
    assert "images" in edited_properties
    assert "mask_artifact_id" in edited_properties
    assert edited_schema["required"] == ["prompt", "images"]


def test_mcp_descriptor_preserves_live_schema_annotations_and_output_schema() -> None:
    tool = mcp_tools_to_definitions(
        "calendar",
        [
            {
                "name": "events",
                "description": "List calendar events.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"calendar_id": {"type": "string"}},
                    "required": ["calendar_id"],
                },
                "outputSchema": {"type": "object"},
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                },
            }
        ],
        30,
        server_id="srv_calendar",
    )[0]

    assert tool.descriptor is not None
    assert tool.descriptor.authority == "external"
    assert tool.descriptor.annotations["readOnlyHint"] is True
    assert tool.descriptor.output_schema == {"type": "object"}
    assert tool.descriptor.input_schema == tool.parameters
    assert tool.read_only is False


def test_replaced_ad_hoc_operations_are_absent() -> None:
    assert MANAGE_AGENTS_TOOL.native_operations is not None
    assert MANAGE_SCHEDULES_TOOL.native_operations is not None
    manage_actions = {operation.operation for operation in MANAGE_AGENTS_TOOL.native_operations}
    schedule_actions = {
        operation.operation for operation in MANAGE_SCHEDULES_TOOL.native_operations
    }

    assert "settings_schema" not in manage_actions
    assert "tools_list_available" not in manage_actions
    assert "tools_validate" not in manage_actions
    assert "options" not in schedule_actions


def test_write_deliverable_registers_authoritative_pulse_operation() -> None:
    assert WRITE_DELIVERABLE_TOOL.descriptor is not None
    operation = next(
        operation
        for operation in WRITE_DELIVERABLE_TOOL.descriptor.operations
        if operation.operation == "rich:pulse"
    )

    contract = WRITE_DELIVERABLE_TOOL.descriptor.extensions["presentation_contracts"]["rich:pulse"]
    assert WRITE_DELIVERABLE_TOOL.descriptor.schema_version == "cognis.tool.v2"
    assert operation.validator_ids == ["write_deliverable.rich"]
    assert operation.examples[0]["action"] == "rich:pulse"
    assert operation.input_schema["properties"]["action"]["const"] == "rich:pulse"
    assert contract["valid_skeleton"]


@pytest.mark.asyncio
async def test_write_deliverable_generic_block_types_match_runtime_validation() -> None:
    described = describe_available_tool([WRITE_DELIVERABLE_TOOL], "write_deliverable")
    operations = described["descriptor"]["operations"]
    generic = next(item for item in operations if item["operation"] == "write_deliverable")
    pulse = next(item for item in operations if item["operation"] == "rich:pulse")
    generic_type = generic["input_schema"]["definitions"]["genericRichBlock"]["properties"]["type"]
    pulse_type = pulse["input_schema"]["properties"]["rich"]["properties"]["blocks"]["items"][
        "properties"
    ]["type"]
    supported_types = generic_type["enum"]

    assert isinstance(supported_types, list)
    assert supported_types == sorted(SUPPORTED_RICH_BLOCK_TYPES)
    assert "markdown" in supported_types
    assert "text" not in supported_types
    assert pulse_type == {"type": "string"}

    markdown = {
        "action": "write_deliverable",
        "content": "Fallback",
        "format": "rich",
        "rich": {"blocks": [{"type": "markdown", "content": "## Summary"}]},
    }
    unsupported = {
        **markdown,
        "rich": {"blocks": [{"type": "text", "content": "## Summary"}]},
    }

    assert (
        validate_available_tool_call(
            [WRITE_DELIVERABLE_TOOL],
            "write_deliverable",
            markdown,
        )["valid"]
        is True
    )
    normalize_required_rich_payload(markdown["rich"])

    schema_result = validate_available_tool_call(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        unsupported,
    )
    assert schema_result["valid"] is False
    assert any("text" in error and "is not one of" in error for error in schema_result["errors"])
    with pytest.raises(RichPayloadValidationError, match="unsupported_rich_block_type"):
        normalize_required_rich_payload(unsupported["rich"])

    pulse_payload = deepcopy(PULSE_DAILY_SKELETON)
    pulse_payload["blocks"][0]["type"] = "research_answer"
    pulse_result = await validate_available_tool_call_with_context(
        [WRITE_DELIVERABLE_TOOL],
        "write_deliverable",
        {
            "action": "rich:pulse",
            "content": "Pulse fallback",
            "format": "rich",
            "rich": pulse_payload,
        },
        None,
    )
    assert pulse_result["valid"] is False


@pytest.mark.asyncio
async def test_agent_loop_dynamic_options_use_restricted_effective_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = ToolDefinition(
        name="allowed_read",
        description="Read allowed state.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="executor"),
        read_only=True,
    )
    denied = ToolDefinition(
        name="denied_write",
        description="Mutate denied state.",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="executor"),
    )

    async def empty(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr("cognis.store.queries.list_visible_agents", empty)
    monkeypatch.setattr("cognis.store.queries.list_workflows", empty)
    monkeypatch.setattr("cognis.store.queries.list_knowledgebases", empty)
    monkeypatch.setattr("cognis.store.queries.list_executors", empty)
    monkeypatch.setattr("cognis.store.queries.list_llm_providers", empty)
    monkeypatch.setattr("cognis.store.queries.list_skills", empty)

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    loop = AgentLoop.__new__(AgentLoop)
    loop._session_factory = SessionContext
    resolved = await loop._resolve_introspection_dynamic_options(
        SimpleNamespace(
            session=SimpleNamespace(user_email="owner@example.com"),
            agent=SimpleNamespace(permissions=None),
        ),
        [MANAGE_AGENTS_TOOL, allowed],
    )
    described = describe_available_tool(resolved, "manage_agents")
    operations = described["descriptor"]["operations"]
    tools_set = next(item for item in operations if item["operation"] == "tools_set")
    by_source = {option["source"]: option["values"] for option in tools_set["dynamic_options"]}

    assert stable_tool_id(allowed) in by_source["agent_management.assignable_tools"]
    assert stable_tool_id(denied) not in by_source["agent_management.assignable_tools"]
    assert "knowledgebase_read" not in by_source["agent_management.tool_groups"]

"""Action-scoped contracts for privileged Cognis configuration management."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from cognis.api.models import (
    ExecutorCreateRequest,
    ExecutorUpdateRequest,
    LLMProviderRequest,
    LLMProviderUpdateRequest,
    ModelRoutingUpdateRequest,
    StepProfileCreateRequest,
    StepProfileUpdateRequest,
)
from cognis.models.tool import (
    NativeToolOperation,
    ToolCapability,
    ToolDefinition,
    ToolMutationKind,
    ToolSource,
    ToolValueSemantics,
)
from cognis.providers.llm.recovery_policy import NamedRouteConfig

SYSTEM_MANAGEMENT_TOOLS = frozenset(
    {
        "manage_providers",
        "manage_routes",
        "manage_settings",
        "manage_executors",
    }
)


def _model_schema(model: type[BaseModel]) -> dict[str, Any]:
    # Resolve Pydantic local references before nesting a model under payload.
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(definitions[value["$ref"].removeprefix("#/$defs/")])
        return {key: inline(item) for key, item in value.items()}

    result = inline(schema)
    assert isinstance(result, dict)
    result["additionalProperties"] = False
    identifier = (
        "provider_id"
        if model is LLMProviderRequest
        else "executor_id"
        if model is ExecutorCreateRequest
        else None
    )
    if identifier:
        result["properties"][identifier] = {"type": "string", "minLength": 1}
        result["required"] = [*result.get("required", []), identifier]
    return result


def _operation(
    action: str,
    kind: ToolMutationKind,
    summary: str,
    fields: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
) -> NativeToolOperation:
    properties: dict[str, Any] = {"action": {"type": "string", "const": action}, **(fields or {})}
    if kind in {ToolMutationKind.CREATE, ToolMutationKind.UPDATE, ToolMutationKind.DELETE}:
        properties["preview"] = {"type": "boolean", "default": False}
    schema = {
        "type": "object",
        "properties": properties,
        "required": ["action", *required],
        "additionalProperties": False,
    }
    return NativeToolOperation(
        operation=action,
        summary=summary,
        mutation_kind=kind,
        input_schema=schema,
        examples=[_schema_example(schema)],
        semantics=ToolValueSemantics(
            omitted="Preserve omitted patch fields; creation uses documented defaults.",
            null="Accepted only in explicitly nullable payload fields; never means delete implicitly.",
            arrays="Supplied arrays replace the corresponding collection.",
            concurrency="Read first. Supply the returned precondition; stale writes fail. Preview never reserves a revision.",
        ),
        side_effects=[]
        if kind is ToolMutationKind.READ
        else [
            "May persist configuration, contact a provider, or reconfigure an executor. No automatic retries after an uncertain outcome.",
        ],
    )


def _schema_example(schema: dict[str, Any]) -> Any:
    """Produce structural examples; resource IDs must be replaced with discovered values."""
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return _schema_example(schema["anyOf"][0])
    if "default" in schema:
        return schema["default"]
    kind = schema.get("type")
    if kind == "object":
        return {
            key: _schema_example(schema["properties"][key]) for key in schema.get("required", [])
        }
    if kind == "array":
        return [_schema_example(schema["items"])] * schema.get("minItems", 0)
    if kind in {"integer", "number"}:
        return schema.get("minimum", 1)
    if kind == "boolean":
        return False
    if kind == "string":
        return "value-from-read-operation"
    return None


_ID = {"type": "string", "minLength": 1}
_EXPECTED = {"expected_updated_at": _ID}
_PROVIDER = {"provider_id": _ID}
_EXECUTOR = {"executor_id": _ID}
_NAMED = {"route_id": _ID}
_REVISION = {"expected_revision": {"type": "integer", "minimum": 1}}
_READ = ToolMutationKind.READ
_UPDATE = ToolMutationKind.UPDATE
_CREATE = ToolMutationKind.CREATE
_DELETE = ToolMutationKind.DELETE
_EXECUTE = ToolMutationKind.EXECUTE


def _definition(
    name: str, description: str, operations: list[NativeToolOperation]
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description + " Root owner primary chats only; explicit agent opt-in required. "
        "Call describe_tool for the exact action schema before mutation. Never pass raw secrets.",
        parameters={},
        native_operations=operations,
        source=ToolSource(type="builtin"),
        category="system_management",
        profile_group="system",
        read_only=False,
        capabilities=[ToolCapability.PRIVILEGED, ToolCapability.WRITE],
        non_bypassable=True,
        timeout_seconds=120,
        max_result_size=100_000,
    )


def system_management_tools() -> list[ToolDefinition]:
    """Build discoverable domain tools with a single authoritative action schema."""
    return [
        _definition(
            "manage_providers",
            "Manage LLM provider configurations, models, defaults, health, and authentication.",
            [
                _operation("list", _READ, "List visible providers with redacted configuration."),
                _operation(
                    "get",
                    _READ,
                    "Read a visible provider and its update precondition.",
                    _PROVIDER,
                    ("provider_id",),
                ),
                _operation(
                    "schema", _READ, "Describe provider payloads and secret-reference rules."
                ),
                _operation(
                    "create",
                    _CREATE,
                    "Create a provider; explicit provider_id makes retry reconciliation possible.",
                    {"payload": _model_schema(LLMProviderRequest)},
                    ("payload",),
                ),
                _operation(
                    "update",
                    _UPDATE,
                    "Patch provider fields; config replaces the complete configuration.",
                    {**_PROVIDER, **_EXPECTED, "payload": _model_schema(LLMProviderUpdateRequest)},
                    ("provider_id", "expected_updated_at", "payload"),
                ),
                *[
                    _operation(
                        action,
                        kind,
                        summary,
                        {**_PROVIDER, **_EXPECTED},
                        ("provider_id", "expected_updated_at"),
                    )
                    for action, kind, summary in (
                        ("delete", _DELETE, "Delete an unreferenced provider."),
                        ("set_default", _UPDATE, "Set the default in the provider owner's scope."),
                    )
                ],
                *[
                    _operation(action, kind, summary, _PROVIDER, ("provider_id",))
                    for action, kind, summary in (
                        (
                            "discover_models",
                            _EXECUTE,
                            "Fetch available models without saving model overrides.",
                        ),
                        ("health_get", _READ, "Read cached health without an upstream refresh."),
                        (
                            "health_refresh",
                            _EXECUTE,
                            "Refresh shared provider health observations.",
                        ),
                        (
                            "test",
                            _EXECUTE,
                            "Run a potentially billable provider test with a cooldown.",
                        ),
                    )
                ],
                _operation(
                    "enrich_models",
                    _EXECUTE,
                    "Resolve model metadata without saving overrides.",
                    {
                        **_PROVIDER,
                        "model_ids": {
                            "type": "array",
                            "items": _ID,
                            "minItems": 1,
                            "maxItems": 100,
                        },
                    },
                    ("provider_id", "model_ids"),
                ),
                *[
                    _operation(
                        action,
                        kind,
                        summary,
                        {
                            **_PROVIDER,
                            "auth_provider": {"type": "string", "enum": ["chatgpt", "anthropic"]},
                        },
                        ("provider_id", "auth_provider"),
                    )
                    for action, kind, summary in (
                        (
                            "auth_status",
                            _EXECUTE,
                            "Poll OAuth status; may complete authorization. Never exposes tokens.",
                        ),
                        (
                            "auth_start",
                            _EXECUTE,
                            "Begin human OAuth authorization; never submit callback credentials in chat.",
                        ),
                        (
                            "auth_disconnect",
                            _DELETE,
                            "Disconnect the provider's OAuth authorization.",
                        ),
                    )
                ],
            ],
        ),
        _definition(
            "manage_routes",
            "Manage custom named inference routes and global task-to-model bindings.",
            [
                _operation("named_list", _READ, "List visible custom routes."),
                _operation(
                    "named_get",
                    _READ,
                    "Read a custom route by its actual route ID.",
                    _NAMED,
                    ("route_id",),
                ),
                _operation(
                    "named_create",
                    _CREATE,
                    "Create a custom route with concrete provider/model targets.",
                    {"config": _model_schema(NamedRouteConfig)},
                    ("config",),
                ),
                _operation(
                    "named_update",
                    _UPDATE,
                    "Replace a custom route using its revision.",
                    {**_NAMED, **_REVISION, "config": _model_schema(NamedRouteConfig)},
                    ("route_id", "expected_revision", "config"),
                ),
                _operation(
                    "named_delete",
                    _DELETE,
                    "Delete a custom route only when unreferenced.",
                    {**_NAMED, **_REVISION},
                    ("route_id", "expected_revision"),
                ),
                _operation(
                    "bindings_get", _READ, "Read system task bindings and aggregate revision."
                ),
                _operation(
                    "bindings_update",
                    _UPDATE,
                    "Patch only supplied task bindings; each entry replaces that binding.",
                    {
                        "bindings": _model_schema(ModelRoutingUpdateRequest),
                        "expected_revision": _ID,
                    },
                    ("bindings", "expected_revision"),
                ),
                _operation(
                    "bindings_reset",
                    _UPDATE,
                    "Reset selected task bindings to inheritance.",
                    {
                        "task_types": {"type": "array", "items": _ID, "minItems": 1},
                        "expected_revision": _ID,
                    },
                    ("task_types", "expected_revision"),
                ),
                _operation(
                    "resolve",
                    _READ,
                    "Explain the real inference plan without making an inference request.",
                    {"model": _ID, "task_type": _ID},
                    ("model",),
                ),
            ],
        ),
        _definition(
            "manage_settings",
            "Inspect, update, and reset typed Cognis application settings.",
            [
                _operation(
                    "list",
                    _READ,
                    "List supported persisted settings with defaults and application timing.",
                ),
                _operation(
                    "get",
                    _READ,
                    "Read one exposed setting with its concurrency token.",
                    {"key": _ID},
                    ("key",),
                ),
                _operation(
                    "schema", _READ, "Read the authoritative typed setting registry.", {"key": _ID}
                ),
                _operation(
                    "update",
                    _UPDATE,
                    "Set a validated value and apply it according to its registry scope.",
                    {"key": _ID, "value": {}, **_EXPECTED},
                    ("key", "value", "expected_updated_at"),
                ),
                _operation(
                    "reset",
                    _UPDATE,
                    "Reset one setting to its registry default.",
                    {"key": _ID, **_EXPECTED},
                    ("key", "expected_updated_at"),
                ),
                _operation(
                    "diagnostics", _READ, "Report safe configuration and provider readiness counts."
                ),
                _operation("web_get", _READ, "Read exposed web backend settings."),
                _operation(
                    "web_update",
                    _UPDATE,
                    "Update one typed web setting with its precondition.",
                    {"key": _ID, "value": {}, **_EXPECTED},
                    ("key", "value", "expected_updated_at"),
                ),
                _operation(
                    "step_profiles_list",
                    _READ,
                    "Read workflow step profiles and aggregate revision.",
                ),
                _operation(
                    "step_profiles_get",
                    _READ,
                    "Read a workflow step profile and aggregate revision.",
                    {"profile_id": _ID},
                    ("profile_id",),
                ),
                _operation(
                    "step_profiles_create",
                    _CREATE,
                    "Create a custom workflow step profile.",
                    {"payload": _model_schema(StepProfileCreateRequest), "expected_revision": _ID},
                    ("payload", "expected_revision"),
                ),
                _operation(
                    "step_profiles_update",
                    _UPDATE,
                    "Replace a custom or built-in profile override.",
                    {
                        "profile_id": _ID,
                        "payload": _model_schema(StepProfileUpdateRequest),
                        "expected_revision": _ID,
                    },
                    ("profile_id", "payload", "expected_revision"),
                ),
                _operation(
                    "step_profiles_reset",
                    _DELETE,
                    "Reset an override or delete an unreferenced custom profile.",
                    {"profile_id": _ID, "expected_revision": _ID},
                    ("profile_id", "expected_revision"),
                ),
            ],
        ),
        _definition(
            "manage_executors",
            "Manage executor configuration, browser/local inference settings, and runtime reconfiguration.",
            [
                _operation("list", _READ, "List caller-visible executors without credentials."),
                _operation(
                    "get",
                    _READ,
                    "Read executor configuration and desired/applied runtime generations.",
                    _EXECUTOR,
                    ("executor_id",),
                ),
                _operation("schema", _READ, "Describe executor configuration payloads."),
                _operation(
                    "create",
                    _CREATE,
                    "Create an executor configuration; provision_token stores its token securely.",
                    {"payload": _model_schema(ExecutorCreateRequest)},
                    ("payload",),
                ),
                _operation(
                    "update",
                    _UPDATE,
                    "Patch executor configuration; supplied config and arrays replace.",
                    {**_EXECUTOR, **_EXPECTED, "payload": _model_schema(ExecutorUpdateRequest)},
                    ("executor_id", "expected_updated_at", "payload"),
                ),
                _operation(
                    "delete",
                    _DELETE,
                    "Delete a non-default executor and fence its connection.",
                    {**_EXECUTOR, **_EXPECTED},
                    ("executor_id", "expected_updated_at"),
                ),
                _operation(
                    "set_default",
                    _UPDATE,
                    "Make an executor the default in its ownership scope.",
                    {**_EXECUTOR, **_EXPECTED},
                    ("executor_id", "expected_updated_at"),
                ),
                _operation(
                    "reconfigure",
                    _EXECUTE,
                    "Retry runtime initialization without replacing configuration.",
                    _EXECUTOR,
                    ("executor_id",),
                ),
                _operation(
                    "provision_token",
                    _EXECUTE,
                    "Rotate a private executor token into an encrypted caller-agent credential; return only its reference. Reuse operation_id to reconcile an uncertain outcome, never rotate blindly.",
                    {
                        **_EXECUTOR,
                        **_EXPECTED,
                        "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    ("executor_id", "expected_updated_at", "operation_id"),
                ),
            ],
        ),
    ]

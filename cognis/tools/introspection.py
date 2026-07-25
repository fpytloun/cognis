"""Unified, authorization-scoped tool description and validation."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft7Validator, SchemaError  # type: ignore[import-untyped]

from cognis.models.tool import (
    ToolDefinition,
    derive_native_input_schema,
    finalize_tool_descriptor,
    stable_tool_id,
    tool_display_name,
    tool_input_schema,
    tool_matches_identifier,
)
from cognis.tools.native_validation import (
    NativeValidationContext,
    registered_native_validator_ids,
    validate_native_operation_domains,
)


def resolve_available_tool(
    tools: list[ToolDefinition],
    identifier: str,
) -> ToolDefinition | None:
    """Resolve only within the caller's already authorization-filtered inventory."""

    matches = [
        tool
        for tool in tools
        if tool_matches_identifier(tool, identifier)
        or tool_display_name(tool) == identifier
        or tool.source.raw_tool_name == identifier
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def describe_available_tool(
    tools: list[ToolDefinition],
    identifier: str,
) -> dict[str, Any]:
    """Return the authoritative descriptor for one currently available tool."""

    tool = resolve_available_tool(tools, identifier)
    if tool is None:
        return {
            "valid": False,
            "error": "tool_not_available",
            "message": "No unique currently authorized tool matches the supplied identifier.",
        }
    assert tool.descriptor is not None
    return {
        "valid": True,
        "tool_id": stable_tool_id(tool),
        "name": tool_display_name(tool),
        "callable_name": tool.name,
        "description": tool.description,
        "source": tool.source.model_dump(mode="json"),
        "category": tool.category,
        "read_only": tool.read_only,
        "descriptor": tool.descriptor.model_dump(mode="json"),
    }


def validate_available_tool_call(
    tools: list[ToolDefinition],
    identifier: str,
    arguments: Any,
) -> dict[str, Any]:
    """Validate a proposed call against the same descriptor exposed to the model."""

    tool = resolve_available_tool(tools, identifier)
    if tool is None:
        return {
            "valid": False,
            "error": "tool_not_available",
            "message": "No unique currently authorized tool matches the supplied identifier.",
        }
    if not isinstance(arguments, dict):
        return {
            "valid": False,
            "tool_id": stable_tool_id(tool),
            "callable_name": tool.name,
            "errors": ["<root>: arguments must be a JSON object"],
        }
    errors = _schema_errors(tool_input_schema(tool), arguments)
    operation = _selected_operation(tool, arguments)
    if operation is not None:
        errors.extend(_schema_errors(operation.input_schema, arguments))
    errors = list(dict.fromkeys(errors))
    return {
        "valid": not errors,
        "tool_id": stable_tool_id(tool),
        "callable_name": tool.name,
        "operation": operation.operation if operation is not None else tool.name,
        "schema_version": tool.descriptor.schema_version if tool.descriptor else None,
        "schema_hash": tool.descriptor.schema_hash if tool.descriptor else None,
        "errors": errors,
    }


async def validate_available_tool_call_with_context(
    tools: list[ToolDefinition],
    identifier: str,
    arguments: Any,
    context: NativeValidationContext | None,
) -> dict[str, Any]:
    """Validate schema and declared native domains without executing the tool."""

    result = validate_available_tool_call(tools, identifier, arguments)
    if not isinstance(arguments, dict):
        return result
    tool = resolve_available_tool(tools, identifier)
    operation = _selected_operation(tool, arguments) if tool is not None else None
    if operation is None or tool is None or not tool.native_operations:
        return result
    if not result.get("valid") and "write_deliverable.rich" not in operation.validator_ids:
        return result
    issues = await validate_native_operation_domains(operation, arguments, context)
    if not issues:
        return result
    rejected = {
        **result,
        "valid": False,
        "errors": [
            {
                "code": issue.code,
                "path": issue.path,
                "message": issue.message,
            }
            for issue in issues
        ],
    }
    if not result.get("valid"):
        rejected["schema_errors"] = result.get("errors", [])
    return rejected


def resolve_descriptor_dynamic_options(
    tools: list[ToolDefinition],
    values_by_source: dict[str, list[Any]],
) -> list[ToolDefinition]:
    """Populate declared live options without changing tool authorization."""

    resolved: list[ToolDefinition] = []
    for tool in tools:
        if tool.descriptor is None:
            resolved.append(tool)
            continue
        changed = False
        operations = []
        for operation in tool.descriptor.operations:
            options = []
            for option in operation.dynamic_options:
                values = values_by_source.get(option.source)
                if values is None:
                    options.append(option)
                    continue
                changed = True
                options.append(option.model_copy(update={"values": list(values)}))
            operations.append(operation.model_copy(update={"dynamic_options": options}))
        if not changed:
            resolved.append(tool)
            continue
        descriptor = finalize_tool_descriptor(
            tool.descriptor.model_copy(update={"operations": operations})
        )
        if tool.native_operations:
            payload = tool.model_dump(mode="python")
            payload["descriptor"] = None
            payload["native_operations"] = operations
            resolved.append(ToolDefinition.model_validate(payload))
        else:
            resolved.append(tool.model_copy(update={"descriptor": descriptor}))
    return resolved


def audit_tool_descriptors(tools: list[ToolDefinition]) -> list[str]:
    """Return descriptor/schema/example consistency failures for native tools."""

    failures: list[str] = []
    for tool in tools:
        if tool.source.type not in {"builtin", "controller", "executor"}:
            continue
        if tool.descriptor is None:
            failures.append(f"{tool.name}: missing descriptor")
            continue
        if not tool.native_operations:
            failures.append(f"{tool.name}: native tool has no declared native_operations")
            continue
        if tool.parameters != tool.descriptor.input_schema:
            failures.append(f"{tool.name}: parameters diverge from descriptor input_schema")
        if tool.descriptor.operations != tool.native_operations:
            failures.append(f"{tool.name}: descriptor operations diverge from native declarations")
        try:
            derived = derive_native_input_schema(tool.native_operations)
        except ValueError as exc:
            failures.append(f"{tool.name}: invalid native operations: {exc}")
        else:
            if derived != tool.descriptor.input_schema:
                failures.append(f"{tool.name}: native operations do not derive descriptor schema")
        if not tool.descriptor.schema_hash.startswith("sha256:"):
            failures.append(f"{tool.name}: missing schema hash")
        if not tool.descriptor.operations:
            failures.append(f"{tool.name}: no operations")
        for operation in tool.descriptor.operations:
            unknown_validators = sorted(
                set(operation.validator_ids) - registered_native_validator_ids()
            )
            if unknown_validators:
                failures.append(
                    f"{tool.name}/{operation.operation}: unknown validators "
                    + ", ".join(unknown_validators)
                )
            if not operation.examples:
                failures.append(f"{tool.name}/{operation.operation}: no examples")
            for index, example in enumerate(operation.examples):
                errors = _schema_errors(operation.input_schema, example)
                if errors:
                    failures.append(
                        f"{tool.name}/{operation.operation} example {index}: {'; '.join(errors)}"
                    )
        try:
            round_trip = ToolDefinition.model_validate(tool.model_dump(mode="python"))
        except ValueError as exc:
            failures.append(f"{tool.name}: serialization round-trip failed: {exc}")
        else:
            if (
                round_trip.descriptor is None
                or round_trip.descriptor.schema_hash != tool.descriptor.schema_hash
            ):
                failures.append(f"{tool.name}: serialization round-trip changed schema hash")
    return failures


async def audit_native_tool_domains(
    tools: list[ToolDefinition],
    context: NativeValidationContext | None,
) -> list[str]:
    """Validate every declared native example through registered domain validators."""

    failures: list[str] = []
    for tool in tools:
        if not tool.native_operations:
            continue
        for operation in tool.native_operations:
            for index, example in enumerate(operation.examples):
                issues = await validate_native_operation_domains(
                    operation,
                    example,
                    context,
                )
                if issues:
                    failures.append(
                        f"{tool.name}/{operation.operation} example {index}: "
                        + "; ".join(
                            f"{issue.code}@{issue.path}: {issue.message}" for issue in issues
                        )
                    )
    return failures


def _selected_operation(tool: ToolDefinition, arguments: dict[str, Any]) -> Any | None:
    if tool.descriptor is None:
        return None
    selected = arguments.get("action")
    if isinstance(selected, str):
        for operation in tool.descriptor.operations:
            if operation.operation == selected:
                return operation
    if len(tool.descriptor.operations) == 1:
        return tool.descriptor.operations[0]
    return None


def _schema_errors(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    try:
        validator = Draft7Validator(schema)
    except SchemaError as exc:
        return [f"<schema>: {exc.message}"]
    errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.absolute_path))
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in errors[:10]
    ]

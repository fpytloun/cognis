"""Schema-aware normalization for tool call arguments."""

from __future__ import annotations

from typing import Any


def strip_empty_optional_values(
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Remove empty optional values from tool arguments.

    LLMs frequently materialize optional JSON Schema fields as empty strings or
    empty collections instead of omitting them. Many tool handlers and upstream
    APIs treat those placeholders as present invalid values. This helper drops
    empty optional values while preserving required fields, explicit booleans,
    and numeric zeros.
    """
    if not schema:
        return arguments
    required: set[str] = set(schema.get("required", []))
    properties: dict[str, Any] = schema.get("properties", {})
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if key not in required and _is_empty_placeholder(value):
            continue
        if isinstance(value, dict) and key in properties:
            nested_schema = properties[key]
            if isinstance(nested_schema, dict) and nested_schema.get("type") == "object":
                value = strip_empty_optional_values(value, nested_schema)
        if isinstance(value, list) and key in properties:
            nested_schema = properties[key]
            if isinstance(nested_schema, dict):
                items_schema = nested_schema.get("items", {})
                if isinstance(items_schema, dict) and items_schema.get("type") == "object":
                    value = [
                        strip_empty_optional_values(item, items_schema)
                        if isinstance(item, dict)
                        else item
                        for item in value
                    ]
        cleaned[key] = value
    return cleaned


def _is_empty_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False

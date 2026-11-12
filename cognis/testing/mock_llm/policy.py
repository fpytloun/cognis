"""Deterministic, narrow Intaris assessment model for the PTC E2E fixture.

This is a test-only evaluator model, not an Intaris/provider substitute. Intaris
still builds the active policy, applies its normal fail-closed evaluator, and
owns the final decision. Unknown tools/actions receive unknown classifications.
"""

from __future__ import annotations

import json
from typing import Any

PTC_POLICY_MODEL = "ptc-policy-model"
_QUALIFICATION_MARKERS = ("PTC-QUALIFY-01", "PTC-QUALIFY-02", "PTC-MUTATION-01")

_SAFE_VALUES: dict[str, Any] = {
    "mutation": "mutating",
    "relationship": "directly_serves_intent",
    "risk": "low",
    "authorization": "explicit",
    "operation": "write",
    "environment": "development",
    "reversibility": "reversible",
    "side_effect_scope": "bounded",
    "authorization_source": "latest_user_message",
    "authorization_scope": "within_scope",
    "effects": ["execute", "read", "write"],
    "consequence": "routine",
    "contextual_restrictions": "consistent",
    "target_scope": "consistent",
    "restriction_reference": "none",
    "target_reference": "none",
    "restriction_quote": "",
    "status": "consistent",
    "reference": "latest_user_message",
}
_UNKNOWN_VALUES: dict[str, Any] = {
    "mutation": "unclear",
    "relationship": "unclear",
    "risk": "unknown",
    "authorization": "unclear",
    "operation": "unknown",
    "environment": "unknown",
    "reversibility": "unknown",
    "side_effect_scope": "unknown",
    "authorization_source": "none",
    "authorization_scope": "unclear",
    "effects": ["unknown"],
    "consequence": "unclear",
    "contextual_restrictions": "unresolved",
    "target_scope": "unresolved",
    "restriction_reference": "none",
    "target_reference": "none",
    "restriction_quote": "",
    "status": "unresolved",
    "reference": "none",
}


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part["text"])
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return ""


def _schema(response_format: Any) -> dict[str, Any] | None:
    if not isinstance(response_format, dict):
        return None
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, dict):
        return None
    schema = json_schema.get("schema")
    return schema if isinstance(schema, dict) else None


def _enum_values(spec: Any) -> list[Any]:
    if not isinstance(spec, dict):
        return []
    values = list(spec.get("enum", [])) if isinstance(spec.get("enum"), list) else []
    if "const" in spec:
        values.append(spec["const"])
    for key in ("anyOf", "oneOf", "allOf"):
        variants = spec.get(key)
        if isinstance(variants, list):
            for variant in variants:
                values.extend(_enum_values(variant))
    return list(dict.fromkeys(values))


def _field_value(
    name: str,
    schema: dict[str, Any],
    *,
    allowed_action: bool,
) -> Any:
    preferred = (_SAFE_VALUES if allowed_action else _UNKNOWN_VALUES).get(name)
    enum = _enum_values(schema)
    if name == "effects":
        item_enum = _enum_values(schema.get("items"))
        if preferred is None:
            preferred = ["unknown"]
        return [value for value in preferred if not item_enum or value in item_enum]
    if preferred in enum or not enum:
        return preferred
    if name == "authorization_source":
        for fallback in ("session_intention", "none"):
            if fallback in enum:
                return fallback
    for fallback in ("unclear", "unresolved", "unknown", "none"):
        if fallback in enum:
            return fallback
    raise ValueError(f"Unsupported required test-policy field: {name}")


def _object_values(schema: dict[str, Any], *, allowed_action: bool) -> dict[str, Any]:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("Unsupported test-policy object schema")
    result: dict[str, Any] = {}
    for name in required:
        field_schema = properties.get(name)
        if not isinstance(field_schema, dict):
            raise ValueError(f"Unsupported required test-policy field: {name}")
        if "properties" in field_schema:
            result[name] = _object_values(field_schema, allowed_action=allowed_action)
        else:
            result[name] = _field_value(name, field_schema, allowed_action=allowed_action)
    return result


def build_ptc_policy_assessment(
    messages: list[dict[str, Any]], response_format: Any
) -> tuple[str, dict[str, Any]]:
    """Return a schema-shaped classification; never make a policy decision."""
    user_text = _last_user_text(messages)
    allowed_action = "python_execute" in user_text and any(
        marker in user_text for marker in _QUALIFICATION_MARKERS
    )
    schema = _schema(response_format)
    if schema is None:
        result = {
            name: _field_value(name, {}, allowed_action=allowed_action) for name in _SAFE_VALUES
        }
    else:
        result = _object_values(schema, allowed_action=allowed_action)
    return json.dumps(result, ensure_ascii=False), {
        "test_model": PTC_POLICY_MODEL,
        "qualified_action": allowed_action,
        "classification": result,
    }


__all__ = ["PTC_POLICY_MODEL", "build_ptc_policy_assessment"]

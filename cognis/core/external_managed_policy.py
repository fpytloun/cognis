"""Fail-closed capability policy for managed external conversations."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from cognis.models.tool import ToolDefinition, stable_tool_id
from cognis.providers.memory.policy import MemoryRuntimePolicy

MEMORY_SEARCH_TOOLS = frozenset({"memory_search", "memory_find", "memory_ask"})
MEMORY_MUTATION_TOOLS = frozenset(
    {
        "memory_add",
        "memory_add_batch",
        "memory_update",
        "memory_delete",
        "memory_save_artifact",
        "memory_delete_artifact",
    }
)


def external_managed_policy(context: Any) -> dict[str, Any] | None:
    """Return the immutable external policy snapshot from conversation context."""

    platform_data = getattr(context, "platform_data", None)
    if not isinstance(platform_data, dict):
        return None
    if platform_data.get("managed_conversation_kind") != "channel":
        return None
    policy = platform_data.get("managed_creation_policy_snapshot")
    return policy if isinstance(policy, dict) else {}


def restrict_external_memory_policy(
    policy: MemoryRuntimePolicy,
    context: Any,
) -> MemoryRuntimePolicy:
    """Keep configured bootstrap memory but disable ambient recall and writes."""

    if external_managed_policy(context) is None:
        return policy
    return replace(policy, auto_recall=False, auto_remember=False)


def external_tool_allowed(
    *,
    tool_name: str,
    tool_id: str,
    context: Any,
    memory_backend_configured: bool,
) -> bool:
    """Apply the creation snapshot, allowlist, and non-bypassable external floor."""

    policy = external_managed_policy(context)
    if policy is None:
        return True
    snapshot = {str(value) for value in policy.get("tool_ids", []) if value}
    allowlist = {str(value) for value in policy.get("explicit_tool_allowlist", []) if value}
    if tool_id not in snapshot and tool_name not in snapshot:
        return False
    if tool_id not in allowlist and tool_name not in allowlist:
        return False
    if tool_name in MEMORY_MUTATION_TOOLS or (
        tool_name.startswith("memory_") and tool_name not in MEMORY_SEARCH_TOOLS
    ):
        return False
    if tool_name in MEMORY_SEARCH_TOOLS:
        return bool(
            memory_backend_configured and policy.get("memory_search_safety_permitted") is True
        )
    return True


def filter_external_tool_definitions(
    tools: list[ToolDefinition],
    *,
    context: Any,
    memory_backend_configured: bool,
) -> list[ToolDefinition]:
    """Filter runtime definitions without ever adding a tool."""

    return [
        tool
        for tool in tools
        if external_tool_allowed(
            tool_name=tool.name,
            tool_id=stable_tool_id(tool),
            context=context,
            memory_backend_configured=memory_backend_configured,
        )
    ]


def filter_external_controller_schemas(
    schemas: list[dict[str, Any]],
    *,
    context: Any,
    memory_backend_configured: bool,
) -> list[dict[str, Any]]:
    """Filter controller schemas by their stable builtin names."""

    filtered: list[dict[str, Any]] = []
    for schema in schemas:
        function = schema.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str):
            continue
        if external_tool_allowed(
            tool_name=name,
            tool_id=f"builtin:{name}",
            context=context,
            memory_backend_configured=memory_backend_configured,
        ):
            filtered.append(schema)
    return filtered

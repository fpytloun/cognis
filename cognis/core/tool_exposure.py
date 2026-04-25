"""Provider-aware tool exposure for model-facing tool schemas."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cognis.models.config import ModelInfo
from cognis.models.tool import ToolDefinition, stable_tool_id, tool_profile_group
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL

_VISIBLE_TOOL_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_VISIBLE_TOOL_NAME_LENGTH = 64

# Tools that must always be visible when present in the inventory, regardless
# of provider slot caps.  These are the model's essential recovery and
# navigation primitives.
_CRITICAL_GENERIC_TOOL_NAMES = frozenset(
    {
        "skill_load",
        "read_tool_output",
        "search_tool_output",
        "list_tool_output_anchors",
        "read_tool_output_anchor",
    }
)


@dataclass(slots=True)
class ToolExposureResult:
    """Prepared model-facing tools and related metadata."""

    tools: list[dict[str, Any]]
    alias_map: dict[str, str]
    request_kwargs: dict[str, Any] = field(default_factory=dict)
    visible_tool_ids: set[str] = field(default_factory=set)
    hidden_searchable_tool_ids: set[str] = field(default_factory=set)
    debug_metadata: dict[str, Any] = field(default_factory=dict)


class LLMApiMode(StrEnum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class ToolDiscoveryMode(StrEnum):
    CONTROLLER_SEARCH = "controller_search"
    NONE = "none"


class EditToolMode(StrEnum):
    """Preferred model-facing editing surface."""

    APPLY_PATCH = "apply_patch"
    EXACT = "exact"


class EditToolFamily(StrEnum):
    """Coarse model family used for prompt and tool-surface tailoring."""

    GPT5 = "gpt5"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    GROQ = "groq"
    OPEN_SOURCE = "open_source"
    GENERIC = "generic"


_EXACT_TOOL_NAMES = frozenset({"edit", "multiedit", "write"})
_APPLY_PATCH_TOOL_NAMES = frozenset({"apply_patch"})
_OPEN_SOURCE_MODEL_TOKENS = (
    "gpt-oss",
    "llama",
    "qwen",
    "mistral",
    "mixtral",
    "deepseek",
    "gemma",
    "phi",
    "falcon",
    "rwkv",
    "granite",
    "olmo",
    "yi-",
    "smollm",
)


@dataclass(frozen=True, slots=True)
class ToolExposureContract:
    llm_api: LLMApiMode
    discovery_mode: ToolDiscoveryMode
    native_apply_patch: bool = False
    native_apply_patch_reason: str | None = None


def detect_edit_tool_family(model_id: str | None) -> EditToolFamily:
    """Return the family used for edit-tool preference decisions."""

    normalized = _normalize_model_name(model_id)
    if "claude" in normalized or normalized.startswith("anthropic/"):
        return EditToolFamily.ANTHROPIC
    if "gemini" in normalized or normalized.startswith(("google/", "vertex_ai/")):
        return EditToolFamily.GEMINI
    if normalized.startswith("groq/"):
        return EditToolFamily.GROQ
    if ("gpt-5" in normalized or "codex" in normalized) and "gpt-oss" not in normalized:
        return EditToolFamily.GPT5
    if any(token in normalized for token in _OPEN_SOURCE_MODEL_TOKENS):
        return EditToolFamily.OPEN_SOURCE
    return EditToolFamily.GENERIC


def preferred_edit_tool_mode(model_id: str | None) -> EditToolMode:
    """Return the preferred edit surface for a model family."""

    if detect_edit_tool_family(model_id) is EditToolFamily.GPT5:
        return EditToolMode.APPLY_PATCH
    return EditToolMode.EXACT


def filter_edit_tools_for_model(tools: list[ToolDefinition], model_id: str | None) -> list[ToolDefinition]:
    """Drop mutually-exclusive edit tools when both surfaces are available."""

    tool_names = {tool.name for tool in tools}
    has_patch = bool(tool_names & _APPLY_PATCH_TOOL_NAMES)
    has_exact = bool(tool_names & _EXACT_TOOL_NAMES)
    if not (has_patch and has_exact):
        return tools

    preferred = preferred_edit_tool_mode(model_id)
    keep_names = (
        _APPLY_PATCH_TOOL_NAMES if preferred is EditToolMode.APPLY_PATCH else _EXACT_TOOL_NAMES
    )
    filtered: list[ToolDefinition] = []
    for tool in tools:
        if tool.name in _APPLY_PATCH_TOOL_NAMES | _EXACT_TOOL_NAMES and tool.name not in keep_names:
            continue
        filtered.append(tool)
    return filtered


def _normalize_model_name(model_id: str | None) -> str:
    if not isinstance(model_id, str):
        return ""
    return model_id.strip().lower()


def prepare_tool_exposure(
    *,
    inventory_tools: list[ToolDefinition],
    controller_tool_schemas: list[dict[str, Any]],
    model_info: ModelInfo,
    contract: ToolExposureContract,
    promoted_tool_ids: set[str],
    default_visible_tool_ids: set[str] | None = None,
    allow_tool_search: bool = True,
) -> ToolExposureResult:
    """Prepare provider-specific model-facing tool schemas.

    The inventory is split into three logical sets:

    - **policy_visible**: tools that should be visible by default according to
      the step profile (``default_visible_tool_ids``).
    - **hidden_searchable**: tools in the searchable inventory that are not
      currently policy-visible.  These are the candidates for ``search_tools``
      discovery.
    - **promoted**: tools explicitly surfaced by ``search_tools`` or skill
      activation (``promoted_tool_ids``).  They must become visible on the next
      turn regardless of which hidden bucket they came from.

    Provider packing then decides how to fit these sets into the model-facing
    tool list given any slot cap.  The invariant is:

    - critical helpers always win slots
    - promoted tools always win slots after critical helpers
    - ``search_tools`` is included whenever hidden tools remain
    - remaining slots go to policy-visible non-MCP tools first, then MCP
    """
    controller_tool_schemas_with_search = list(controller_tool_schemas)
    controller_tool_schemas_without_search = [
        schema
        for schema in controller_tool_schemas_with_search
        if schema.get("function", {}).get("name") != SEARCH_TOOLS_TOOL.name
    ]
    request_kwargs: dict[str, Any] = {}

    effective_model_id = getattr(model_info, "model_id", None)
    edit_tool_family = detect_edit_tool_family(effective_model_id)
    edit_tool_mode = preferred_edit_tool_mode(effective_model_id)
    sorted_inventory = sorted(inventory_tools, key=_tool_sort_key)

    # Resolve policy-visible set.
    visible_defaults: set[str] = (
        default_visible_tool_ids
        if default_visible_tool_ids is not None
        else {stable_tool_id(tool) for tool in sorted_inventory if not _is_deferred_tool(tool)}
    )

    # Build the three logical sets.
    policy_visible_tools = [
        tool for tool in sorted_inventory if stable_tool_id(tool) in visible_defaults
    ]
    hidden_searchable_tools = [
        tool for tool in sorted_inventory if stable_tool_id(tool) not in visible_defaults
    ]

    # Promoted tools must be actually visible next turn regardless of policy
    # status.  A tool can be in ``visible_defaults`` (policy-visible) yet still
    # be hidden in practice because a provider slot cap truncated the visible
    # set.  We therefore include every inventory tool that is in
    # ``promoted_tool_ids`` — provider packing and ``_unique_tools`` handle
    # dedup against ``policy_visible_tools`` below.
    promoted_visible = [
        tool
        for tool in sorted_inventory
        if stable_tool_id(tool) in promoted_tool_ids
    ]
    policy_visible_tools = filter_edit_tools_for_model(policy_visible_tools, effective_model_id)
    hidden_searchable_tools = filter_edit_tools_for_model(hidden_searchable_tools, effective_model_id)
    promoted_visible = filter_edit_tools_for_model(promoted_visible, effective_model_id)

    # search_tools lives in the controller schemas, not the inventory.
    search_tool_schema_present = any(
        schema.get("function", {}).get("name") == SEARCH_TOOLS_TOOL.name
        for schema in controller_tool_schemas_with_search
    )

    max_tools = model_info.max_tools
    use_responses_api = contract.llm_api == LLMApiMode.RESPONSES
    discovery_enabled = (
        allow_tool_search and contract.discovery_mode == ToolDiscoveryMode.CONTROLLER_SEARCH
    )
    has_hidden = bool(hidden_searchable_tools)

    use_anthropic_defer = bool(
        not use_responses_api
        and discovery_enabled
        and model_info.supports_defer_loading
        and has_hidden
    )
    use_openai_controller_search_fallback = bool(
        use_responses_api
        and discovery_enabled
        and search_tool_schema_present
        and has_hidden
    )

    if not allow_tool_search:
        filtered_controller_tool_schemas = controller_tool_schemas_without_search
    elif not use_responses_api or use_openai_controller_search_fallback:
        filtered_controller_tool_schemas = controller_tool_schemas_with_search
    else:
        filtered_controller_tool_schemas = controller_tool_schemas_without_search

    alias_map: dict[str, str] = {
        schema.get("function", {}).get("name", ""): schema.get("function", {}).get("name", "")
        for schema in filtered_controller_tool_schemas
        if isinstance(schema.get("function", {}).get("name"), str)
    }
    controller_count = len(filtered_controller_tool_schemas)
    available_slots = None if max_tools is None else max(0, max_tools - controller_count)

    if use_anthropic_defer:
        # Anthropic: all tools in the array; hidden ones carry defer_loading=True.
        # Promoted tools (explicitly surfaced by search or skill activation) must not
        # be deferred — they should be immediately usable without a search call.
        # We place them between policy-visible and the remaining hidden tools so the
        # Anthropic cache breakpoint (placed at the last policy-visible tool) still
        # covers the stable prefix correctly.
        strategy = "anthropic_defer_loading"
        deferred_tool_ids = {stable_tool_id(tool) for tool in hidden_searchable_tools}
        deferred_tool_ids -= promoted_tool_ids
        promoted_non_policy = [
            tool
            for tool in hidden_searchable_tools
            if stable_tool_id(tool) in promoted_tool_ids
        ]
        remaining_hidden = [
            tool
            for tool in hidden_searchable_tools
            if stable_tool_id(tool) not in promoted_tool_ids
        ]
        visible_tools = policy_visible_tools + promoted_non_policy + remaining_hidden
        visible_tools = filter_edit_tools_for_model(visible_tools, effective_model_id)
        tool_schemas = _build_inventory_schemas(
            visible_tools,
            alias_map,
            deferred_tool_ids=deferred_tool_ids,
        )
        _mark_anthropic_cache_breakpoint(
            tool_schemas,
            stable_anchor_tool_ids={stable_tool_id(tool) for tool in policy_visible_tools},
        )
        request_kwargs = {
            "extra_headers": {"anthropic-beta": "tool-search-tool-2025-10-19"},
            "disable_parallel_tool_use": False,
        }

    elif use_openai_controller_search_fallback:
        # OpenAI Responses fallback: controller search_tools is the discovery
        # mechanism.  Under slot pressure we keep:
        #   1. critical helpers (always)
        #   2. promoted tools (always — they were explicitly requested)
        #   3. search_tools (always when hidden tools remain)
        #   4. remaining policy-visible non-MCP tools
        #   5. remaining policy-visible MCP tools
        strategy = "openai_responses_controller_search_fallback"
        visible_tools = _select_fallback_visible_tools(
            policy_visible_tools=policy_visible_tools,
            promoted_tools=promoted_visible,
            available_slots=available_slots,
            has_hidden=has_hidden,
        )
        visible_tools = filter_edit_tools_for_model(visible_tools, effective_model_id)
        tool_schemas = _build_inventory_schemas(visible_tools, alias_map)
        request_kwargs = {"tool_choice": "auto", "parallel_tool_calls": True}

    elif use_responses_api:
        # OpenAI Responses without discovery: show policy-visible + promoted.
        strategy = "openai_responses_visible_only"
        visible_tools = _unique_tools(policy_visible_tools + promoted_visible)
        visible_tools = filter_edit_tools_for_model(visible_tools, effective_model_id)
        tool_schemas = _build_inventory_schemas(visible_tools, alias_map)
        request_kwargs = {"tool_choice": "auto", "parallel_tool_calls": True}

    elif discovery_enabled:
        # Generic chat-completions with controller search_tools.
        strategy = "generic_search_tools"
        visible_tools = _select_fallback_visible_tools(
            policy_visible_tools=policy_visible_tools,
            promoted_tools=promoted_visible,
            available_slots=available_slots,
            has_hidden=has_hidden,
        )
        visible_tools = filter_edit_tools_for_model(visible_tools, effective_model_id)
        tool_schemas = _build_inventory_schemas(visible_tools, alias_map)

    else:
        # No discovery: show policy-visible + promoted only.
        strategy = "chat_visible_only"
        visible_tools = _unique_tools(policy_visible_tools + promoted_visible)
        visible_tools = filter_edit_tools_for_model(visible_tools, effective_model_id)
        tool_schemas = _build_inventory_schemas(visible_tools, alias_map)

    native_apply_patch_exposed = False
    if (
        use_responses_api
        and contract.native_apply_patch
        and any(tool.name == "apply_patch" for tool in visible_tools)
    ):
        tool_schemas = [
            schema
            for schema in tool_schemas
            if not (
                isinstance(schema.get("function"), dict)
                and schema["function"].get("name") == "apply_patch"
            )
        ]
        tool_schemas.append({"type": "apply_patch"})
        native_apply_patch_exposed = True

    visible_tool_ids = {stable_tool_id(tool) for tool in visible_tools}
    hidden_searchable_tool_ids = {
        stable_tool_id(tool)
        for tool in hidden_searchable_tools
        if stable_tool_id(tool) not in visible_tool_ids
    }
    promoted_inventory_ids = {stable_tool_id(tool) for tool in promoted_visible}
    promoted_visible_ids = visible_tool_ids & promoted_inventory_ids
    final_tool_schemas = _strip_internal_schema_metadata(tool_schemas)
    return ToolExposureResult(
        tools=[*filtered_controller_tool_schemas, *final_tool_schemas],
        alias_map=alias_map,
        request_kwargs=request_kwargs,
        visible_tool_ids=visible_tool_ids,
        hidden_searchable_tool_ids=hidden_searchable_tool_ids,
        debug_metadata={
            "strategy": strategy,
            "llm_api": str(contract.llm_api),
            "discovery_mode": str(contract.discovery_mode),
            "controller_tool_count": controller_count,
            "inventory_tool_count": len(sorted_inventory),
            "policy_visible_count": len(policy_visible_tools),
            "hidden_searchable_count": len(hidden_searchable_tools),
            # Tools the caller asked us to surface this turn.
            "promoted_requested_count": len(promoted_inventory_ids),
            # Tools that actually made it into the visible surface.  A drop
            # here indicates slot-cap pressure.
            "promoted_visible_count": len(promoted_visible_ids),
            "visible_tool_count": len(visible_tools),
            "max_tools": max_tools,
            "edit_tool_family": str(edit_tool_family),
            "edit_tool_mode": str(edit_tool_mode),
            "native_apply_patch_requested": contract.native_apply_patch,
            "native_apply_patch_exposed": native_apply_patch_exposed,
            "native_apply_patch_reason": contract.native_apply_patch_reason,
        },
    )


def _select_fallback_visible_tools(
    *,
    policy_visible_tools: list[ToolDefinition],
    promoted_tools: list[ToolDefinition],
    available_slots: int | None,
    has_hidden: bool,
) -> list[ToolDefinition]:
    """Select the actual visible tool set under a controller-search fallback cap.

    Priority order (highest first):
    1. Critical helpers (``skill_load``, ``read_tool_output*``)
    2. Promoted tools (explicitly surfaced by search or skill activation)
    3. Non-MCP policy-visible tools (builtins, executor tools)
    4. MCP policy-visible tools

    ``search_tools`` is handled by the controller schema layer, not here.
    The slot budget passed in already excludes the controller schema count.
    """
    if available_slots is None:
        return _unique_tools(policy_visible_tools + promoted_tools)

    critical = [t for t in policy_visible_tools if _is_critical_generic_tool(t)]
    promoted_set = {stable_tool_id(t) for t in promoted_tools}
    non_critical_policy = [
        t
        for t in policy_visible_tools
        if not _is_critical_generic_tool(t) and stable_tool_id(t) not in promoted_set
    ]
    non_mcp_policy = [t for t in non_critical_policy if not _is_mcp_tool(t)]
    mcp_policy = [t for t in non_critical_policy if _is_mcp_tool(t)]

    # Reserve one slot for search_tools when hidden tools remain.
    search_reserve = 1 if has_hidden else 0

    visible: list[ToolDefinition] = []

    for tool in critical:
        if len(visible) >= available_slots:
            break
        visible.append(tool)

    for tool in promoted_tools:
        if len(visible) >= available_slots:
            break
        visible.append(tool)

    remaining = max(0, available_slots - len(visible) - search_reserve)

    for tool in non_mcp_policy:
        if len(visible) >= available_slots - search_reserve:
            break
        visible.append(tool)
        remaining -= 1

    for tool in mcp_policy:
        if remaining <= 0 or len(visible) >= available_slots - search_reserve:
            break
        visible.append(tool)
        remaining -= 1

    return _unique_tools(visible)


def _build_inventory_schemas(
    tools: list[ToolDefinition],
    alias_map: dict[str, str],
    *,
    deferred_tool_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    deferred_tool_ids = deferred_tool_ids or set()
    used_names: set[str] = set(alias_map)
    schemas: list[dict[str, Any]] = []
    for tool in tools:
        visible_name = _dedupe_visible_name(
            _tool_visible_name(tool), stable_tool_id(tool), used_names
        )
        alias_map[visible_name] = tool.name
        function_schema: dict[str, Any] = {
            "name": visible_name,
            "description": tool.description,
            "parameters": tool.parameters,
            "x-stable-tool-id": stable_tool_id(tool),
        }
        if stable_tool_id(tool) in deferred_tool_ids:
            function_schema["defer_loading"] = True
        schemas.append({"type": "function", "function": function_schema})
    return schemas


def _mark_anthropic_cache_breakpoint(
    tool_schemas: list[dict[str, Any]],
    *,
    stable_anchor_tool_ids: set[str],
) -> None:
    """Attach Anthropic tool-array cache_control to a stable schema edge.

    Prefer the last non-deferred core tool because deferred/discovered tools can
    be appended or reordered across turns as the model explores the inventory.
    """

    if not tool_schemas:
        return

    anchor_index = len(tool_schemas) - 1
    for index in range(len(tool_schemas) - 1, -1, -1):
        function = tool_schemas[index].get("function")
        if not isinstance(function, dict):
            continue
        tool_id = function.get("x-stable-tool-id")
        if isinstance(tool_id, str) and tool_id in stable_anchor_tool_ids:
            anchor_index = index
            break

    function = tool_schemas[anchor_index].get("function")
    if isinstance(function, dict):
        function["cache_control"] = {"type": "ephemeral"}


def _strip_internal_schema_metadata(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for schema in tool_schemas:
        sanitized_schema = dict(schema)
        function = sanitized_schema.get("function")
        if isinstance(function, dict) and "x-stable-tool-id" in function:
            function = dict(function)
            function.pop("x-stable-tool-id", None)
            sanitized_schema["function"] = function
        sanitized.append(sanitized_schema)
    return sanitized


def _build_openai_deferred_namespaces(
    tools: list[ToolDefinition], alias_map: dict[str, str]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[ToolDefinition]] = {}
    for tool in tools:
        grouped.setdefault(_openai_namespace_key(tool), []).append(tool)

    namespaces: list[dict[str, Any]] = []
    used_namespace_names: set[str] = set(alias_map)
    for namespace_key in sorted(grouped):
        group_tools = grouped[namespace_key]
        namespace_name = _dedupe_visible_name(
            _sanitize_visible_name(namespace_key), namespace_key, used_namespace_names
        )
        namespace_tools: list[dict[str, Any]] = []
        used_names: set[str] = set(alias_map)
        for tool in group_tools:
            visible_name = _dedupe_visible_name(
                _tool_visible_name(tool), stable_tool_id(tool), used_names
            )
            alias_map[visible_name] = tool.name
            namespace_tools.append(
                {
                    "type": "function",
                    "name": visible_name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "defer_loading": True,
                }
            )
        namespaces.append(
            {
                "type": "namespace",
                "name": namespace_name,
                "description": _openai_namespace_description(group_tools[0]),
                "tools": namespace_tools,
            }
        )
    return namespaces


def _openai_allowed_tools_choice(visible_schemas: list[dict[str, Any]]) -> dict[str, Any] | str:
    return {
        "type": "allowed_tools",
        "mode": "auto",
        "tools": [
            *[
                {"type": "function", "name": function_name}
                for schema in visible_schemas
                if isinstance(schema.get("function"), dict)
                and isinstance((function_name := schema["function"].get("name")), str)
            ]
        ],
    }


def _openai_namespace_key(tool: ToolDefinition) -> str:
    if tool.source.type == "skill":
        return f"skill_{tool.source.skill_id or 'skill'}"
    server_name = tool.source.server_name or tool.source.server_id or "server"
    return f"mcp_{server_name}"


def _openai_namespace_description(tool: ToolDefinition) -> str:
    if tool.source.type == "skill":
        skill_id = tool.source.skill_id or "skill"
        return f"Deferred tools loaded from skill '{skill_id}'."
    server_name = tool.source.server_name or tool.source.server_id or "server"
    return f"Deferred tools loaded from MCP server '{server_name}'."


def _unique_tools(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    seen: set[str] = set()
    unique: list[ToolDefinition] = []
    for tool in tools:
        tool_id = stable_tool_id(tool)
        if tool_id in seen:
            continue
        seen.add(tool_id)
        unique.append(tool)
    return unique


def _dedupe_visible_name(name: str, tool_id: str, used_names: set[str]) -> str:
    if name not in used_names:
        used_names.add(name)
        return name
    suffix = hashlib.sha1(tool_id.encode()).hexdigest()[:6]
    trimmed = name[: max(1, 64 - len(suffix) - 2)]
    deduped = f"{trimmed}__{suffix}"
    used_names.add(deduped)
    return deduped


def _is_deferred_tool(tool: ToolDefinition) -> bool:
    return tool.source.type in {"skill", "local_mcp", "intaris_mcp"}


def _is_mcp_tool(tool: ToolDefinition) -> bool:
    return tool.source.type in {"local_mcp", "intaris_mcp"}


def _tool_visible_name(tool: ToolDefinition) -> str:
    if tool.source.type == "skill" and tool.source.raw_tool_name:
        return _sanitize_visible_name(tool.source.raw_tool_name)
    return tool.name


def _sanitize_visible_name(name: str) -> str:
    cleaned = _VISIBLE_TOOL_NAME_PATTERN.sub("_", name).strip("_")
    if not cleaned:
        return "tool"
    if len(cleaned) <= _MAX_VISIBLE_TOOL_NAME_LENGTH:
        return cleaned
    suffix = hashlib.sha1(name.encode()).hexdigest()[:8]
    trimmed = cleaned[: _MAX_VISIBLE_TOOL_NAME_LENGTH - len(suffix) - 1].rstrip("_")
    return f"{trimmed}_{suffix}"


def _tool_sort_key(tool: ToolDefinition) -> tuple[int, int, int, int, str]:
    profile_group_priority = {
        "system": 0,
        "memory": 1,
        "filesystem": 2,
        "shell": 3,
        "web": 4,
        "browser": 5,
        "office": 6,
        "personal": 7,
        "communication": 8,
        "development": 9,
    }
    category_priority = {
        "system": 0,
        "context": 1,
        "skill": 2,
        "datetime": 3,
        "workflow": 4,
        "tool_output": 5,
        "artifact": 6,
        "schedule": 7,
        "memory": 8,
        "image": 9,
        "filesystem": 10,
        "search": 11,
        "shell": 12,
        "web": 13,
        "mcp": 14,
    }
    source_priority = {
        "builtin": 0,
        "executor": 1,
        "skill": 2,
        "local_mcp": 3,
        "intaris_mcp": 4,
    }
    return (
        0 if _is_critical_generic_tool(tool) else 1,
        profile_group_priority.get(tool_profile_group(tool), 50),
        category_priority.get(tool.category, 50),
        source_priority.get(tool.source.type, 50),
        stable_tool_id(tool),
    )


def _is_critical_generic_tool(tool: ToolDefinition) -> bool:
    return tool.name in _CRITICAL_GENERIC_TOOL_NAMES

"""Provider-aware tool exposure for model-facing tool schemas."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

from cognis.models.config import ModelInfo
from cognis.models.tool import ToolDefinition, stable_tool_id
from cognis.providers.llm.responses_bridge import should_use_openai_responses
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL

_ANTHROPIC_MODEL_PATTERNS = re.compile(r"(claude|anthropic)", re.IGNORECASE)


@dataclass(slots=True)
class ToolExposureResult:
    """Prepared model-facing tools and related metadata."""

    tools: list[dict[str, Any]]
    alias_map: dict[str, str]
    request_kwargs: dict[str, Any] = field(default_factory=dict)
    visible_tool_ids: set[str] = field(default_factory=set)
    debug_metadata: dict[str, Any] = field(default_factory=dict)


def prepare_tool_exposure(
    *,
    inventory_tools: list[ToolDefinition],
    controller_tool_schemas: list[dict[str, Any]],
    model: str,
    model_info: ModelInfo,
    discovered_tool_ids: set[str],
) -> ToolExposureResult:
    """Prepare provider-specific model-facing tool schemas."""

    filtered_controller_tool_schemas = list(controller_tool_schemas)
    if use_openai_responses := should_use_openai_responses(
        model=model,
        model_info=model_info,
        rollout_mode=os.getenv("COGNIS_OPENAI_RESPONSES_MODE", "auto").strip().lower(),
    ):
        filtered_controller_tool_schemas = [
            schema
            for schema in controller_tool_schemas
            if schema.get("function", {}).get("name") != SEARCH_TOOLS_TOOL.name
        ]

    alias_map = {
        schema.get("function", {}).get("name", ""): schema.get("function", {}).get("name", "")
        for schema in filtered_controller_tool_schemas
        if isinstance(schema.get("function", {}).get("name"), str)
    }
    request_kwargs: dict[str, Any] = {}
    controller_count = len(filtered_controller_tool_schemas)
    sorted_inventory = sorted(inventory_tools, key=_tool_sort_key)
    core_tools = [tool for tool in sorted_inventory if not _is_deferred_tool(tool)]
    deferred_tools = [tool for tool in sorted_inventory if _is_deferred_tool(tool)]
    search_tool = next((tool for tool in core_tools if tool.name == SEARCH_TOOLS_TOOL.name), None)
    core_without_search = [tool for tool in core_tools if tool.name != SEARCH_TOOLS_TOOL.name]
    discovered_visible = [
        tool for tool in deferred_tools if stable_tool_id(tool) in discovered_tool_ids
    ]
    max_tools = model_info.max_tools
    available_slots = None if max_tools is None else max(0, max_tools - controller_count)
    use_anthropic_defer = bool(
        model_info.supports_defer_loading or _ANTHROPIC_MODEL_PATTERNS.search(model)
    )
    if use_anthropic_defer:
        strategy = "anthropic_defer_loading"
        visible_tools = core_without_search + deferred_tools
        tool_schemas = _build_inventory_schemas(
            visible_tools,
            alias_map,
            deferred_tool_ids={stable_tool_id(tool) for tool in deferred_tools},
        )
        if tool_schemas:
            tool_schemas[-1]["function"]["cache_control"] = {"type": "ephemeral"}
        request_kwargs = {"extra_headers": {"anthropic-beta": "tool-search-tool-2025-10-19"}}
    elif use_openai_responses:
        strategy = "openai_responses_full_inventory"
        visible_tools = core_without_search + deferred_tools
        tool_schemas = _build_inventory_schemas(visible_tools, alias_map)
        request_kwargs = {"tool_choice": "auto", "parallel_tool_calls": True}
    else:
        strategy = "generic_search_tools"
        base_tools, overflowed = _select_generic_visible_tools(
            core_tools=core_without_search,
            discovered_tools=discovered_visible,
            search_tool=search_tool,
            available_slots=available_slots,
            deferred_present=bool(deferred_tools),
        )
        visible_tools = base_tools
        tool_schemas = _build_inventory_schemas(visible_tools, alias_map)
        request_kwargs = {}
        if overflowed and tool_schemas and max_tools is not None:
            tool_schemas = tool_schemas[:available_slots]

    visible_tool_ids = {stable_tool_id(tool) for tool in visible_tools}
    return ToolExposureResult(
        tools=[*filtered_controller_tool_schemas, *tool_schemas],
        alias_map=alias_map,
        request_kwargs=request_kwargs,
        visible_tool_ids=visible_tool_ids,
        debug_metadata={
            "strategy": strategy,
            "model": model,
            "controller_tool_count": controller_count,
            "inventory_tool_count": len(sorted_inventory),
            "core_tool_count": len(core_tools),
            "deferred_tool_count": len(deferred_tools),
            "visible_tool_count": len(visible_tools),
            "discovered_tool_count": len(discovered_visible),
            "max_tools": max_tools,
        },
    )


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
        visible_name = _dedupe_visible_name(tool.name, stable_tool_id(tool), used_names)
        alias_map[visible_name] = tool.name
        function_schema: dict[str, Any] = {
            "name": visible_name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        if stable_tool_id(tool) in deferred_tool_ids:
            function_schema["defer_loading"] = True
        schemas.append({"type": "function", "function": function_schema})
    return schemas


def _select_generic_visible_tools(
    *,
    core_tools: list[ToolDefinition],
    discovered_tools: list[ToolDefinition],
    search_tool: ToolDefinition | None,
    available_slots: int | None,
    deferred_present: bool,
) -> tuple[list[ToolDefinition], bool]:
    if available_slots is None:
        all_visible = list(core_tools)
        if deferred_present and search_tool is not None:
            all_visible.append(search_tool)
        all_visible.extend(discovered_tools)
        return _unique_tools(all_visible), False

    visible: list[ToolDefinition] = []
    overflowed = False
    must_include_search = (
        deferred_present or len(core_tools) + len(discovered_tools) > available_slots
    )
    reserved_for_search = (
        1 if must_include_search and search_tool is not None and available_slots > 0 else 0
    )
    core_budget = max(0, available_slots - reserved_for_search)
    for tool in core_tools:
        if len(visible) >= core_budget:
            overflowed = True
            break
        visible.append(tool)
    if reserved_for_search and search_tool is not None:
        visible.append(search_tool)
    remaining = max(0, available_slots - len(visible))
    for tool in discovered_tools:
        if remaining <= 0:
            overflowed = True
            break
        visible.append(tool)
        remaining -= 1
    if len(core_tools) + len(discovered_tools) > len(visible):
        overflowed = True
    return _unique_tools(visible), overflowed


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
    return tool.source.type in {"local_mcp", "intaris_mcp"}


def _tool_sort_key(tool: ToolDefinition) -> tuple[int, int, str]:
    category_priority = {
        "system": 0,
        "workflow": 1,
        "memory": 2,
        "tool_output": 3,
        "image": 4,
        "filesystem": 5,
        "search": 6,
        "shell": 7,
        "web": 8,
        "mcp": 9,
    }
    source_priority = {
        "builtin": 0,
        "executor": 1,
        "skill": 2,
        "local_mcp": 3,
        "intaris_mcp": 4,
    }
    return (
        category_priority.get(tool.category, 50),
        source_priority.get(tool.source.type, 50),
        stable_tool_id(tool),
    )

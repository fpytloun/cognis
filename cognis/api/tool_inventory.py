"""Helpers for read-only tool inventory API responses."""

from __future__ import annotations

from typing import Any

from cognis.models.tool import (
    ToolDefinition,
    ToolSource,
    sanitize_mcp_tool_name,
    stable_tool_id,
)


def extract_intaris_aggregated_server_name(row: dict[str, Any]) -> str | None:
    """Extract the canonical Intaris server name from an aggregated row."""
    source = row.get("source")
    if isinstance(source, dict):
        server_name = source.get("server_name") or source.get("server")
    else:
        server_name = row.get("server_name") or row.get("server")
    resolved_server = str(server_name).strip() if isinstance(server_name, str) else None
    return resolved_server or None


def extract_intaris_aggregated_raw_tool_name(row: dict[str, Any]) -> str | None:
    """Extract the canonical raw Intaris MCP tool name from an aggregated row."""
    source = row.get("source")
    if isinstance(source, dict):
        raw_tool_name = source.get("raw_tool_name") or source.get("tool")
    else:
        raw_tool_name = row.get("raw_tool_name") or row.get("tool") or row.get("name")
    resolved_tool = str(raw_tool_name).strip() if isinstance(raw_tool_name, str) else None
    return resolved_tool or None


def build_intaris_tool_definition(
    *,
    server_name: str,
    raw_tool_name: str,
    payload: dict[str, Any],
) -> ToolDefinition:
    """Build a normalized Intaris MCP tool definition from aggregated metadata."""
    tool_name = sanitize_mcp_tool_name(server_name, raw_tool_name)
    annotations = payload.get("annotations") if isinstance(payload.get("annotations"), dict) else {}
    return ToolDefinition(
        name=tool_name,
        description=str(payload.get("description", f"Intaris MCP tool {tool_name}")),
        parameters=payload.get("inputSchema") or payload.get("parameters") or {},
        annotations=annotations,
        output_schema=(
            payload.get("outputSchema") if isinstance(payload.get("outputSchema"), dict) else None
        ),
        source=ToolSource(
            type="intaris_mcp",
            server_name=server_name,
            raw_tool_name=raw_tool_name,
        ),
        category="mcp",
        content_trust="untrusted",
        timeout_seconds=30,
    )


def collect_unique_observed_local_mcp_tools(
    observed_tools: list[dict[str, Any]] | None,
) -> list[ToolDefinition]:
    """Return unique cached local MCP tools from serialized executor inventories."""
    if not observed_tools:
        return []

    unique: dict[str, ToolDefinition] = {}
    for item in observed_tools:
        if not isinstance(item, dict):
            continue
        try:
            tool = ToolDefinition.model_validate(item)
        except Exception:
            continue
        if tool.source.type != "local_mcp":
            continue
        unique.setdefault(stable_tool_id(tool), tool)
    return list(unique.values())

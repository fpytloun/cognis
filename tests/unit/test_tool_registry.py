from __future__ import annotations

import pytest

from cognis.models.tool import ToolDefinition, ToolSource
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _tool(name: str, source_type: str) -> RegisteredTool:
    return RegisteredTool(
        definition=ToolDefinition(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            source=ToolSource(type=source_type, server_name="server" if "/" in name else None),
        )
    )


def test_registry_merge_prefers_higher_priority_source() -> None:
    builtin = ToolRegistry()
    builtin.register(_tool("filesystem/read_file", "builtin"))
    local = ToolRegistry()
    local.register(_tool("filesystem/read_file", "local_mcp"))

    merged = local.merge(builtin)

    tool = merged.get("filesystem/read_file")
    assert tool is not None
    assert tool.definition.source.type == "builtin"


def test_registry_merge_rejects_same_source_duplicates() -> None:
    first = ToolRegistry()
    first.register(_tool("filesystem/read_file", "local_mcp"))
    second = ToolRegistry()
    second.register(_tool("filesystem/read_file", "local_mcp"))

    with pytest.raises(ValueError):
        first.merge(second)

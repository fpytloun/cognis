"""Executor-native LSP query tool."""

from __future__ import annotations

import os
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext

_LSP_TOOL_OPERATIONS = {
    "goToDefinition": "definition",
    "findReferences": "references",
    "hover": "hover",
    "documentSymbol": "document_symbol",
    "workspaceSymbol": "workspace_symbol",
    "goToImplementation": "implementation",
}


async def handle_lsp(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Run an LSP query against the current workspace."""

    operation = str(arguments.get("operation") or "")
    file_path = str(arguments.get("file_path") or "")
    query = str(arguments.get("query") or "")
    line_value = arguments.get("line")
    character_value = arguments.get("character")

    if operation not in _LSP_TOOL_OPERATIONS:
        return ToolResult(output=f"Unsupported LSP operation: {operation}", is_error=True)
    if operation == "workspaceSymbol" and not query.strip():
        return ToolResult(output="workspaceSymbol requires a non-empty query.", is_error=True)
    if operation in {"goToDefinition", "findReferences", "hover", "goToImplementation"}:
        if line_value is None or character_value is None:
            return ToolResult(
                output=f"{operation} requires both line and character arguments.",
                is_error=True,
            )
        line = max(1, int(line_value))
        character = max(1, int(character_value))
    else:
        line = 1
        character = 1

    lsp = context.runtime_metadata.get("lsp_manager")
    if lsp is None or not hasattr(lsp, "touch_file") or not hasattr(lsp, "has_clients"):
        return ToolResult(output="LSP is not available in this executor.", is_error=True)

    resolved_path = str(resolve_path(file_path))
    if not os.path.exists(resolved_path):
        return ToolResult(output=f"Path does not exist: {file_path}", is_error=True)
    if not os.path.isfile(resolved_path):
        return ToolResult(output=f"Not a file: {file_path}", is_error=True)

    await lsp.touch_file(resolved_path, wait=True)
    if not await lsp.has_clients(resolved_path):
        return ToolResult(output="No LSP server available for this file type.", is_error=True)
    method_name = _LSP_TOOL_OPERATIONS[operation]
    if operation == "workspaceSymbol":
        result = await getattr(lsp, method_name)(resolved_path, query)
    elif operation == "documentSymbol":
        result = await getattr(lsp, method_name)(resolved_path)
    else:
        result = await getattr(lsp, method_name)(resolved_path, line - 1, character - 1)

    title = f"{operation} {resolved_path}:{line}:{character}"
    if not result:
        return ToolResult(output=f"No results found for {operation}.", metadata={"result": []})
    return ToolResult(
        output=_format_lsp_result(result), metadata={"result": result, "title": title}
    )


def _format_lsp_result(result: list[dict[str, Any]]) -> str:
    return _truncate_result_lines(result)


def _truncate_result_lines(result: list[dict[str, Any]]) -> str:
    import json

    return json.dumps(result, indent=2)

const MCP_VISIBLE_TOOL_SEPARATOR = '__';

export function displayToolName(toolName: string): string {
  if (!toolName.startsWith('mcp_')) return toolName;

  const separatorIndex = toolName.indexOf(MCP_VISIBLE_TOOL_SEPARATOR);
  if (separatorIndex < 0) return toolName;

  const visibleName = toolName.slice(separatorIndex + MCP_VISIBLE_TOOL_SEPARATOR.length);
  return visibleName || toolName;
}

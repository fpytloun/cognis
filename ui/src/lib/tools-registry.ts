import type { ToolDefinitionSummary, ToolSource } from '$lib/types/api';

export interface ToolsFilters {
  searchQuery: string;
  sourceFilter: string;
  categoryFilter: string;
}

export interface ToolGroup {
  category: string;
  tools: ToolDefinitionSummary[];
  sourceTypes: string[];
}

export interface RegistryLoadState {
  staticTools: boolean;
  intarisMcpTools: boolean;
  observedLocalMcpTools: boolean;
  skills: boolean;
  executors: boolean;
  mcpServers: boolean;
}

export const CATEGORY_ORDER = ['memory', 'filesystem', 'shell', 'web', 'mcp', 'workflow', 'orchestration', 'system'];

export function getSourceType(tool: ToolDefinitionSummary): string {
  return tool.source?.type || 'unknown';
}

export function getSourceLabel(sourceType: string): string {
  switch (sourceType) {
    case 'executor':
      return 'Executor';
    case 'builtin':
      return 'Built-in';
    case 'local_mcp':
      return 'Local MCP';
    case 'intaris_mcp':
      return 'Intaris MCP';
    case 'skill':
      return 'Skill';
    default:
      return sourceType;
  }
}

export function getToolKey(tool: ToolDefinitionSummary): string {
  const source = tool.source;
  if (source.type === 'local_mcp' || source.type === 'intaris_mcp') {
    return `${source.type}:${source.server_id || source.server_name || 'unknown'}:${source.raw_tool_name || tool.name}`;
  }
  if (source.type === 'skill') {
    return `skill:${source.skill_id || 'unknown'}:${tool.name}`;
  }
  return `${source.type}:${tool.name}`;
}

export function mergeToolInventories(toolSets: ToolDefinitionSummary[][]): ToolDefinitionSummary[] {
  const merged = new Map<string, ToolDefinitionSummary>();
  for (const toolSet of toolSets) {
    for (const tool of toolSet) {
      merged.set(getToolKey(tool), tool);
    }
  }
  return Array.from(merged.values());
}

export function filterTools(tools: ToolDefinitionSummary[], filters: ToolsFilters): ToolDefinitionSummary[] {
  const query = filters.searchQuery.trim().toLowerCase();
  return tools.filter((tool) => {
    const matchesSearch = !query
      || tool.name.toLowerCase().includes(query)
      || tool.description.toLowerCase().includes(query);
    const matchesSource = filters.sourceFilter === 'all' || getSourceType(tool) === filters.sourceFilter;
    const matchesCategory = filters.categoryFilter === 'all' || tool.category === filters.categoryFilter;
    return matchesSearch && matchesSource && matchesCategory;
  });
}

export function groupToolsByCategory(tools: ToolDefinitionSummary[]): ToolGroup[] {
  const grouped = new Map<string, ToolDefinitionSummary[]>();
  for (const tool of tools) {
    const group = grouped.get(tool.category) || [];
    group.push(tool);
    grouped.set(tool.category, group);
  }

  const orderedCategories = [
    ...CATEGORY_ORDER.filter((category) => grouped.has(category)),
    ...Array.from(grouped.keys()).filter((category) => !CATEGORY_ORDER.includes(category)).sort()
  ];

  return orderedCategories.map((category) => {
    const categoryTools = (grouped.get(category) || []).slice().sort((left, right) => {
      const sourceCompare = getSourceLabel(getSourceType(left)).localeCompare(getSourceLabel(getSourceType(right)));
      if (sourceCompare !== 0) return sourceCompare;
      return left.name.localeCompare(right.name);
    });
    const sourceTypes = Array.from(new Set(categoryTools.map(getSourceType))).sort((left, right) => getSourceLabel(left).localeCompare(getSourceLabel(right)));
    return { category, tools: categoryTools, sourceTypes };
  });
}

export function formatSourceSummary(sourceTypes: string[]): string {
  if (sourceTypes.length === 0) return '';
  if (sourceTypes.length === 1) return getSourceLabel(sourceTypes[0]);
  return sourceTypes.map(getSourceLabel).join(' + ');
}

export function isCachedObservedTool(source: ToolSource): boolean {
  return source.type === 'local_mcp';
}

export function buildRegistryWarnings(state: RegistryLoadState): string[] {
  const warnings: string[] = [];
  if (!state.staticTools) warnings.push('Static tool registry failed to load.');
  if (!state.intarisMcpTools) warnings.push('Intaris MCP tools are unavailable right now.');
  if (!state.observedLocalMcpTools) warnings.push('Cached local MCP tool inventory is unavailable right now.');
  if (!state.skills) warnings.push('Skills failed to load.');
  if (!state.executors) warnings.push('Executors failed to load.');
  if (!state.mcpServers) warnings.push('Configured MCP servers failed to load.');
  return warnings;
}

import { describe, expect, it } from 'vitest';

import {
  buildRegistryWarnings,
  filterTools,
  filterMcpTools,
  formatMcpCommand,
  formatSourceSummary,
  getToolKey,
  groupToolsByCategory,
  groupToolsByServer,
  mergeToolInventories
} from '$lib/tools-registry';
import type { ToolDefinitionSummary } from '$lib/types/api';

function tool(overrides: Partial<ToolDefinitionSummary> & Pick<ToolDefinitionSummary, 'name'>): ToolDefinitionSummary {
  return {
    name: overrides.name,
    description: overrides.description || overrides.name,
    parameters: overrides.parameters || { type: 'object', properties: {} },
    category: overrides.category || 'mcp',
    read_only: overrides.read_only ?? true,
    source: overrides.source || { type: 'builtin' },
    timeout_seconds: overrides.timeout_seconds ?? 30,
    non_bypassable: overrides.non_bypassable ?? false
  };
}

describe('tools registry helpers', () => {
  it('keeps local and intaris MCP variants as separate rows', () => {
    const local = tool({
      name: 'mcp_github__search',
      source: { type: 'local_mcp', server_name: 'github', raw_tool_name: 'search' }
    });
    const intaris = tool({
      name: 'mcp_github__search',
      source: { type: 'intaris_mcp', server_name: 'github', raw_tool_name: 'search' }
    });

    expect(getToolKey(local)).not.toBe(getToolKey(intaris));
    expect(mergeToolInventories([[local], [intaris]])).toHaveLength(2);
  });

  it('collapses duplicate local MCP observations across executors', () => {
    const first = tool({
      name: 'mcp_github__search',
      source: { type: 'local_mcp', server_id: 'srv-1', server_name: 'github', raw_tool_name: 'search' }
    });
    const second = tool({
      name: 'mcp_github__search',
      description: 'same tool from another executor',
      source: { type: 'local_mcp', server_id: 'srv-1', server_name: 'github', raw_tool_name: 'search' }
    });

    const merged = mergeToolInventories([[first], [second]]);
    expect(merged).toHaveLength(1);
  });

  it('groups mixed-source categories without losing source visibility', () => {
    const groups = groupToolsByCategory([
      tool({ name: 'read', category: 'filesystem', source: { type: 'executor' } }),
      tool({ name: 'mcp_github__search', category: 'mcp', source: { type: 'local_mcp', server_name: 'github', raw_tool_name: 'search' } }),
      tool({ name: 'mcp_linear__search', category: 'mcp', source: { type: 'intaris_mcp', server_name: 'linear', raw_tool_name: 'search' } })
    ]);

    expect(groups[0]?.category).toBe('filesystem');
    expect(groups[1]?.category).toBe('mcp');
    // sorted by label: "Executor MCP" < "Intaris MCP"
    expect(groups[1]?.sourceTypes).toEqual(['local_mcp', 'intaris_mcp']);
    expect(formatSourceSummary(groups[1]?.sourceTypes || [])).toBe('Executor MCP + Intaris MCP');
  });

  it('groups MCP tools by server name', () => {
    const tools = [
      tool({ name: 'mcp_github__search', source: { type: 'intaris_mcp', server_name: 'github', raw_tool_name: 'search' } }),
      tool({ name: 'mcp_github__create', source: { type: 'intaris_mcp', server_name: 'github', raw_tool_name: 'create' } }),
      tool({ name: 'mcp_linear__list', source: { type: 'intaris_mcp', server_name: 'linear', raw_tool_name: 'list' } })
    ];

    const groups = groupToolsByServer(tools);
    expect(groups).toHaveLength(2);
    expect(groups[0]?.serverName).toBe('github');
    expect(groups[0]?.tools).toHaveLength(2);
    expect(groups[1]?.serverName).toBe('linear');
    expect(groups[1]?.tools).toHaveLength(1);
  });

  it('filters tools by search query including server name', () => {
    const tools = [
      tool({ name: 'mcp_github__search', description: 'Search issues', source: { type: 'intaris_mcp', server_name: 'github', raw_tool_name: 'search' } }),
      tool({ name: 'mcp_linear__list', description: 'List items', source: { type: 'intaris_mcp', server_name: 'linear', raw_tool_name: 'list' } })
    ];

    expect(filterMcpTools(tools, 'github')).toHaveLength(1);
    expect(filterMcpTools(tools, 'list')).toHaveLength(1);
    expect(filterMcpTools(tools, '')).toHaveLength(2);
  });

  it('filters by search and category together', () => {
    const tools = [
      tool({ name: 'read', category: 'filesystem', source: { type: 'executor' } }),
      tool({ name: 'mcp_github__search', description: 'GitHub issues', category: 'mcp', source: { type: 'local_mcp', server_name: 'github', raw_tool_name: 'search' } })
    ];

    const filtered = filterTools(tools, {
      searchQuery: 'git',
      categoryFilter: 'mcp'
    });

    expect(filtered).toHaveLength(1);
    expect(filtered[0]?.name).toBe('mcp_github__search');
  });

  it('formats MCP command with args', () => {
    expect(formatMcpCommand('npx', ['-y', '@doist/todoist-ai'])).toBe('npx -y @doist/todoist-ai');
    expect(formatMcpCommand('npx', [])).toBe('npx');
    expect(formatMcpCommand(null, [])).toBe('');
  });

  it('builds partial-failure warnings per data source', () => {
    expect(buildRegistryWarnings({
      staticTools: true,
      intarisMcpTools: false,
      observedLocalMcpTools: false,
      skills: true,
      executors: false,
      mcpServers: true
    })).toEqual([
      'Intaris MCP tools are unavailable right now.',
      'Cached local MCP tool inventory is unavailable right now.',
      'Executors failed to load.'
    ]);
  });
});

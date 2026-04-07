import { describe, expect, it } from 'vitest';

import {
  executorRuntimeBadgeStatus,
  executorRuntimeLabel,
  executorRuntimeSummary,
  validateStdioCommand
} from '$lib/executors';
import type { ExecutorConfig } from '$lib/types/api';

function executor(overrides: Partial<ExecutorConfig> = {}): ExecutorConfig {
  return {
    executor_id: 'exec-1',
    name: 'Exec',
    executor_type: 'websocket',
    labels: {},
    enabled_tools: [],
    enabled_tool_groups: [],
    config: {},
    status: 'active',
    runtime_state: 'active',
    desired_config_version: 1,
    applied_config_version: 1,
    runtime_metadata: {},
    last_observed_at: null,
    is_default: false,
    owner_email: 'user@example.com',
    created_at: null,
    updated_at: null,
    ...overrides
  };
}

describe('executor helpers', () => {
  it('maps degraded and stale runtime states to degraded labels', () => {
    expect(executorRuntimeBadgeStatus(executor({ runtime_state: 'degraded' }))).toBe('degraded');
    expect(executorRuntimeLabel(executor({ runtime_state: 'stale' }))).toBe('pending reconfigure');
  });

  it('summarizes degraded MCP server names', () => {
    const summary = executorRuntimeSummary(
      executor({
        runtime_state: 'degraded',
        runtime_metadata: {
          mcp_servers: [
            { name: 'todoist', status: 'failed', phase: 'initialize' },
            { name: 'github', status: 'ready', phase: 'ready' }
          ]
        }
      })
    );

    expect(summary).toContain('todoist');
  });

  it('rejects shell-style stdio commands with spaces', () => {
    expect(validateStdioCommand('npx -y @doist/todoist-ai')).toContain('Command must be only the executable');
    expect(validateStdioCommand('npx')).toBeNull();
  });
});

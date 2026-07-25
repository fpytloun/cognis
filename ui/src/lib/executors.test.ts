import { describe, expect, it } from 'vitest';

import {
  executorDegradedDetails,
  executorHealth,
  executorRuntimeBadgeStatus,
  executorRuntimeLabel,
  executorRuntimeSummary,
  providerInferenceExecutors,
  providerSelectorCapabilityWarning,
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
    local_inference_enabled: true,
    ollama_management_enabled: true,
    ollama_port: 11434,
    ollama_endpoint: 'http://127.0.0.1:11434',
    local_inference_config_status: 'confirmed',
    status: 'active',
    runtime_state: 'active',
    desired_config_version: 1,
    applied_config_version: 1,
    runtime_metadata: {},
    resource_snapshot: null,
    last_observed_at: null,
    is_default: false,
    shared: false,
    owner_email: 'user@example.com',
    created_at: null,
    updated_at: null,
    ...overrides
  };
}

describe('executor helpers', () => {
  it('excludes local-inference-disabled executors except an existing saved reference', () => {
    const enabled = executor({ executor_id: 'enabled' });
    const disabled = executor({ executor_id: 'disabled', local_inference_enabled: false });
    const subprocess = executor({ executor_id: 'subprocess', executor_type: 'subprocess' });

    expect(providerInferenceExecutors([enabled, disabled, subprocess]).map((item) => item.executor_id)).toEqual([
      'enabled'
    ]);
    expect(
      providerInferenceExecutors([enabled, disabled], 'disabled').map((item) => item.executor_id)
    ).toEqual(['enabled', 'disabled']);
  });

  it('warns only when a saved label selector has disabled matches', () => {
    const disabled = executor({
      executor_id: 'disabled',
      labels: { gpu: 'nvidia' },
      local_inference_enabled: false
    });
    const enabled = executor({
      executor_id: 'enabled',
      labels: { gpu: 'nvidia' }
    });

    expect(providerSelectorCapabilityWarning([disabled], '', { gpu: 'nvidia' })).toContain(
      'matches only executors with local inference disabled'
    );
    expect(providerSelectorCapabilityWarning([disabled, enabled], '', { gpu: 'nvidia' })).toBeNull();
  });

  it('maps degraded and stale runtime states to degraded labels', () => {
    expect(executorRuntimeBadgeStatus(executor({ runtime_state: 'degraded' }))).toBe('degraded');
    expect(executorRuntimeLabel(executor({ runtime_state: 'stale' }))).toBe('pending reconfigure');
  });

  it('prioritizes degraded runtime state and keeps missing telemetry unknown', () => {
    expect(executorHealth(executor({ runtime_state: 'degraded', resource_snapshot: null })).state).toBe(
      'pressure'
    );
    expect(executorHealth(executor({ runtime_state: 'active', resource_snapshot: null })).state).toBe(
      'unknown'
    );
  });

  it('reports stale and critical current resource health in plain language', () => {
    expect(
      executorHealth(
        executor({
          resource_snapshot: {
            schema_version: 1,
            observed_at: '2026-07-13T10:00:00Z',
            freshness: { age_seconds: 180, stale_after_seconds: 120, stale: true },
            os: 'linux',
            arch: 'x86_64',
            cpu: null,
            memory: null,
            accelerators: null,
            ollama_model_store: null,
            ollama: null,
            runtime: null
          }
        })
      ).state
    ).toBe('stale');

    const critical = executorHealth(
      executor({
        resource_snapshot: {
          schema_version: 1,
          observed_at: '2026-07-13T10:00:00Z',
          freshness: { age_seconds: 10, stale_after_seconds: 120, stale: false },
          os: 'linux',
          arch: 'x86_64',
          cpu: { model: null, physical_cores: null, logical_cores: null, utilization_percent: 97 },
          memory: null,
          accelerators: null,
          ollama_model_store: null,
          ollama: null,
          runtime: null
        }
      })
    );

    expect(critical.state).toBe('critical');
    expect(critical.detail).toContain('CPU');
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

  it('summarizes generic degraded runtime issues before MCP details', () => {
    const degraded = executor({
      runtime_state: 'degraded',
      runtime_metadata: {
        degraded_issues: [
          {
            source: 'browser',
            title: 'Browser unavailable',
            message: 'Playwright failed to initialize'
          }
        ],
        mcp_servers: [
          { name: 'todoist', status: 'failed', phase: 'initialize', message: 'startup failed' }
        ]
      }
    });

    expect(executorRuntimeSummary(degraded)).toBe('1 degraded issue(s): Browser unavailable');
    expect(executorDegradedDetails(degraded)).toEqual([
      'browser: Browser unavailable · Playwright failed to initialize',
      'todoist: initialize · startup failed'
    ]);
  });

  it('rejects shell-style stdio commands with spaces', () => {
    expect(validateStdioCommand('npx -y @doist/todoist-ai')).toContain('Command must be only the executable');
    expect(validateStdioCommand('npx')).toBeNull();
  });
});

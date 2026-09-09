import { describe, expect, it } from 'vitest';

import {
  executorDegradedDetails,
  executorHealth,
  executorRuntimeBadgeStatus,
  executorRuntimeLabel,
  executorRuntimeSummary,
  executorCapabilityFreshness,
  executorTypeChoices,
  executorUnavailable,
  executorToolSelectionGuard,
  localExecutorTypesUnavailable,
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
    available: true,
    unavailable_reason: null,
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
  const report = (overrides: Record<string, unknown> = {}) => ({
    schema_version: 1 as const,
    observed_at: '2026-07-13T10:00:00Z',
    executor_version: '2.0.0',
    image_variant: 'general' as const,
    desired_tools: [],
    observed_tools: [],
    supported_tools: [],
    supported_components: [],
    components: {},
    browser: { runtimes: {}, engines: {}, channels: {} },
    officecli: { state: 'unknown' as const, reason_code: 'unknown', message: 'Unknown' },
    mcp_launch: { state: 'unknown' as const, reason_code: 'unknown', message: 'Unknown' },
    git: { state: 'unknown' as const, reason_code: 'unknown', message: 'Unknown' },
    node: { state: 'unknown' as const, reason_code: 'unknown', message: 'Unknown' },
    uv: { state: 'unknown' as const, reason_code: 'unknown', message: 'Unknown' },
    lsp: { state: 'unknown' as const, reason_code: 'unknown', message: 'Unknown' },
    ...overrides
  });

  it('uses advertised executor types for new executor choices', () => {
    expect(executorTypeChoices({ available_executor_types: ['websocket'] }, true)).toEqual(['websocket']);
    expect(executorTypeChoices({ available_executor_types: ['in_process', 'subprocess', 'websocket'] }, true)).toEqual([
      'in_process',
      'subprocess',
      'websocket'
    ]);
    expect(localExecutorTypesUnavailable({ available_executor_types: ['websocket'] })).toBe(true);
  });

  it('retains legacy choices when the availability field is absent', () => {
    expect(executorTypeChoices({}, true)).toEqual(['websocket', 'subprocess', 'in_process']);
    expect(executorTypeChoices(undefined, true)).toEqual(['websocket', 'subprocess', 'in_process']);
    expect(executorTypeChoices({ available_executor_types: ['websocket', 'in_process', 'subprocess'] }, false)).toEqual([
      'websocket'
    ]);
    expect(localExecutorTypesUnavailable(undefined)).toBe(false);
  });

  it('identifies unavailable persisted local rows without affecting WebSocket rows', () => {
    expect(executorUnavailable(executor({
      executor_type: 'in_process',
      available: false,
      unavailable_reason: 'Install cognis-executor.'
    }))).toBe(true);
    expect(executorHealth(executor({
      executor_type: 'in_process',
      available: false,
      unavailable_reason: 'Install cognis-executor.'
    })).label).toBe('Unavailable');
    expect(executorUnavailable(executor({ executor_type: 'websocket', available: true }))).toBe(false);
    expect(executorRuntimeLabel(executor({ executor_type: 'websocket', available: true }))).toBe('connected');
  });

  it('soft-guards unavailable and installable new explicit tools only', () => {
    const unavailable = executor({
      observed_capabilities: report({
        components: {
          browser: { state: 'unavailable', reason_code: 'missing_dependency', message: 'Install a browser runtime.' }
        }
      })
    });
    const installable = executor({
      observed_capabilities: report({
        components: {
          browser: { state: 'installable', reason_code: 'missing_dependency', message: 'Browser can be installed.' }
        }
      })
    });

    expect(executorToolSelectionGuard(unavailable, 'browse', 'browser')).toMatchObject({
      blocked: true,
      state: 'unavailable',
      reason: 'Install a browser runtime.'
    });
    expect(executorToolSelectionGuard(installable, 'browse', 'browser').blocked).toBe(true);
  });

  it('keeps configured tools removable and unknown or portable selections available', () => {
    const configured = executor({
      enabled_tools: ['browse'],
      enabled_tool_groups: ['browser'],
      observed_capabilities: report({
        components: {
          browser: { state: 'unavailable', reason_code: 'missing_dependency', message: 'Not installed.' }
        }
      })
    });
    expect(executorToolSelectionGuard(configured, 'browse', 'browser')).toMatchObject({
      blocked: false,
      configured: true
    });
    expect(executorToolSelectionGuard(executor(), 'new_tool', 'browser').blocked).toBe(false);
    expect(
      executorToolSelectionGuard(
        executor({ runtime_state: 'offline', observed_capabilities: report({
          components: {
            browser: { state: 'unavailable', reason_code: 'missing_dependency', message: 'Not installed.' }
          }
        }) }),
        'new_tool',
        'browser'
      ).blocked
    ).toBe(false);
    expect(
      executorToolSelectionGuard(
        executor({
          runtime_metadata: { legacy_metadata: true },
          observed_capabilities: report({
            components: {
              browser: { state: 'unavailable', reason_code: 'missing_dependency', message: 'Not installed.' }
            }
          })
        }),
        'new_tool',
        'browser'
      ).blocked
    ).toBe(false);
    expect(executorToolSelectionGuard(configured, '*').portable).toBe(true);
    expect(executorToolSelectionGuard(configured, 'other_tool', 'browser').portable).toBe(true);
  });

  it('does not treat supported definitions as active observations', () => {
    const withSupport = executor({
      observed_capabilities: report({
        supported_tools: ['supported_only'],
        observed_tools: ['active_tool']
      })
    });

    expect(executorToolSelectionGuard(withSupport, 'supported_only', 'browser').blocked).toBe(false);
    expect(withSupport.observed_capabilities?.observed_tools).not.toContain('supported_only');
  });

  it('classifies capability report freshness without making missing data unavailable', () => {
    expect(executorCapabilityFreshness(undefined)).toMatchObject({ state: 'unknown', ageSeconds: null });
    expect(executorCapabilityFreshness(report(), Date.parse('2026-07-13T10:00:30Z'), 60)).toMatchObject({
      state: 'fresh',
      ageSeconds: 30
    });
    expect(executorCapabilityFreshness(report(), Date.parse('2026-07-13T10:02:01Z'), 60).state).toBe('stale');
  });

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

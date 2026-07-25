import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import type { ExecutorConfig, ExecutorResourceSnapshot } from '$lib/types/api';
import ExecutorHealthPanel from './ExecutorHealthPanel.svelte';

function snapshot(overrides: Partial<ExecutorResourceSnapshot> = {}): ExecutorResourceSnapshot {
  return {
    schema_version: 1,
    observed_at: '2026-07-13T10:00:00Z',
    freshness: { age_seconds: 12, stale_after_seconds: 120, stale: false },
    os: 'darwin',
    arch: 'arm64',
    cpu: {
      model: 'Apple M4 Max',
      physical_cores: 16,
      logical_cores: 16,
      utilization_percent: 38
    },
    memory: {
      total_bytes: 64 * 1024 ** 3,
      available_bytes: 40 * 1024 ** 3,
      used_bytes: 24 * 1024 ** 3,
      unified: true
    },
    accelerators: [
      {
        backend: 'metal',
        name: 'Apple M4 Max',
        total_memory_bytes: 64 * 1024 ** 3,
        used_memory_bytes: null,
        utilization_percent: null
      }
    ],
    ollama_model_store: {
      total_bytes: 1024 * 1024 ** 3,
      free_bytes: 640 * 1024 ** 3
    },
    ollama: {
      status: 'reachable',
      version: '0.9.1',
      installed_model_count: 3,
      running_model_count: 1,
      running_models: ['qwen3:8b']
    },
    runtime: {
      uptime_seconds: 7200,
      active_calls: 1,
      configured: true,
      state: 'active'
    },
    ...overrides
  };
}

function executor(overrides: Partial<ExecutorConfig> = {}): ExecutorConfig {
  return {
    executor_id: 'exec-1',
    name: 'Mac Studio',
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
    resource_snapshot: snapshot(),
    last_observed_at: '2026-07-13T10:00:00Z',
    is_default: false,
    shared: false,
    owner_email: 'user@example.com',
    created_at: null,
    updated_at: null,
    ...overrides
  };
}

describe('ExecutorHealthPanel', () => {
  it('renders current unified-memory, Ollama, and runtime state', () => {
    render(ExecutorHealthPanel, { executor: executor() });

    expect(screen.getByText('Healthy')).toBeTruthy();
    expect(screen.getByText('Unified memory')).toBeTruthy();
    expect(screen.getByText('CPU + GPU')).toBeTruthy();
    expect(screen.getByText('Online · v0.9.1')).toBeTruthy();
    expect(screen.getByText('qwen3:8b')).toBeTruthy();
    expect(screen.getByText('1 active call')).toBeTruthy();
    expect(screen.getByRole('progressbar', { name: 'CPU now' })).toHaveAttribute('aria-valuenow', '38');
  });

  it('uses plain-language stale health and preserves unknown values', () => {
    render(ExecutorHealthPanel, {
      executor: executor({
        resource_snapshot: snapshot({
          freshness: { age_seconds: 180, stale_after_seconds: 120, stale: true },
          cpu: null,
          memory: null,
          accelerators: null,
          ollama: null,
          ollama_model_store: null
        })
      })
    });

    expect(screen.getByText('Stale data')).toBeTruthy();
    expect(screen.getByText('Model not reported')).toBeTruthy();
    expect(screen.getAllByText('Not reported').length).toBeGreaterThan(0);
    expect(screen.getByText('Status unknown')).toBeTruthy();
    expect(screen.queryByRole('progressbar', { name: 'CPU now' })).toBeNull();
  });

  it('renders a useful empty state for older executors', () => {
    render(ExecutorHealthPanel, {
      executor: executor({ resource_snapshot: null })
    });

    expect(screen.getByText('Resource data unavailable')).toBeTruthy();
    expect(screen.getByText('Current hardware details are not available.')).toBeTruthy();
  });
});

import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import type { ExecutorConfig, RuntimeCapabilityReport } from '$lib/types/api';
import ExecutorCapabilitiesPanel from './ExecutorCapabilitiesPanel.svelte';

function report(): RuntimeCapabilityReport {
  const ready = { state: 'ready' as const, reason_code: 'available', message: 'Available.', version: '1.2.3' };
  const unknown = { state: 'unknown' as const, reason_code: 'unknown', message: 'Not checked.' };
  return {
    schema_version: 1,
    observed_at: '2026-07-13T10:00:00Z',
    executor_version: '2.0.0',
    image_variant: 'development',
    desired_tools: ['read_file'],
    observed_tools: ['read_file'],
    supported_tools: ['read_file', 'future_tool'],
    supported_components: ['browser'],
    components: { browser: ready, mcp: unknown },
    browser: {
      runtimes: { patchright: ready },
      engines: { chromium: ready },
      channels: { chrome: unknown }
    },
    officecli: ready,
    mcp_launch: unknown,
    git: ready,
    node: ready,
    uv: ready,
    lsp: unknown
  };
}

function executor(overrides: Partial<ExecutorConfig> = {}): ExecutorConfig {
  return {
    executor_id: 'exec-1',
    name: 'Development executor',
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
    owner_email: null,
    created_at: null,
    updated_at: null,
    ...overrides
  };
}

describe('ExecutorCapabilitiesPanel', () => {
  it('renders runtime metadata, browser states, and distinct tool axes', () => {
    render(ExecutorCapabilitiesPanel, { executor: executor({ observed_capabilities: report() }) });

    expect(screen.getByText('Executor v2.0.0 · Development image')).toBeTruthy();
    expect(screen.getByText('Patchright')).toBeTruthy();
    expect(screen.getAllByText('Ready · v1.2.3').length).toBeGreaterThan(0);
    expect(screen.getByText('Configured desired tools')).toBeTruthy();
    expect(screen.getByText('Active observed tools')).toBeTruthy();
    expect(screen.getByText('Supported definitions')).toBeTruthy();
    expect(screen.getByText(/future_tool/)).toBeTruthy();
  });

  it('renders a safe legacy or offline empty state', () => {
    render(ExecutorCapabilitiesPanel, { executor: executor({ runtime_state: 'offline' }) });

    expect(screen.getByText('Runtime capability details are not available.')).toBeTruthy();
    expect(screen.getByText(/Legacy, offline, or older executors/)).toBeTruthy();
  });
});

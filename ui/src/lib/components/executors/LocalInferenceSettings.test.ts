import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import type { ExecutorConfig } from '$lib/types/api';
import LocalInferenceSettings from './LocalInferenceSettings.svelte';

function executor(overrides: Partial<ExecutorConfig> = {}): ExecutorConfig {
  return {
    executor_id: 'exec-1',
    name: 'Workstation',
    executor_type: 'websocket',
    labels: {},
    enabled_tools: [],
    enabled_tool_groups: [],
    config: {
      local_inference_enabled: true,
      ollama_runtime: {
        port: 11434,
        management_enabled: true,
        max_concurrent_pulls: 1,
        disk_headroom_bytes: 5 * 1024 ** 3,
        request_timeout_seconds: 1800,
        model_store_path: null
      }
    },
    local_inference_enabled: true,
    ollama_management_enabled: true,
    ollama_port: 11434,
    ollama_endpoint: 'http://127.0.0.1:11434',
    local_inference_config_status: 'confirmed',
    status: 'active',
    runtime_state: 'active',
    desired_config_version: 4,
    applied_config_version: 4,
    runtime_metadata: {
      local_inference_enabled: true,
      ollama_runtime: {
        runtime_type: 'ollama',
        port: 11434,
        endpoint: 'http://127.0.0.1:11434',
        management_enabled: true,
        max_concurrent_pulls: 1,
        disk_headroom_bytes: 5 * 1024 ** 3
      }
    },
    resource_snapshot: {
      schema_version: 1,
      observed_at: '2026-07-14T10:00:00Z',
      freshness: null,
      os: 'linux',
      arch: 'x86_64',
      cpu: null,
      memory: null,
      accelerators: null,
      ollama_model_store: null,
      ollama: {
        status: 'reachable',
        version: '0.10.1',
        installed_model_count: 2,
        running_model_count: 0,
        running_models: []
      },
      runtime: null
    },
    last_observed_at: '2026-07-14T10:00:00Z',
    is_default: false,
    shared: false,
    owner_email: 'owner@example.com',
    created_at: null,
    updated_at: null,
    ...overrides
  };
}

describe('LocalInferenceSettings', () => {
  it('shows confirmed reachability and saves a custom derived port without an endpoint', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(LocalInferenceSettings, { executor: executor(), editable: true, onSave });

    expect(screen.getByText('Executor confirmed')).toBeInTheDocument();
    expect(screen.getByText('Reachable · v0.10.1')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: /Enable local inference/ })).toBeChecked();

    await fireEvent.input(screen.getByRole('spinbutton', { name: 'Ollama port' }), {
      target: { value: '22434' }
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    const config = onSave.mock.calls[0][0];
    expect(config.ollama_runtime.port).toBe(22434);
    expect(config.ollama_runtime.endpoint).toBeUndefined();
    expect(screen.getByRole('status')).toHaveTextContent('saved');
  });

  it('renders applying generations and validates ports before save', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(LocalInferenceSettings, {
      executor: executor({
        local_inference_config_status: 'applying',
        desired_config_version: 5,
        applied_config_version: 4
      }),
      editable: true,
      onSave
    });

    expect(screen.getByText('Applying')).toBeInTheDocument();
    expect(screen.getByText('v4 / desired v5')).toBeInTheDocument();
    await fireEvent.input(screen.getByRole('spinbutton', { name: 'Ollama port' }), {
      target: { value: '70000' }
    });
    expect(screen.getByRole('alert')).toHaveTextContent('1 to 65535');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('is read-only for viewers while preserving status and advanced details', () => {
    const onSave = vi.fn();
    const { container } = render(LocalInferenceSettings, {
      executor: executor({ local_inference_config_status: 'unconfirmed' }),
      editable: false,
      onSave
    });

    expect(screen.getByText('Not confirmed')).toBeInTheDocument();
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    expect(screen.getAllByRole('switch').every((element) => element.hasAttribute('disabled'))).toBe(true);
    expect(container.querySelector('.md\\:grid-cols-2')).toBeTruthy();
  });

  it('surfaces save errors and keeps the form editable for retry', async () => {
    const onSave = vi.fn().mockRejectedValue(new Error('configuration conflict'));
    render(LocalInferenceSettings, { executor: executor(), editable: true, onSave });

    await fireEvent.click(screen.getByRole('switch', { name: /Allow Cognis/ }));
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('configuration conflict')
    );
    expect(screen.getByRole('button', { name: 'Save' })).not.toBeDisabled();
  });
});

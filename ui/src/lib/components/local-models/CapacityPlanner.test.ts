import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import CapacityPlanner from './CapacityPlanner.svelte';
import type { LocalModelCatalogItem, LocalModelFitPlan } from '$lib/types/api';

const model: LocalModelCatalogItem = {
  catalog_id: 'ollama:qwen3:8b',
  source: 'ollama',
  requested_ref: 'qwen3:8b',
  title: 'Qwen 3 8B',
  publisher: 'Qwen',
  repository_url: null,
  model_card_url: null,
  revision_sha: null,
  license: 'Apache-2.0',
  description: 'Test model',
  downloads: null,
  likes: null,
  last_modified: null,
  pipeline_tag: null,
  tags: [],
  base_models: [],
  capabilities: ['chat'],
  parameter_count: 8_000_000_000,
  quantizations: [
    {
      name: 'Q4_K_M',
      requested_ref: 'qwen3:8b',
      file_name: null,
      size_bytes: 5_000_000_000,
      bits_per_weight: 4.5
    }
  ],
  file_size_bytes: 5_000_000_000,
  advertised_max_context: 131072,
  architecture: {},
  architecture_name: null,
  metadata_status: 'complete',
  metadata_confidence: 'high',
  metadata_diagnostics: [],
  reference_integrity: 'floating',
  warnings: []
};

const redPlan: LocalModelFitPlan = {
  assessment_generation: 7,
  advisory_only: true,
  requested_context_tokens: 200000,
  advertised_max_context: 131072,
  advertised_max_exceeded: true,
  recommended_context_tokens: 32768,
  context_options: [
    { context_tokens: 32768, zone: 'green', limiting_executor_ids: [] },
    { context_tokens: 131072, zone: 'red', limiting_executor_ids: ['small'] }
  ],
  executors: [
    {
      executor_id: 'small',
      executor_name: 'Small GPU',
      context_tokens: 200000,
      static: {
        status: 'NO_FIT',
        confidence: 'high',
        available_bytes: 8,
        accelerator_available_bytes: 4,
        host_available_bytes: 4,
        reason_codes: ['combined_memory_insufficient']
      },
      admission: {
        status: 'NO_FIT',
        confidence: 'high',
        available_bytes: 8,
        accelerator_available_bytes: 4,
        host_available_bytes: 4,
        reason_codes: ['combined_memory_insufficient']
      },
      breakdown: {
        weights_bytes: 5,
        kv_cache_min_bytes: 2,
        kv_cache_max_bytes: 4,
        runtime_buffer_bytes: 1,
        reserved_headroom_bytes: 2,
        required_min_bytes: 10,
        required_max_bytes: 12
      },
      unified_memory: false,
      snapshot_age_seconds: 3,
      advertised_max_exceeded: true,
      assumptions: []
    }
  ]
};

describe('CapacityPlanner', () => {
  it('exposes a logarithmic preset slider and accepts a custom red-zone context', async () => {
    const onplan = vi.fn();
    const oncontextchange = vi.fn();
    render(CapacityPlanner, {
      model,
      plan: redPlan,
      contextTokens: 32768,
      onplan,
      oncontextchange
    });

    const slider = screen.getByRole('slider', { name: 'Context window' });
    expect(slider).toHaveAttribute('max', '5');
    await fireEvent.input(screen.getByLabelText('Custom context tokens'), {
      target: { value: '200000' }
    });

    expect(screen.getByText(/above the advertised 128k context/i)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Capacity warning');
    expect(screen.getByText('Probably will not load')).toBeInTheDocument();
    expect(oncontextchange).toHaveBeenCalledOnce();
  });

  it('has accessible labels and invokes advisory estimation explicitly', async () => {
    const onplan = vi.fn();
    render(CapacityPlanner, {
      model,
      plan: null,
      contextTokens: 32768,
      onplan
    });

    expect(screen.getByRole('heading', { name: 'Choose context capacity' })).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Estimate fit' }));
    expect(onplan).toHaveBeenCalledOnce();
  });
});

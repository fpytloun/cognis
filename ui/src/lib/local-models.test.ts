import { describe, expect, it } from 'vitest';

import {
  contextPresets,
  deploymentPayload,
  fitMetadata,
  matchedExecutors,
  planZone,
  requiresCapacityOverride
} from '$lib/local-models';
import type {
  ExecutorConfig,
  LocalModelFitPlan
} from '$lib/types/api';

function plan(overrides: Partial<LocalModelFitPlan> = {}): LocalModelFitPlan {
  return {
    assessment_generation: 42,
    advisory_only: true,
    requested_context_tokens: 32768,
    advertised_max_context: 131072,
    advertised_max_exceeded: false,
    recommended_context_tokens: 32768,
    context_options: [
      { context_tokens: 32768, zone: 'green', limiting_executor_ids: [] },
      { context_tokens: 65536, zone: 'red', limiting_executor_ids: ['small'] }
    ],
    executors: [
      {
        executor_id: 'large',
        executor_name: 'Large',
        context_tokens: 32768,
        static: {
          status: 'FIT',
          confidence: 'high',
          available_bytes: 64,
          accelerator_available_bytes: null,
          host_available_bytes: 64,
          reason_codes: ['host_memory_sufficient']
        },
        admission: {
          status: 'FIT',
          confidence: 'high',
          available_bytes: 64,
          accelerator_available_bytes: null,
          host_available_bytes: 64,
          reason_codes: ['host_memory_sufficient']
        },
        breakdown: {
          weights_bytes: 4,
          kv_cache_min_bytes: 1,
          kv_cache_max_bytes: 2,
          runtime_buffer_bytes: 1,
          reserved_headroom_bytes: 2,
          required_min_bytes: 8,
          required_max_bytes: 9
        },
        unified_memory: false,
        snapshot_age_seconds: 5,
        advertised_max_exceeded: false,
        assumptions: []
      }
    ],
    ...overrides
  };
}

describe('local model UX helpers', () => {
  it('keeps logarithmic presets and adds the advertised max without clamping', () => {
    expect(contextPresets(200000)).toEqual([
      8192,
      16384,
      32768,
      65536,
      131072,
      200000,
      262144
    ]);
  });

  it('marks custom above-advertised context red and requires an override', () => {
    const custom = plan({
      requested_context_tokens: 200000,
      advertised_max_exceeded: true
    });

    expect(planZone(custom)).toBe('red');
    expect(requiresCapacityOverride(custom)).toBe(true);
  });

  it('preserves the group limiting executor instead of averaging results', () => {
    const result = plan();

    expect(result.context_options[1].limiting_executor_ids).toEqual(['small']);
    expect(result.recommended_context_tokens).toBe(32768);
  });

  it('builds an exact desired-state payload with persisted override acknowledgement', () => {
    const custom = plan({ advertised_max_exceeded: true });

    expect(
      deploymentPayload(
        'hf.co/acme/model:Q4_K_M',
        { executor_ids: ['small'] },
        'ollama-provider',
        custom,
        true
      )
    ).toEqual({
      requested_ref: 'hf.co/acme/model:Q4_K_M',
      selector: { executor_ids: ['small'] },
      provider_id: 'ollama-provider',
      capacity_override_acknowledged: true,
      capacity_assessment_generation: 42
    });
  });

  it('never substitutes another quantization size for an unknown selected artifact', () => {
    const metadata = fitMetadata(
      {
        catalog_id: 'huggingface:acme/model',
        source: 'huggingface',
        requested_ref: 'hf.co/acme/model:Q4_K_M',
        title: 'Model',
        publisher: 'acme',
        repository_url: 'https://huggingface.co/acme/model',
        model_card_url: 'https://huggingface.co/acme/model#model-card',
        revision_sha: null,
        license: null,
        description: null,
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
            requested_ref: 'hf.co/acme/model:Q4_K_M',
            file_name: 'model-Q4_K_M.gguf',
            size_bytes: 4_000_000_000,
            bits_per_weight: 4.5
          },
          {
            name: 'Q8_0',
            requested_ref: 'hf.co/acme/model:Q8_0',
            file_name: 'Multiple independent 2 GGUF artifacts',
            size_bytes: null,
            bits_per_weight: 8
          }
        ],
        file_size_bytes: 4_000_000_000,
        advertised_max_context: 32768,
        architecture: {},
        architecture_name: null,
        metadata_status: 'basic',
        metadata_confidence: 'medium',
        metadata_diagnostics: [],
        reference_integrity: 'floating',
        warnings: []
      },
      'hf.co/acme/model:Q8_0'
    );

    expect(metadata.weights_bytes).toBeNull();
    expect(metadata.file_size_bytes).toBeNull();
    expect(metadata.quantization).toBe('Q8_0');
  });

  it('previews exact IDs and label selectors against heterogeneous executors', () => {
    const executors = [
      { executor_id: 'small', labels: { gpu: 'none' } },
      { executor_id: 'large', labels: { gpu: 'nvidia' } }
    ] as unknown as ExecutorConfig[];

    expect(matchedExecutors(executors, { match_labels: { gpu: 'nvidia' } })).toEqual([
      executors[1]
    ]);
    expect(matchedExecutors(executors, { executor_ids: ['small'] })).toEqual([
      executors[0]
    ]);
  });
});

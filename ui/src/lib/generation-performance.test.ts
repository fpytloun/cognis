import { describe, expect, it } from 'vitest';

import {
  localPerformanceMetrics,
  mergeLatestPerformance,
  responsivenessBadge
} from './generation-performance';
import type { GenerationPerformanceSnapshot } from './types/api';

const localPerformance: GenerationPerformanceSnapshot = {
  is_local: true,
  provider_id: 'ollama',
  provider_name: 'Local Ollama',
  runtime: 'Ollama',
  location: 'executor',
  executor_id: 'exec-1',
  executor_name: 'Workstation',
  model: 'qwen3:8b',
  digest: null,
  quantization: 'Q4_K_M',
  configured_context_tokens: 32768,
  prompt_tokens: 100,
  completion_tokens: 20,
  prompt_tokens_per_second: 50,
  generation_tokens_per_second: 25,
  time_to_first_token_seconds: 0.8,
  load_duration_seconds: 0.25,
  total_duration_seconds: 2,
  processor: 'GPU',
  gpu_residency: '8 GB',
  measured_at: '2026-07-13T12:00:00Z'
};

describe('local generation performance', () => {
  it('renders raw pp, tg, TTFT, and load metrics', () => {
    expect(localPerformanceMetrics(localPerformance)).toEqual([
      { label: 'Prompt processing', value: '50.0 tokens/s', raw: 'pp' },
      { label: 'Generation', value: '25.0 tokens/s', raw: 'tg' },
      { label: 'First token', value: '800 ms', raw: 'TTFT' },
      { label: 'Model load', value: '250 ms', raw: 'load' },
      { label: 'Total generation', value: '2.00 s', raw: 'total' }
    ]);
  });

  it('ties the responsiveness badge to model, executor, and context', () => {
    expect(responsivenessBadge(localPerformance)).toEqual({
      label: 'Responsive',
      detail: 'qwen3:8b on Workstation at 32,768-token context',
      tone: 'good'
    });
  });

  it('hides local-only metrics for cloud observations', () => {
    expect(localPerformanceMetrics({ ...localPerformance, is_local: false })).toEqual([]);
    expect(responsivenessBadge({ ...localPerformance, is_local: false })).toBeNull();
  });

  it('retains omitted observations but clears an explicit null', () => {
    expect(mergeLatestPerformance(localPerformance, undefined)).toBe(localPerformance);
    expect(mergeLatestPerformance(localPerformance, null)).toBeNull();
  });
});

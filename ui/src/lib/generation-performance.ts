import type { GenerationPerformanceSnapshot } from '$lib/types/api';

export type PerformanceMetric = {
  label: string;
  value: string;
  raw: string;
};

export function mergeLatestPerformance(
  current: GenerationPerformanceSnapshot | null,
  incoming: GenerationPerformanceSnapshot | null | undefined
): GenerationPerformanceSnapshot | null {
  return incoming === undefined ? current : incoming;
}

export function formatPerformanceDuration(seconds: number | null): string | null {
  if (seconds == null) return null;
  return seconds < 1 ? `${Math.round(seconds * 1000)} ms` : `${seconds.toFixed(2)} s`;
}

export function localPerformanceMetrics(
  performance: GenerationPerformanceSnapshot
): PerformanceMetric[] {
  if (!performance.is_local) return [];
  const metrics: PerformanceMetric[] = [];
  if (performance.prompt_tokens_per_second != null) {
    metrics.push({
      label: 'Prompt processing',
      value: `${performance.prompt_tokens_per_second.toFixed(1)} tokens/s`,
      raw: 'pp'
    });
  }
  if (performance.generation_tokens_per_second != null) {
    metrics.push({
      label: 'Generation',
      value: `${performance.generation_tokens_per_second.toFixed(1)} tokens/s`,
      raw: 'tg'
    });
  }
  const ttft = formatPerformanceDuration(performance.time_to_first_token_seconds);
  if (ttft) metrics.push({ label: 'First token', value: ttft, raw: 'TTFT' });
  const load = formatPerformanceDuration(performance.load_duration_seconds);
  if (load) metrics.push({ label: 'Model load', value: load, raw: 'load' });
  const total = formatPerformanceDuration(performance.total_duration_seconds);
  if (total) metrics.push({ label: 'Total generation', value: total, raw: 'total' });
  return metrics;
}

export function responsivenessBadge(
  performance: GenerationPerformanceSnapshot
): { label: string; detail: string; tone: 'good' | 'neutral' | 'slow' } | null {
  if (!performance.is_local) return null;
  const ttft = performance.time_to_first_token_seconds;
  const tg = performance.generation_tokens_per_second;
  if (ttft == null && tg == null) return null;
  const executor = performance.executor_name ?? performance.executor_id ?? 'local runtime';
  const context = performance.configured_context_tokens
    ? `${performance.configured_context_tokens.toLocaleString()}-token context`
    : 'configured context';
  const detail = `${performance.model} on ${executor} at ${context}`;
  if ((ttft == null || ttft <= 2) && (tg == null || tg >= 20)) {
    return { label: 'Responsive', detail, tone: 'good' };
  }
  if ((ttft != null && ttft > 6) || (tg != null && tg < 8)) {
    return { label: 'Slow response', detail, tone: 'slow' };
  }
  return { label: 'Steady', detail, tone: 'neutral' };
}

import type { CompanionUsage, ModelParameter, UsageLifecycle, UsagePerformance } from './model';
export const fixtureLifecycle: UsageLifecycle = {
  observedAttempts: 3072, observedStart: 8, inflight: 12, dispatchedKnown: 3056,
  completed: 2980, failed: 51, cancelled: 13, unknown: 8
};
export const fixturePerformance: UsagePerformance[] = [
  {
    provider: 'OpenAI', serverLatency: { totalMs: 812000, samples: 100 },
    serverTtft: { totalMs: 72000, samples: 80 }, providerSpan: { totalMs: 770000, samples: 100 },
    executorSpan: { totalMs: 784000, samples: 100 },
    clientLatency: { totalMs: null, samples: 0 }, clientTtft: { totalMs: null, samples: 0 },
    generation: { outputTokens: 58000, durationMs: 640000, samples: 80 },
    endToEnd: { outputTokens: 68000, durationMs: 812000, samples: 100 },
    failedAttempts: 9, dispatchedAttempts: 100, logicalFailed: 1, logicalCompleted: 90
  },
  {
    provider: 'Anthropic', serverLatency: { totalMs: 526000, samples: 50 },
    serverTtft: { totalMs: 45000, samples: 45 }, providerSpan: { totalMs: 510000, samples: 50 },
    executorSpan: { totalMs: null, samples: 0 },
    clientLatency: { totalMs: 204000, samples: 20 }, clientTtft: { totalMs: 22000, samples: 20 },
    clientSources: [{ source: 'Cognis UI · client monotonic', first: { totalMs: 22000, samples: 20 }, last: { totalMs: 204000, samples: 20 } }],
    generation: { outputTokens: 22000, durationMs: 392000, samples: 45 },
    endToEnd: { outputTokens: 27000, durationMs: 526000, samples: 50 },
    failedAttempts: 2, dispatchedAttempts: 50, logicalFailed: null, logicalCompleted: null
  }
];
export const fixtureCompanions: CompanionUsage[] = [
  { service: 'Mnemory', operation: 'memory.extract', sessionless: 182, finalized: 182, input: 920400, output: 62100, cost: { amount: '2.84125', currency: 'USD', basis: 'api_estimate' } },
  { service: 'Mnemory', operation: 'embedding', sessionless: 248, finalized: 248, input: 512800, output: null, cost: { amount: null, currency: 'USD', basis: 'unknown' } },
  { service: 'Intaris', operation: 'guardrail.classify', sessionless: 304, finalized: 304, input: 684200, output: 8300, cost: { amount: '0.91287', currency: 'USD', basis: 'api_equivalent' } }
];
export const fixtureParameters: ModelParameter[] = [
  { name: 'temperature', requested: .2, effective: .2, provenance: 'explicit' },
  { name: 'top_p', requested: null, effective: 1, provenance: 'configured default' },
  { name: 'top_k', requested: null, effective: null, provenance: 'unsupported' },
  { name: 'max_output_tokens', requested: 4096, effective: 4096, provenance: 'explicit' },
  { name: 'seed', requested: 42, effective: null, provenance: 'unsupported' },
  { name: 'frequency_penalty', requested: null, effective: null, provenance: 'provider default' },
  { name: 'presence_penalty', requested: null, effective: null, provenance: 'unknown' },
  { name: 'reasoning_effort', requested: 'high', effective: 'high', provenance: 'explicit' },
  { name: 'reasoning_budget_tokens', requested: null, effective: null, provenance: 'unknown' },
  { name: 'service_tier', requested: 'auto', effective: 'standard', provenance: 'explicit' },
  { name: 'response_format', requested: 'json_object', effective: 'json_object', provenance: 'explicit' },
  { name: 'tool_choice', requested: 'auto', effective: 'auto', provenance: 'configured default' }
];
export function fixtureAttemptTiming(index: number, output: number | null = 2360): UsagePerformance {
  const row = fixturePerformance[index];
  const timing = (value: { totalMs: number | null; samples: number }) => ({ totalMs: value.totalMs === null || !value.samples ? null : value.totalMs / value.samples, samples: value.samples ? 1 : 0 });
  const rate = (value: { outputTokens: number | null; durationMs: number | null; samples: number }) => ({ outputTokens: output, durationMs: output === null || value.durationMs === null ? null : value.durationMs / value.samples, samples: output !== null && value.samples ? 1 : 0 });
  return { ...row, serverLatency: timing(row.serverLatency), serverTtft: output === null ? { totalMs: null, samples: 0 } : timing(row.serverTtft), providerSpan: timing(row.providerSpan), executorSpan: timing(row.executorSpan), clientLatency: timing(row.clientLatency), clientTtft: output === null ? { totalMs: null, samples: 0 } : timing(row.clientTtft), generation: rate(row.generation), endToEnd: rate(row.endToEnd), dispatchedAttempts: 1, failedAttempts: output === null ? 1 : 0, logicalFailed: null, logicalCompleted: null };
}

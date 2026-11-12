import { blankFilters, type UsageAttempt, type UsageCost, type UsageDimension, type UsageQuery, type UsageSession, type UsageTotals, type UsageView } from './model';
import { addDecimals } from './decimal';
import { fixtureAttemptTiming, fixtureCompanions, fixtureLifecycle, fixtureParameters, fixturePerformance } from './performance-fixtures';
import { fixtureForecast } from './fixture-forecast';

const cost = (amount: string | null): UsageCost => ({ amount, currency: 'USD', basis: 'api_estimate' });
export const fixtureQuery: UsageQuery = { mode: 'admin', filters: { ...blankFilters(), start: '2026-10-01', end: '2026-10-29' }, groupBy: 'agent' };
type FixtureRecord = UsageSession & Record<UsageDimension | 'hook' | 'service' | 'operation' | 'day', string> & { projectVisible: boolean };
const specs = [
  ['Repository implementation', 'LaForge', 'Interactive', 'OpenAI', 'GPT-5', 'high', 'Alex', 'Cognis', 'None', true],
  ['Release review', 'Riker', 'Interactive', 'Anthropic', 'Sonnet', 'medium', 'Sam', 'Cognis', 'None', true],
  ['Context recap', 'System', 'Recap', 'OpenAI', 'GPT-5 mini', 'low', 'Alex', 'Cognis', 'Recap', true],
  ['Private investigation', 'LaForge', 'Interactive', 'OpenAI', 'GPT-5', 'high', 'Alex', 'Cognis', 'None', false],
  ['Document ingestion', 'Riker', 'Embedding', 'OpenAI', 'Embedding', 'none', 'Sam', 'Research', 'Ingest', true],
  ['Policy evaluation', 'System', 'Evaluation', 'Anthropic', 'Sonnet', 'low', 'Alex', 'Cognis', 'Guard', true],
  ['Morning hook', 'Riker', 'Hook', 'OpenAI', 'GPT-5 mini', 'low', 'Alex', 'Research', 'Morning', true]
] as const;
const records: FixtureRecord[] = Array.from({ length: 29 }, (_, day) => specs.map((spec, index) => ({
  id: `fixture-session-${day + 1}-${index}`, label: spec[0], agent: spec[1], purpose: spec[2], service: index === 4 ? 'Mnemory · embedding' : 'Cognis · inference', operation: index === 4 ? 'embedding' : 'inference',
  provider: spec[3], model: spec[4], effort: spec[5], user: spec[6], project: spec[7], hook: spec[8], projectVisible: spec[9],
  day: `2026-10-${String(day + 1).padStart(2, '0')}`,
  transcriptHref: null, input: 18300 + ((day * 7919 + index * 4441) % 42000), output: 1200 + day * 57 + index * 491,
  cacheRead: 10000 + day * 150, cacheWrite: index === 4 ? null : 1500 + index * 120, cacheEligibleInput: 30000 + day * 250,
  reasoning: index === 4 ? null : 630 + day * 21, attempts: 12 + index, unknown: index === 4 ? 1 : 0, partial: day === 28 ? 1 : 0,
  unpriced: index === 4 ? 1 : 0, cost: cost(index === 4 ? null : (BigInt(18300 + day * 591 + index * 323) * 10n).toString().padStart(7, '0').replace(/(\d{6})$/, '.$1'))
}))).flat();

function sum(rows: FixtureRecord[]): UsageTotals {
  const add = (key: 'input' | 'output' | 'cacheRead' | 'cacheWrite' | 'cacheEligibleInput' | 'reasoning') =>
    rows.length && rows.every((row) => row[key] === null) ? null : rows.reduce((total, row) => total + (row[key] ?? 0), 0);
  return {
    input: add('input'), output: add('output'), cacheRead: add('cacheRead'), cacheWrite: add('cacheWrite'),
    cacheEligibleInput: add('cacheEligibleInput'), reasoning: add('reasoning'),
    attempts: rows.reduce((n, row) => n + row.attempts, 0), unknown: rows.reduce((n, row) => n + row.unknown, 0),
    partial: rows.reduce((n, row) => n + row.partial, 0), unpriced: rows.reduce((n, row) => n + (row.unpriced ?? 0), 0),
    cost: cost(rows.length && rows.every((row) => row.cost.amount === null) ? null : addDecimals(rows.flatMap((row) => row.cost.amount === null ? [] : [row.cost.amount])))
  };
}
/** Synthetic harness only. Production aggregation and authorization belong to PostgreSQL/API. */
export function fixtureView(query: UsageQuery): UsageView & { totals: UsageTotals } {
  const authorized = records.filter((row) => query.mode === 'admin' || (query.mode === 'personal' ? row.user === 'Alex' : row.project === 'Cognis' && row.projectVisible));
  const dimensions: UsageDimension[] = ['provider', 'model', 'effort', 'agent', 'user', 'purpose', 'project'];
  const rows = authorized.filter((row) => (!query.filters.start || row.day >= query.filters.start) && (!query.filters.end || row.day <= query.filters.end)
    && dimensions.every((key) => !query.filters[key] || row[key] === query.filters[key]));
  const facets = Object.fromEntries(dimensions.map((key) => [key, [...new Set(authorized.map((row) => row[key]))].map((value) => ({ value, label: value }))])) as UsageView['facets'];
  // Fixture records carry the visible dimensions only; other groupings fall back to provider.
  const groupKey = (dimensions as string[]).includes(query.groupBy) ? query.groupBy as UsageDimension : 'provider';
  const groups = [...new Set(rows.map((row) => row[groupKey]))].map((label) => ({ id: label, label, ...sum(rows.filter((row) => row[groupKey] === label)) })).sort((a, b) => (b.input ?? 0) - (a.input ?? 0));
  const days = [...new Set(rows.map((row) => row.day))];
  const total = sum(rows);
  const buckets = days.map((start) => { const totals = sum(rows.filter((row) => row.day === start)); return { start, complete: start !== '2026-10-29', costComplete: totals.unpriced === 0, tokens: totals.input === null || totals.output === null ? null : totals.input + totals.output, cost: totals.cost }; });
  return {
    scopeLabel: query.mode === 'admin' ? 'Global usage-only access' : query.mode === 'project' ? 'Cognis · current project-visible resources' : 'Alex · authenticated usage',
    scopeDescription: query.mode === 'admin' ? 'All consumers across providers. Usage access does not grant transcript or credential access.' : query.mode === 'project' ? 'Across project members. Private resources are excluded, including your own; client and key identifiers are not exposed.' : 'Your usage across owned and shared providers. Provider ownership does not reveal other consumers.',
    freshness: '2026-10-29 18:00', coverageStart: '2026-10-01', pricing: 'historical', status: 'partial', totals: sum(rows), facets,
    buckets, forecast: fixtureForecast(buckets),
    groups, sessions: rows.slice(-5).reverse(), lifecycle: {
      ...fixtureLifecycle, observedAttempts: total.attempts + (rows.length ? 20 : 0),
      observedStart: rows.length ? 8 : 0, inflight: rows.length ? 12 : 0,
      dispatchedKnown: total.attempts - total.unknown + (rows.length ? 12 : 0),
      completed: total.attempts - Math.floor(total.attempts * .02) - Math.floor(total.attempts * .004) - total.unknown,
      failed: Math.floor(total.attempts * .02), cancelled: Math.floor(total.attempts * .004), unknown: total.unknown
    }, performance: fixturePerformance,
    companions: query.mode === 'project' ? [] : fixtureCompanions,
    quota: query.mode === 'project' ? null : { provider: 'OpenAI', window: '5-hour window', samples: [
      { at: '2026-10-29T08:10', usedPercent: 31, reset: false }, { at: '2026-10-29T09:45', usedPercent: 48, reset: false },
      { at: '2026-10-29T11:00', usedPercent: null, reset: false }, { at: '2026-10-29T13:10', usedPercent: 4, reset: true },
      { at: '2026-10-29T15:20', usedPercent: 22, reset: false }, { at: '2026-10-29T18:00', usedPercent: 64, reset: false }
    ] }
  };
}
export const fixtureAttempts: UsageAttempt[] = [
  { id: 'fixture-attempt-01', cycle: 'Cycle 1', model: 'GPT-5', effort: 'high', outcome: 'Completed', input: 18420, output: 2360, cacheRead: 12000, cost: cost('0.084'), detail: { lifecycle: 'completed', dispatched: true, service: 'Mnemory', operation: 'memory.extract', sessionId: null, requestedModel: 'gpt-5', provider: 'OpenAI', timings: fixtureAttemptTiming(0), parameters: fixtureParameters, error: null, identifiers: { client: 'mnemory', key: 'service-key-17', request: 'fixture-proxy-request-01' } } },
  { id: 'fixture-attempt-02', cycle: 'Cycle 2', model: 'GPT-5', effort: 'high', outcome: 'Failed · retry followed', input: 19200, output: null, cacheRead: null, cost: cost(null), detail: { lifecycle: 'failed', dispatched: true, service: 'Intaris', operation: 'guardrail.classify', sessionId: null, requestedModel: 'gpt-5', provider: 'OpenAI', timings: fixtureAttemptTiming(0, null), parameters: fixtureParameters, error: { category: 'rate_limit', code: '429', summary: 'Provider rate limit exceeded. Request content is not retained.' }, identifiers: null } },
  { id: 'fixture-attempt-03', cycle: 'Cycle 2', model: 'Sonnet', effort: null, outcome: 'Completed · fallback', input: 19600, output: 3120, cacheRead: 14000, cost: { amount: '0.18', currency: 'USD', basis: 'api_equivalent' } },
  { id: 'fixture-attempt-04', cycle: 'Cycle 3', model: 'GPT-5 mini', effort: null, outcome: 'Observed start · dispatch unknown', input: null, output: null, cacheRead: null, cost: cost(null), detail: { lifecycle: 'observed_start', dispatched: null, service: 'Mnemory', operation: 'embedding', sessionId: null, requestedModel: 'gpt-5-mini', provider: 'OpenAI', timings: { provider: 'OpenAI', serverLatency: { totalMs: null, samples: 0 }, serverTtft: { totalMs: null, samples: 0 }, providerSpan: { totalMs: null, samples: 0 }, executorSpan: { totalMs: null, samples: 0 }, clientLatency: { totalMs: null, samples: 0 }, clientTtft: { totalMs: null, samples: 0 }, generation: { outputTokens: null, durationMs: null, samples: 0 }, endToEnd: { outputTokens: null, durationMs: null, samples: 0 }, dispatchedAttempts: 0, failedAttempts: 0, logicalCompleted: null, logicalFailed: null }, parameters: [], error: null, identifiers: null } }
];

/** Deterministic opt-in screen data. Never imported by shipping routes/adapters. */
import { fixtureAttempts, fixtureView } from './fixtures';
import { fixtureParameters } from './performance-fixtures';
import type { UsageAttempt, UsagePerformance, UsageQuery, UsageView } from './model';
export function fixtureDayPerformance(index: number): UsagePerformance {
  const duration = (ms: number, samples = 12) => ({ totalMs: ms * samples, samples });
  const unknown = { totalMs: null, samples: 0 };
  const clients = index % 4 === 1 ? [] : [{ source: 'Cognis UI · synthetic client monotonic', first: duration(700 + index % 6 * 90, 8), last: duration(6600 + index % 5 * 350, 8) }];
  return { provider: 'Synthetic observed population', providerSpan: duration(4900 + index % 7 * 420), providerTtft: index % 6 ? duration(490 + index % 5 * 70) : unknown,
    serverLatency: duration(6400 + index % 7 * 370, 10), serverTtft: duration(650 + index % 6 * 80, 10), cognisOverhead: duration(900 + index % 5 * 110, 10),
    executorSpan: index % 5 ? duration(130 + index % 4 * 30, 10) : unknown, clientLatency: unknown, clientTtft: unknown, clientSources: clients,
    generation: { outputTokens: 3200 + index * 70, durationMs: 80000 + index % 5 * 4000, samples: 12 },
    endToEnd: { outputTokens: 3200 + index * 70, durationMs: 120000 + index % 5 * 7000, samples: 12 }, dispatchedAttempts: 12, failedAttempts: index % 4, logicalCompleted: 10, logicalFailed: index % 3 ? 0 : 1 };
}
export function fixtureScreen(query: UsageQuery): UsageView {
  const base = fixtureView(query);
  const availability = { resolution: 'retained' as const, cutoff: '2026-09-29T00:00:00Z', configuredDays: 30, grain: 'day', supportedDimensions: ['agent','provider','model','effort','subject','project','purpose','hook','service','operation','session'], unsupportedFields: [] };
  const sessions = [0, 1].map((index) => ({ ...(base.sessions[index] ?? base.totals), id: `fixture-cognis-session-${index + 1}`, label: index ? 'Cognis · nightly hook run' : 'Cognis · routing investigation', agent: 'Fixture Cognis agent', purpose: index ? 'hook' : 'interactive', transcriptHref: null,
    sessionType: 'cognis' as const, selectionRef: `v2.screen-fixture-session-${index + 1}`, totalsScope: 'cumulative' as const, drillFilters: { session_ids: [`fixture-cognis-session-${index + 1}`] } }));
  const serviceRows = fixtureAttempts.map((row, index): UsageAttempt => ({ ...row, startedAt: `2026-10-29T10:00:0${index}.000Z`, source: row.detail?.service ? 'Authenticated companion service' : 'Unattributed request',
    serviceId: row.detail?.service ?? null, operation: row.detail?.operation ?? null, providerId: row.detail?.provider ?? (row.model.startsWith('Claude') ? 'Anthropic' : 'OpenAI'),
    sessionId: null, cycleId: row.cycle === 'Cycle 2' ? 'fixture-cycle-2' : `fixture-cycle-${index + 1}`, logicalId: row.cycle === 'Cycle 2' ? 'fixture-logical-2' : `fixture-logical-${index + 1}`,
    status: row.detail?.lifecycle === 'completed' ? 'succeeded' : row.detail?.lifecycle ?? 'succeeded', cacheWrite: index % 2 ? null : 1100 }));
  const cognisRows: UsageAttempt[] = [0, 1, 2, 3].map((index) => ({ ...(index === 0 ? fixtureAttempts[1] : fixtureAttempts[0]), id: `fixture-cognis-request-${index + 1}`, cycle: `Cycle ${Math.floor(index / 2) + 1}`,
    startedAt: `2026-10-29T11:0${index}:00.000Z`, providerId: (index === 0 ? fixtureAttempts[1] : fixtureAttempts[0]).detail!.provider, source: 'Cognis session', sessionId: sessions[Math.floor(index / 2)].id, serviceId: null, operation: 'inference',
    cycleId: `fixture-cognis-cycle-${Math.floor(index / 2) + 1}`, logicalId: `fixture-cognis-logical-${Math.floor(index / 2) + 1}`, status: index === 0 ? 'failed' : 'succeeded', cacheWrite: index === 0 ? null : 1500,
    detail: { ...(index === 0 ? fixtureAttempts[1].detail! : fixtureAttempts[0].detail!), retry: index % 2 === 1, fallback: index === 1, service: null, operation: 'inference', sessionId: sessions[Math.floor(index / 2)].id, parameters: fixtureParameters, identifiers: query.mode === 'project' ? null : fixtureAttempts[0].detail!.identifiers }
  }));
  const raw = [...cognisRows, ...(query.mode === 'project' ? [] : serviceRows)].filter((row) => (!query.sessionId || row.sessionId === query.sessionId) &&
    (!query.cycleId || row.cycleId === query.cycleId) && (!query.state || row.status === query.state) &&
    (!query.serviceId || row.serviceId === query.serviceId) && (!query.operation || row.operation === query.operation) &&
    (!query.filters.provider || row.providerId === query.filters.provider) && (!query.filters.model || row.model === query.filters.model) &&
    (!query.preciseStart || row.startedAt! >= query.preciseStart) && (!query.preciseEnd || row.startedAt! < query.preciseEnd));
  const offset = /^fixture-page-\d+$/.test(query.cursor ?? '') ? Number(query.cursor!.split('-').at(-1)) : 0;
  const page = raw.slice(offset, offset + 3);
  return { ...base, availability, sessions,
    buckets: base.buckets.map((bucket, index) => ({ ...bucket, start: `${bucket.start}T00:00:00.000Z`, end: new Date(Date.parse(`${bucket.start}T00:00:00Z`) + 86400000).toISOString(),
      source: 'retained', totals: (() => { const totals = fixtureView({ ...query, filters: { ...query.filters, start: bucket.start, end: bucket.start } }).totals; return { ...totals, cacheRatioRead: totals.cacheRead }; })(), performance: fixtureDayPerformance(index), costs: [bucket.cost] })),
    requestPage: { rows: page, nextCursor: offset + 3 < raw.length ? `fixture-page-${offset + 3}` : null, availability },
    coverage: { captureState: 'gaps', dropped: 3, gaps: 1, epoch: '2026-10-01T00:00:00Z', projectionState: 'lagging', capturedThrough: '2026-10-29T10:30:00Z', projectedThrough: '2026-10-29T10:29:10Z', projectionLagSeconds: 50, outstanding: 4, limitations: ['FIXTURE-BACKED deterministic synthetic screen data. No PostgreSQL or live backend qualification.'] },
    quota: query.mode === 'project' ? null : { provider: query.filters.provider || 'OpenAI', window: '5h / 7d', nextCursor: null, limitations: ['Synthetic stored observations; no provider was refreshed.'], samples: [0, 1, 2, 5, 6, 9].map((index) => ({
      at: `2026-10-${String(19 + index).padStart(2, '0')}T12:00:00Z`, account: index > 5 ? 'fixture-account-generation-2' : 'fixture-account-generation-1', window: '5h', source: 'synthetic provider response', sampleCount: 1,
      utilization: (index > 5 ? (index - 5) * 13 : 15 + index * 12).toFixed(2), remaining: (100 - (index > 5 ? (index - 5) * 13 : 15 + index * 12)).toFixed(2), usedPercent: null, resetAt: index > 5 ? '2026-10-25T00:00:00Z' : '2026-10-19T00:00:00Z', reset: index === 6
    })) }
  };
}

import { request } from '$lib/api/client';
import { formatCompactDecimalMoney } from './decimal';
import type * as W from './wire';
import { boundaries, localDate, localMidnight } from './local-time';
import { purposeLabel, SYSTEM_AGENT_LABEL } from './system-work';

export type BreakdownRange = 'auto' | '24h' | '7d' | '30d';

export interface BreakdownRow {
  id: string | null;
  label: string;
  cost: string;
  costValue: number;
  attempts: number;
  input: number | null;
  output: number | null;
  cached: number | null;
  /** Calls with usage but no price (failed calls without usage are not counted). */
  unpriced: number;
}

/** Failed or stopped calls grouped by model and reason. */
export interface BreakdownIssue {
  key: string;
  model: string;
  reason: string;
  /** Calls of this kind, retries included. */
  calls: number;
  /** True when the provider reported a problem (as opposed to a user stop). */
  failed: boolean;
}

/** Chat sidebar breakdown of one conversation (tree): cost over time and where it went. */
export interface ConversationBreakdown {
  unit: 'hour' | 'day';
  /** Start of the charted window. */
  from: string;
  points: { start: string; cost: number | null; tokens: number | null }[];
  currency: string | null;
  conversations: BreakdownRow[];
  agents: BreakdownRow[];
  models: BreakdownRow[];
  /** What the requests without an agent were for (compaction, titles, ...). */
  system: BreakdownRow[];
  issues: BreakdownIssue[];
  /** Sections whose read failed; the caller keeps what it showed before. */
  missing: ('conversations' | 'agents' | 'models' | 'system' | 'issues')[];
}

const DAY = 86_400_000, HOUR = 3_600_000;
/** The server reads hourly data for at most 90 days. */
const HISTORY_DAYS = 90;
const RANGE_MS: Record<Exclude<BreakdownRange, 'auto'>, number> = { '24h': DAY, '7d': 7 * DAY, '30d': 30 * DAY };

function post<T>(body: W.Query, signal: AbortSignal): Promise<T> {
  return request<T>('/api/v1/usage/query', { method: 'POST', body: JSON.stringify(body), signal });
}
function cost(metrics: W.Metrics): { amount: string | null; currency: string | null } {
  const known = metrics.costs.find((item) => item.known_amount !== null);
  return { amount: known?.known_amount ?? null, currency: known?.currency ?? null };
}
function tokens(metrics: W.Metrics): number | null {
  return metrics.tokens.input === null || metrics.tokens.output === null ? null : metrics.tokens.input + metrics.tokens.output;
}
const EMPTY: Record<string, string> = { agent: SYSTEM_AGENT_LABEL, model: 'Unknown model', conversation: 'Outside conversations' };

function rows(response: W.QueryResponse | null, dimension: string): BreakdownRow[] {
  return (response?.groups ?? []).map((group) => {
    const value = cost(group.metrics);
    let display = '—';
    if (value.amount !== null && value.currency) { try { display = formatCompactDecimalMoney(value.amount, value.currency); } catch { display = '—'; } }
    const label = dimension === 'purpose' ? purposeLabel(group.key) : group.label ?? group.key ?? EMPTY[dimension] ?? 'Unknown';
    return { id: group.key, label, cost: display, costValue: Number(value.amount ?? 0),
      attempts: group.metrics.finalized_attempts, input: group.metrics.tokens.input, output: group.metrics.tokens.output,
      cached: group.metrics.tokens.cache_read,
      unpriced: Math.max(0, group.metrics.unpriced_attempts - group.metrics.unknown_usage_attempts) };
  });
}

const STATUS_REASON: Record<number, string> = {
  400: 'Rejected request', 401: 'Not signed in to provider', 402: 'Provider billing (payment required)',
  403: 'Access denied by provider', 404: 'Model not found', 408: 'Timed out', 413: 'Request too large',
  429: 'Rate limited', 500: 'Provider error', 502: 'Provider unavailable', 503: 'Provider unavailable', 504: 'Provider timed out'
};
const CATEGORY_REASON: Record<string, string> = {
  authentication: 'Not signed in to provider', rate_limit: 'Rate limited', timeout: 'Timed out', connection: 'Connection failed',
  cancelled: 'Stopped', invalid_request: 'Rejected request', provider: 'Provider error', internal: 'Cognis error'
};

export function issueReason(attempt: Pick<W.Attempt, 'status_code' | 'error_category' | 'state'>): string {
  if (attempt.state === 'cancelled') return 'Stopped';
  return (attempt.status_code && STATUS_REASON[attempt.status_code]) || CATEGORY_REASON[attempt.error_category ?? ''] || 'Failed';
}

function issues(attempts: W.Attempt[]): BreakdownIssue[] {
  const groups = new Map<string, BreakdownIssue>();
  for (const attempt of attempts) {
    const model = attempt.resolved_model ?? attempt.requested_model ?? 'Unknown model';
    const reason = issueReason(attempt);
    const key = `${model}\u0001${reason}`;
    const item = groups.get(key) ?? { key, model, reason, calls: 0, failed: attempt.state === 'failed' };
    item.calls += 1;
    groups.set(key, item);
  }
  return [...groups.values()].sort((a, b) => Number(b.failed) - Number(a.failed) || b.calls - a.calls);
}

/**
 * Bounded reads over the conversation tree (the same membership as the
 * sidebar total). `auto` fits the chart to the conversation's lifetime: hourly
 * when its activity spans three days or less, daily otherwise.
 */
export async function loadConversationBreakdown(conversationId: string, inclusion: 'direct' | 'inclusive', signal: AbortSignal,
  range: BreakdownRange = 'auto', now = new Date()): Promise<ConversationBreakdown> {
  // All reads use the server's hourly grain so days follow the browser's time zone.
  const hourEnd = new Date(Math.ceil(now.getTime() / HOUR) * HOUR);
  const filters = { conversation_tree: { id: conversationId, inclusion } } as unknown as W.Filters;
  const base = { scope: { mode: 'personal' as const }, filters, limit: 100, pricing_basis: 'historical' as const, include_pending: false };
  const hourly = (start: number) => post<W.QueryResponse>({ ...base, start: new Date(start).toISOString(), end: hourEnd.toISOString(), view: 'trend', bucket: 'hour' }, signal);

  let from: number;
  let lifetime: W.QueryResponse | null = null;
  if (range === 'auto') {
    // The next local midnight keeps the lifetime read inside the hourly window.
    lifetime = await hourly(localMidnight(localDate(new Date(hourEnd.getTime() - HISTORY_DAYS * DAY)), 1).getTime());
    const active = lifetime.buckets.filter((bucket) => bucket.metrics.finalized_attempts > 0);
    from = active.length ? Date.parse(active[0].start) : hourEnd.getTime() - 7 * DAY;
  } else {
    from = now.getTime() - RANGE_MS[range];
  }
  const unit: 'hour' | 'day' = hourEnd.getTime() - from <= 3 * DAY ? 'hour' : 'day';
  from = unit === 'hour' ? Math.floor(from / HOUR) * HOUR : localMidnight(localDate(new Date(from))).getTime();
  const window = { start: new Date(from).toISOString(), end: hourEnd.toISOString() };
  const ranking = (group: W.Dimension, extra: Record<string, unknown> = {}) => post<W.QueryResponse>({ ...base, ...window,
    filters: { ...filters, ...extra } as W.Filters, view: 'ranking', bucket: 'hour', group_by: group, metric: 'cost', limit: 8 }, signal)
    .catch((error: unknown) => { if (signal.aborted) throw error; return null; });
  const [trend, conversations, agents, models] = await Promise.all([
    lifetime ?? hourly(from),
    inclusion === 'inclusive' ? ranking('conversation') : Promise.resolve(null),
    ranking('agent'),
    ranking('model')
  ]);
  const hasSystem = Boolean(agents?.groups.some((group) => group.key === null));
  // Failed and stopped calls of the listed conversations, with their reasons.
  const conversationIds = inclusion === 'inclusive' ? (conversations?.groups ?? []).map((group) => group.key).filter((id): id is string => Boolean(id)) : [conversationId];
  const [system, failures] = await Promise.all([
    hasSystem ? ranking('purpose', { agent_scope: 'system' }) : Promise.resolve(null),
    // Without the conversation list the failures cannot be read; report them missing.
    inclusion === 'inclusive' && !conversations ? Promise.resolve(null)
    : conversationIds.length
      ? request<W.AttemptPage>('/api/v1/usage/attempts/search', { method: 'POST', signal, body: JSON.stringify({
        scope: base.scope, ...window, pricing_basis: 'historical', limit: 100,
        filters: { conversation_ids: conversationIds, states: ['failed', 'cancelled'] } } satisfies W.DetailBody) })
        .catch((error: unknown) => { if (signal.aborted) throw error; return null; })
      : Promise.resolve({ items: [] } as unknown as W.AttemptPage)
  ]);

  // Continuous axis in local hours or local days; periods without requests are zero.
  const starts = boundaries(window.start, window.end, unit);
  const hours = trend.buckets.map((bucket) => ({ at: Date.parse(bucket.start), bucket }));
  const points: ConversationBreakdown['points'] = [];
  let currency: string | null = null;
  starts.forEach((at, index) => {
    const next = starts[index + 1] ?? hourEnd.getTime();
    const members = hours.filter((item) => item.at >= at && item.at < next).map((item) => item.bucket);
    let amount: number | null = 0, total: number | null = 0;
    for (const member of members) {
      const value = cost(member.metrics);
      currency ??= value.currency;
      // Known priced subtotal: an hour of only unpriced calls adds nothing.
      if (value.amount !== null) amount = (amount ?? 0) + Number(value.amount);
      const used = tokens(member.metrics);
      total = total === null || used === null ? null : total + used;
    }
    points.push({ start: new Date(at).toISOString(), cost: amount, tokens: total });
  });
  for (const response of [conversations, agents, models]) currency ??= response?.groups.map((group) => cost(group.metrics).currency).find(Boolean) ?? null;
  const missing: ConversationBreakdown['missing'] = [];
  if (inclusion === 'inclusive' && !conversations) missing.push('conversations');
  if (!agents) missing.push('agents');
  if (!models) missing.push('models');
  if (hasSystem && !system) missing.push('system');
  if (!failures) missing.push('issues');
  return { unit, from: window.start, points, currency,
    conversations: rows(conversations, 'conversation'), agents: rows(agents, 'agent'), models: rows(models, 'model'),
    system: rows(system, 'purpose'), issues: issues(failures?.items ?? []), missing };
}

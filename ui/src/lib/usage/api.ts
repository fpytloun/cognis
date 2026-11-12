import type { QuotaHistory, UsageAttempt, UsageAvailability, UsageDimension, UsageGroup, UsageLoadState, UsageOption, UsageQuery, UsageTotals } from './model';
import type * as W from './wire';

/**
 * Endpoint-independent boundary. Implement against the API owner's frozen
 * contract; never reconstruct totals by fetching attempts.
 */
export interface UsageAttemptTarget {
  sessionId?: string;
  service?: string;
  operation?: string;
}
export interface UsageSummarySelector { kind: 'conversation' | 'activity' | 'session'; id: string }
export interface UsageDataSource {
  query(query: UsageQuery, signal: AbortSignal): Promise<UsageLoadState>;
  attempts(query: UsageQuery, target: UsageAttemptTarget, cursor: string | null, signal: AbortSignal): Promise<{ rows: UsageAttempt[]; nextCursor: string | null; availability?: UsageAvailability }>;
  sidebar(items: UsageSummarySelector[], signal: AbortSignal): Promise<Record<string, UsageTotals | null>>;
  facets?(query: UsageQuery, dimension: UsageDimension, signal: AbortSignal): Promise<UsageOption[]>;
  providerHistory?(query: UsageQuery, provider: string, cursor: string | null, signal: AbortSignal): Promise<QuotaHistory>;
  /** Logical-request log (retries folded in), newest first. */
  requestLog?(query: UsageQuery, filters: W.Filters, scoped: W.ScopedFilters, cursor: string | null, signal: AbortSignal): Promise<{ items: W.RequestLog[]; nextCursor: string | null; availability?: UsageAvailability }>;
  requestDetail?(query: UsageQuery, selectionRef: string, signal: AbortSignal): Promise<{ request: W.RequestLog | null; attempts: UsageAttempt[] }>;
  /** Groups ranked by known priced cost (task, schedule, conversation, client, …). */
  ranking?(query: UsageQuery, group: W.Dimension, filters: W.Filters, scoped: W.ScopedFilters, signal: AbortSignal): Promise<UsageGroup[]>;
}

/** One read for the already visible page. No background pagination or N+1. */
export async function loadVisibleSummaries(source: UsageDataSource, items: UsageSummarySelector[], signal: AbortSignal): Promise<Record<string, UsageTotals | null>> {
  const unique = [...new Map(items.map((item) => [`${item.kind}:${item.id}`, item])).values()];
  if (unique.length > 100) throw new Error('Visible usage page exceeds 100 resources');
  return unique.length ? source.sidebar(unique, signal) : {};
}

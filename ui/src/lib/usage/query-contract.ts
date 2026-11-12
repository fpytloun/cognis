import type { UsageDimension, UsageQuery } from './model';
import { localMidnight } from './local-time';
import type * as W from './wire';

export class UsageQueryIssue extends Error {}
export const dimensions: Record<string, W.Dimension> = { user: 'subject', agent: 'agent', provider: 'provider', model: 'model', effort: 'effort', purpose: 'purpose', project: 'project', hook: 'hook', service: 'service', operation: 'operation',
  client: 'client', key: 'key', auth_type: 'auth_type', traffic_origin: 'traffic_origin' };
const filterKeys: Record<UsageDimension, keyof W.Filters> = { user: 'subject_emails', agent: 'agent_ids', provider: 'provider_ids', model: 'model_ids', effort: 'efforts', purpose: 'purposes', project: 'project_ids' };
const filterNames = new Set(['subject_emails', 'subject_keys', 'provider_owner_emails', 'provider_ids', 'model_ids', 'agent_ids', 'profile_ids', 'efforts', 'purposes', 'hook_ids', 'service_ids', 'operations', 'project_ids', 'conversation_ids', 'session_ids', 'task_ids', 'step_run_ids', 'turn_ids', 'cycle_ids', 'key_ids', 'client_ids', 'logical_request_ids', 'attempt_ids', 'error_categories', 'states', 'schedule_ids', 'traffic_origins', 'auth_types']);
export const errorCategories = ['authentication', 'rate_limit', 'timeout', 'connection', 'cancelled', 'invalid_request', 'provider', 'internal', 'unknown'];
const states = ['observed_start', 'inflight', 'succeeded', 'failed', 'cancelled', 'unknown'];

/** Intersect populations; an empty intersection must not become an unfiltered [] query. */
export function intersectFilters(left: W.Filters, right: W.Filters = {}): W.Filters {
  const result = { ...left };
  for (const [field, values] of Object.entries(right)) {
    if (!filterNames.has(field)) throw new UsageQueryIssue('An unrecognized detail filter cannot be discarded.');
    // Public UsageFilters serializes unused fields as null or empty arrays.
    if (values === null || Array.isArray(values) && !values.length) continue;
    if (!Array.isArray(values) || values.length > 100
      || values.some((value) => typeof value !== 'string' || !value || value.length > 254 || /[\s\x00-\x1f\x7f]/u.test(value))) {
      throw new UsageQueryIssue('A detail filter is invalid. Correct or clear it explicitly; no filter was removed.');
    }
    const key = field as keyof W.Filters;
    const overlap = left[key]?.length ? values.filter((value) => left[key]!.includes(value)) : values;
    if (!overlap.length) throw new UsageQueryIssue('The selection and active filters do not intersect. Clear the selection explicitly.');
    result[key] = overlap;
  }
  return result;
}
export function wireScope(query: UsageQuery): W.Scope {
  return { mode: query.mode, ...(query.projectScope ? { project_id: query.projectScope.id } : {}) };
}
/** Short ranges (up to three days) chart by hour; longer ranges by day. */
export const HOURLY_MAX_MS = 3 * 86_400_000;
export function bucketFor(start: Date, end: Date): 'hour' | 'day' {
  return end.getTime() - start.getTime() <= HOURLY_MAX_MS ? 'hour' : 'day';
}
/**
 * Longest range read at the server's hourly grain. Within it, the UI sums
 * hours into the browser's local days; longer ranges use UTC days.
 */
export const LOCAL_DAYS_MAX_MS = 90 * 86_400_000;
export function wireQuery(query: UsageQuery): W.Query {
  if (query.requiresSessionSearch && !query.sessionSearch) throw new UsageQueryIssue('This private session search is no longer available. Re-enter it or clear the search explicitly; no broader query was sent.');
  const validDay = (value: string | undefined) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value);
  if ((!query.preciseStart && !validDay(query.filters.start)) || (!query.preciseEnd && !validDay(query.filters.end))) throw new UsageQueryIssue('Invalid usage date range.');
  // Date filters are calendar days in the browser's time zone.
  let start = query.preciseStart ? new Date(query.preciseStart) : localMidnight(query.filters.start);
  let end = query.preciseEnd ? new Date(query.preciseEnd) : localMidnight(query.filters.end, 1);
  if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || start >= end) throw new UsageQueryIssue('Invalid usage date range.');
  let bucket: 'hour' | 'day' = 'hour';
  if (!query.preciseStart && !query.preciseEnd && end.getTime() - start.getTime() > LOCAL_DAYS_MAX_MS) {
    // Beyond hourly history the server aggregates whole UTC days.
    start = new Date(`${query.filters.start}T00:00:00.000Z`);
    end = new Date(`${query.filters.end}T00:00:00.000Z`);
    end.setUTCDate(end.getUTCDate() + 1);
    bucket = 'day';
  }
  const filters: W.Filters = {};
  for (const [key, name] of Object.entries(filterKeys)) {
    const value = query.filters[key as UsageDimension];
    if (value) filters[name] = [value];
  }
  if (query.projectScope) {
    if (query.filters.project && query.filters.project !== query.projectScope.id) throw new UsageQueryIssue('This project scope is fixed. Correct the project filter explicitly.');
    filters.project_ids = [query.projectScope.id];
  }
  if (query.mode === 'personal' && filters.subject_emails) throw new UsageQueryIssue('My usage cannot select another subject. Clear the user filter explicitly.');
  if (query.mode === 'project' && (query.clientId || query.keyId)) throw new UsageQueryIssue('Client and key identifiers are private. Clear those filters before querying project usage.');
  const scalars: [keyof W.Filters, string | undefined][] = [
    ['service_ids', query.serviceId], ['operations', query.operation], ['session_ids', query.sessionId],
    ['cycle_ids', query.cycleId], ['hook_ids', query.hookId], ['states', query.state],
    ['client_ids', query.clientId], ['key_ids', query.keyId],
  ];
  for (const [key, value] of scalars) if (value) filters[key] = [value];
  if (query.errorCategories?.length) filters.error_categories = query.errorCategories;
  if (query.logicalRequestIds?.length) filters.logical_request_ids = query.logicalRequestIds;
  if (query.state && !states.includes(query.state)) throw new UsageQueryIssue('Unsupported attempt state; no filter was removed.');
  if (query.errorCategories?.some((category) => !errorCategories.includes(category))) throw new UsageQueryIssue('Unsupported error category; no filter was removed.');
  const checked = intersectFilters({}, filters);
  return { scope: wireScope(query), start: start.toISOString(), end: end.toISOString(),
    filters: intersectFilters(checked, query.detailFilters), view: 'trend', bucket,
    limit: 100, pricing_basis: 'historical', include_pending: query.includePending ?? false };
}
export function detailBody(query: UsageQuery, filters = wireQuery(query).filters, cursor = query.cursor): W.DetailBody {
  if (query.requiresSelection && !query.selectionRef) throw new UsageQueryIssue('This protected selection is missing or expired. Return to its source and select it again; no broader query was sent.');
  if (query.requiresCursor && !cursor) throw new UsageQueryIssue('This cursor page is no longer available. Choose the first page explicitly; no broader query was sent.');
  const body = wireQuery(query);
  return { scope: body.scope, start: body.start, end: body.end, pricing_basis: body.pricing_basis,
    filters, limit: 100, ...(cursor ? { cursor } : {}), ...(query.selectionRef ? { selection_ref: query.selectionRef } : {}) };
}
export function requireSummaryPopulation(query: UsageQuery, _operation: 'summary' | 'facet' | 'ranking') {
  if (query.selectionRef || query.requiresSelection || query.sessionSearch || query.requiresSessionSearch) {
    throw new UsageQueryIssue('A request selection is active. Clear it or go back to see totals.');
  }
}
/** Session summaries are whole UTC days; selected local days map to the UTC days that cover them. */
export function sessionWindowIssue(query: UsageQuery, body: W.DetailBody): string | null {
  if (query.selectionRef) return 'Use this selection for exact requests, not another session summary.';
  // An exact drilled interval is never widened to whole days.
  const aligned = (value: string) => new Date(value).toISOString().slice(11) === '00:00:00.000Z';
  return (query.preciseStart || query.preciseEnd) && !(aligned(body.start) && aligned(body.end))
    ? 'Session totals cover whole days; this exact interval was preserved, not widened. Open its requests instead.' : null;
}
export function sessionWindow(body: W.DetailBody): W.DetailBody {
  const start = new Date(body.start), end = new Date(body.end);
  start.setUTCHours(0, 0, 0, 0);
  if (end.toISOString().slice(11) !== '00:00:00.000Z') { end.setUTCHours(0, 0, 0, 0); end.setUTCDate(end.getUTCDate() + 1); }
  return { ...body, start: start.toISOString(), end: end.toISOString() };
}
export function requireFilters(catalog: W.Catalog, filters: W.Filters, kind: 'summary' | 'detail') {
  const supported = kind === 'summary' ? catalog.summary_filters : catalog.detail_filters;
  const aliases: Partial<Record<keyof W.Filters, string>> = { subject_emails: 'subject', provider_ids: 'provider', model_ids: 'model', agent_ids: 'agent', profile_ids: 'profile', efforts: 'effort', purposes: 'purpose', hook_ids: 'hook', service_ids: 'service', operations: 'operation', project_ids: 'project', conversation_ids: 'conversation', session_ids: 'session' };
  if (Object.entries(filters).some(([field, values]) => values?.length && !supported.includes(field) && !supported.includes(aliases[field as keyof W.Filters] ?? field))) {
    throw new UsageQueryIssue(`An active filter is not supported for this ${kind} query. Use a supported detail view or clear it explicitly; no filter was discarded.`);
  }
}

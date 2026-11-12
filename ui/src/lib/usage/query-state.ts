import { localDate } from './local-time';
import { blankFilters, type UsageDrillPatch, type UsageQuery, type UsageReturnState, type UsageSurface } from './model';
const dimensions = ['provider', 'model', 'effort', 'agent', 'user', 'purpose', 'project'] as const;
export function readUsageQuery(url: URL, now: Date, project?: { id: string; label: string }): UsageQuery {
  const filters = blankFilters();
  filters.start = localDate(new Date(now.getFullYear(), now.getMonth(), 1));
  filters.end = localDate(now);
  for (const key of [...dimensions, 'start', 'end'] as const) {
    const value = url.searchParams.get(`usage_${key}`);
    if (value && (key !== 'start' && key !== 'end' || /^\d{4}-\d{2}-\d{2}$/.test(value))) filters[key] = value;
  }
  const scope = url.searchParams.get('usage_scope');
  const mode = project ? scope === 'personal' ? 'personal' : 'project' : scope === 'admin' ? 'admin' : 'personal';
  if (project && !filters.project) filters.project = project.id;
  const group = url.searchParams.get('usage_group');
  const groupBy = [...dimensions, 'hook', 'service', 'operation', 'client', 'key', 'auth_type', 'traffic_origin'].includes(group ?? '') ? group as UsageQuery['groupBy'] : 'agent';
  const surface = url.searchParams.get('usage_surface');
  return readUsageQueryFields(url, mode, groupBy, filters, surface, project);
}
function readUsageQueryFields(url: URL, mode: UsageQuery['mode'], groupBy: UsageQuery['groupBy'], filters: UsageQuery['filters'],
  surface: string | null, project?: { id: string; label: string }): UsageQuery {
  return { mode, groupBy, filters, projectScope: project, includePending: url.searchParams.get('usage_pending') === '1',
    surface: ['costs', 'performance', 'forecast', 'work', 'limits', 'companions', 'explorer'].includes(surface ?? '') ? surface as UsageQuery['surface'] : 'overview',
    explorerMode: ['requests', 'attempts'].includes(url.searchParams.get('usage_explorer') ?? '') ? url.searchParams.get('usage_explorer') as 'requests' | 'attempts' : 'sessions',
    sessionType: url.searchParams.get('usage_session_type') === 'proxy' ? 'proxy' : 'cognis',
    sessionSearch: url.searchParams.get('usage_session_type') !== 'proxy' ? url.searchParams.get('usage_search') ?? undefined : undefined,
    requiresSessionSearch: url.searchParams.get('usage_private_search') === '1',
    errorCategories: url.searchParams.getAll('usage_error_category'),
    logicalRequestIds: url.searchParams.getAll('usage_logical_request_id'),
    requiresSelection: url.searchParams.get('usage_selection') === '1',
    requiresCursor: url.searchParams.get('usage_paged') === '1',
    serviceId: url.searchParams.get('usage_service') ?? undefined, operation: url.searchParams.get('usage_operation') ?? undefined,
    ...Object.fromEntries(['sessionId', 'cycleId', 'state', 'clientId', 'keyId', 'cursor', 'selectedAttempt', 'selectedQuota', 'hookId', 'metric', 'contextLabel', 'returnTo', 'preciseStart', 'preciseEnd'].map((key) => [key, url.searchParams.get(`usage_${key}`) ?? undefined])) };
}
export function writeUsageQuery(url: URL, query: UsageQuery): URL {
  const next = new URL(url);
  for (const [key, value] of Object.entries(query.filters)) {
    if (value) next.searchParams.set(`usage_${key}`, value); else next.searchParams.delete(`usage_${key}`);
  }
  next.searchParams.set('usage_scope', query.mode);
  next.searchParams.set('usage_group', query.groupBy);
  next.searchParams.set('usage_surface', query.surface ?? 'overview');
  if (query.includePending) next.searchParams.set('usage_pending', '1'); else next.searchParams.delete('usage_pending');
  for (const [key, value] of [['service', query.serviceId], ['operation', query.operation]] as const) {
    if (value) next.searchParams.set(`usage_${key}`, value); else next.searchParams.delete(`usage_${key}`);
  }
  next.searchParams.set('usage_explorer', query.explorerMode ?? 'sessions');
  next.searchParams.set('usage_session_type', query.sessionType ?? 'cognis');
  for (const [name, values] of [['error_category', query.errorCategories], ['logical_request_id', query.logicalRequestIds]] as const) {
    next.searchParams.delete(`usage_${name}`);
    for (const value of values ?? []) next.searchParams.append(`usage_${name}`, value);
  }
  for (const key of ['sessionId', 'cycleId', 'state', 'clientId', 'keyId', 'selectedAttempt', 'selectedQuota', 'hookId', 'metric', 'returnTo', 'preciseStart', 'preciseEnd'] as const) {
    const value = query[key];
    if (value) next.searchParams.set(`usage_${key}`, value); else next.searchParams.delete(`usage_${key}`);
  }
  const marker = (key: string, present: boolean) => present ? next.searchParams.set(key, '1') : next.searchParams.delete(key);
  marker('usage_selection', Boolean(query.selectionRef || query.requiresSelection));
  marker('usage_paged', Boolean(query.cursor || query.requiresCursor));
  marker('usage_private_search', Boolean(query.sessionSearch || query.requiresSessionSearch));
  next.searchParams.delete('usage_search');
  if (query.contextLabel && !query.selectionRef && !query.requiresSelection) next.searchParams.set('usage_contextLabel', query.contextLabel); else next.searchParams.delete('usage_contextLabel');
  for (const name of ['usage_cursor', 'usage_selectionRef', 'usage_selection_ref', 'selection_ref']) next.searchParams.delete(name);
  return next;
}
export interface UsageHistoryState { usage?: { key: string; authority: string; context: UsageReturnState } }
function historyKey(query: UsageQuery) { return writeUsageQuery(new URL('http://localhost/usage'), query).search; }
/** Opaque selectors, cursors and private labels never enter URLs or shared caches. */
export function usageHistoryState(query: UsageQuery, authority: string): UsageHistoryState {
  return { usage: { key: historyKey(query), authority, context: {
    selectionRef: query.selectionRef, selectionType: query.selectionType, detailFilters: query.detailFilters, cursor: query.cursor,
    sessionSearch: query.sessionSearch, contextLabel: query.contextLabel, returnTo: query.returnTo, previous: query.returnState,
  } } };
}
export function restoreUsageHistory(query: UsageQuery, history: UsageHistoryState | undefined, authority: string): UsageQuery {
  const saved = history?.usage;
  if (!saved || saved.key !== historyKey(query) || saved.authority !== authority
    || !saved.context || typeof saved.context !== 'object' || Array.isArray(saved.context)) return query;
  const { previous, ...context } = saved.context;
  return { ...query, ...context, returnState: previous };
}
/** Presentation-only changes never reload an already authorized bounded page. */
export function usageRequestKey(query: UsageQuery): string {
  const { selectedAttempt, selectedQuota, metric, contextLabel, returnTo, returnState, ...request } = query;
  return JSON.stringify(request);
}
export function drillUsage(query: UsageQuery, patch: UsageDrillPatch, label: string, metric: string): UsageQuery {
  const returnTo = writeUsageQuery(new URL('http://localhost/usage'), { ...query, returnTo: undefined, contextLabel: undefined, selectedAttempt: undefined }).search;
  return { ...query, cursor: undefined, requiresCursor: false, selectedAttempt: undefined, surface: 'explorer', explorerMode: 'requests',
    ...patch, filters: { ...query.filters, ...patch.filters }, returnTo, contextLabel: label, metric,
    returnState: { selectionRef: query.selectionRef, selectionType: query.selectionType, detailFilters: query.detailFilters, cursor: query.cursor, sessionSearch: query.sessionSearch,
      contextLabel: query.contextLabel, returnTo: query.returnTo, previous: query.returnState } };
}
/**
 * Top-level tab navigation. The request explorer keeps its drill context; any
 * other view starts from the visible filters only, so a detail-only selection
 * (protected selection, session search, exact interval) never reaches a
 * summary query and no hidden narrowing carries over.
 */
export function enterSurface(query: UsageQuery, surface: UsageSurface): UsageQuery {
  const page = { surface, cursor: undefined, requiresCursor: false, selectedAttempt: undefined };
  if (surface === 'explorer') return { ...query, ...page };
  return { ...query, ...page,
    selectionRef: undefined, selectionType: undefined, requiresSelection: false,
    sessionSearch: undefined, requiresSessionSearch: false, detailFilters: undefined,
    returnTo: undefined, returnState: undefined, contextLabel: undefined, metric: undefined,
    preciseStart: undefined, preciseEnd: undefined, sessionId: undefined, cycleId: undefined,
    state: undefined, clientId: undefined, keyId: undefined, hookId: undefined,
    errorCategories: [], logicalRequestIds: [],
    ...(surface === 'companions' ? {} : { serviceId: undefined, operation: undefined }) };
}
export function backUsage(query: UsageQuery, now: Date): UsageQuery | null {
  if (!query.returnTo?.startsWith('?')) return null;
  const next = readUsageQuery(new URL(query.returnTo, 'http://localhost/usage'), now, query.projectScope);
  if (!query.returnState) return next;
  const { previous, ...context } = query.returnState;
  return { ...next, ...context, returnState: previous };
}

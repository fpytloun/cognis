/** Opt-in synthetic protocol boundary. Never imported by shipping routes. */
import { ApiError } from '../../src/lib/api/client';
import type { UsageTransport } from '../../src/lib/usage/adapter';
import { attempt, catalog, meta, metrics, requestPage, response, sessionPage, sessionRef } from '../../src/lib/usage/wire.test-data';
import type * as W from '../../src/lib/usage/wire';

export interface Packet { schema: string; value: unknown }
const schemas: Record<string, [string, string]> = {
  '/api/v1/usage/catalog': ['UsageCatalogRequest', 'UsageCatalog'],
  '/api/v1/usage/query': ['UsageQuery', 'UsageQueryResponse'],
  '/api/v1/usage/sessions': ['UsageSessionQuery', 'UsageSessionPage'],
  '/api/v1/usage/requests': ['UsageAttemptQuery', 'UsageRequestPage'],
  '/api/v1/usage/requests/detail': ['UsageRequestDetailQuery', 'UsageRequestDetail'],
  '/api/v1/usage/attempts/search': ['UsageAttemptQuery', 'UsageAttemptPage'],
};
const proxyRef = 'v2.usage-contract-proxy-fixture';
function scaledMetrics(factor: number): W.Metrics {
  const row = metrics();
  const scale = (record: object, keep: (key: string) => boolean = () => false) => {
    for (const [key, value] of Object.entries(record)) {
      if (typeof value === 'number' && !keep(key)) Reflect.set(record, key, value * factor);
    }
  };
  scale(row);
  for (const record of [row.states, row.tokens, row.token_known_attempts, row.prompt_cache]) scale(record);
  scale(row.performance, (key) => key.endsWith('_mean') || key.endsWith('_rate') || key.endsWith('_per_second'));
  for (const cost of row.costs) {
    scale(cost);
    cost.known_amount = (factor * .125).toFixed(3);
  }
  return row;
}
export function protocolFixture(phase: () => string) {
  const packets: Packet[] = [];
  const calls: { path: string; method: string }[] = [];
  const send: UsageTransport = async <T>(path: string, options: Parameters<UsageTransport>[1] = {}): Promise<T> => {
    if (!schemas[path]) throw new Error('The opt-in fixture does not implement this operation.');
    const body = JSON.parse(String(options.body)) as W.Query & W.SessionBody;
    calls.push({ path, method: options.method ?? 'GET' });
    packets.push({ schema: schemas[path][0], value: body });
    if (phase() === 'denied') throw new ApiError('Synthetic scope denial', { status: 403 });
    if (phase() === 'error') throw new ApiError('The usage database is unavailable. Try again.', {
      status: 503, code: 'usage_query_failed',
      details: { operation: 'Load usage', correlation_id: 'usage-fixture-diagnostic-17' }
    });
    if (phase() === 'expired' && path !== '/api/v1/usage/catalog') throw new ApiError('Synthetic invalid or expired selection', { status: 400 });
    const metadata = { ...meta(), scope: body.scope, private_resources_excluded: body.scope.mode === 'project' };
    const empty = phase() === 'empty';
    let value: unknown;
    if (path === '/api/v1/usage/catalog') value = catalog(body.scope);
    else if (path === '/api/v1/usage/query') {
      const source = response();
      const keys = body.group_by === 'provider' ? ['usage-fixture-provider-alpha', 'usage-fixture-provider-beta']
        : [`usage-fixture-${body.group_by ?? 'agent'}-alpha`, `usage-fixture-${body.group_by ?? 'agent'}-beta`];
      let totalFactor = 0;
      const groups = keys.map((key, index) => ({ key, label: index ? 'Fixture Beta' : 'Fixture Alpha', metrics: scaledMetrics(0) }));
      const buckets: W.QueryResponse['buckets'] = [];
      for (let day = new Date(body.start).valueOf(), index = 0; day < Date.parse(body.end); day += 86400000, index++) {
        const factor = 1 + index % 3;
        totalFactor += factor;
        const row = scaledMetrics(factor * groups.length);
        buckets.push({ start: new Date(day).toISOString(), end: new Date(Math.min(day + 86400000, Date.parse(body.end))).toISOString(),
          complete: index % 5 !== 0, source: 'retained', source_grain: 'day', metrics: row,
          groups: groups.map((group) => ({ ...group, metrics: scaledMetrics(factor) })) });
      }
      if (source.cost_breakdowns) {
        for (const dimension of source.cost_breakdowns.dimensions) {
          dimension.groups = empty ? [] : groups.map((group) => ({ ...group, metrics: scaledMetrics(totalFactor) }));
        }
      }
      value = { ...source, meta: metadata, total: scaledMetrics(empty ? 0 : totalFactor * groups.length),
        groups: empty ? [] : groups.map((group) => ({ ...group, metrics: scaledMetrics(totalFactor) })),
        buckets: empty ? [] : buckets };
    } else if (path === '/api/v1/usage/sessions') {
      const page = sessionPage(body.session_type, body.scope);
      const row = page.items[0];
      row.selection_ref = body.session_type === 'proxy' ? proxyRef : sessionRef;
      row.retained_start = body.start; row.retained_end = body.end;
      if (body.cursor) {
        if (row.session_id) row.session_id = 'usage-fixture-cognis-session-2';
        if (row.client_session_id) row.client_session_id = 'usage_fixture_second_label';
      }
      const match = !body.search || body.search === (row.session_type === 'proxy' ? row.client_session_id : row.session_id);
      value = { ...page, meta: metadata, items: empty || !match ? [] : page.items, next_cursor: empty || body.cursor ? null : 'usage-fixture-session-next' };
    } else {
      if (body.selection_ref && ![sessionRef, proxyRef, 'v2.usage-contract-logical-1', 'v2.usage-contract-logical-2'].includes(body.selection_ref)) throw new ApiError('Synthetic unknown selection', { status: 400 });
      let rows = Array.from({ length: 4 }, (_, index) => {
        const row = attempt();
        row.attempt_id = `usage-fixture-attempt-${index + 1}`;
        row.logical_request_id = `usage-fixture-logical-${Math.floor(index / 2) + 1}`;
        row.provider_id = 'usage-fixture-provider-alpha';
        row.state = index < 2 ? 'failed' : 'succeeded';
        if (index >= 2) { row.error_category = null; row.error_summary = null; row.status_code = 200; }
        row.session_id = index < 2 ? 'usage-fixture-cognis-session' : null;
        row.started_at = new Date(Date.parse(body.start) + 3_600_000 + index * 60_000).toISOString();
        row.finished_at = new Date(Date.parse(row.started_at) + 3_000).toISOString();
        row.selection_ref = `v2.usage-contract-logical-${Math.floor(index / 2) + 1}`;
        return row;
      });
      rows = rows.filter((row) => {
        if (row.started_at! < body.start || row.started_at! >= body.end) return false;
        if (body.selection_ref === sessionRef && !row.session_id || body.selection_ref === proxyRef && row.session_id) return false;
        if (body.selection_ref === 'v2.usage-contract-logical-1' && row.logical_request_id !== 'usage-fixture-logical-1') return false;
        if (body.selection_ref === 'v2.usage-contract-logical-2' && row.logical_request_id !== 'usage-fixture-logical-2') return false;
        const values: Partial<Record<keyof W.Filters, string | null | undefined>> = {
          provider_ids: row.provider_id, model_ids: row.resolved_model, session_ids: row.session_id,
          logical_request_ids: row.logical_request_id, cycle_ids: row.cycle_id, states: row.state,
          error_categories: row.error_category, service_ids: row.service_id, operations: row.operation,
          efforts: row.effort, client_ids: row.client_id, key_ids: row.key_id,
          project_ids: body.scope.project_id, subject_emails: row.subject_email, attempt_ids: row.attempt_id,
        };
        return Object.entries(body.filters ?? {}).every(([field, selected]) => !selected?.length || selected.includes(values[field as keyof W.Filters] ?? ''));
      });
      if (empty) rows = [];
      const offset = body.cursor ? 2 : 0;
      const pageRows = rows.slice(offset, offset + 2);
      const next = offset + 2 < rows.length ? 'usage-fixture-detail-next' : null;
      const visibleRows = pageRows.map((row) => {
        if (body.scope.mode !== 'project') return row;
        const { client_id, key_id, provider_request_id, ...reduced } = row;
        return { ...reduced, attempt_id: null, subject_email: null, session_id: null, cycle_id: null, logical_request_id: null };
      });
      const logicalRows = rows.filter((_, index) => index % 2 === 0).map((row): W.RequestLog => ({
        ...requestPage(body.scope).items[0], request_id: body.scope.mode === 'project' ? null : row.logical_request_id ?? null,
        started_at: row.started_at ?? body.start, state: row.state, selection_ref: row.selection_ref ?? null,
        traffic_origin: 'api', auth_type: 'api_key', requested_model: row.requested_model, service_id: row.service_id,
        attempt_count: 2, retry_count: 1, fallback_count: 0, tokens: row.tokens,
        completed_at: row.finished_at ?? null, provider_ids: row.provider_id ? [row.provider_id] : [],
        resolved_models: row.resolved_model ? [row.resolved_model] : [], request_path: '/v1/chat/completions',
        streaming: true, finish_reason: row.state === 'succeeded' ? 'stop' : null,
        error_summary: row.error_summary, unpriced_attempts: 2,
        costs: row.costs, usage_quality: row.usage_quality, latency_seconds: row.latency_seconds, ttft_seconds: row.ttft_seconds,
        http_status_code: row.status_code, error_category: row.error_category, timing: row.logical_timing ?? null,
        ...(body.scope.mode === 'project' ? {} : { client_id: 'usage-fixture-client', app_name: 'Fixture SDK', user_agent: 'Fixture SDK/1.0' })
      }));
      if (path === '/api/v1/usage/requests/detail') value = {
        meta: metadata, request: logicalRows[0] ?? null, attempts: visibleRows, next_cursor: next
      } satisfies W.RequestLogDetail;
      else if (path === '/api/v1/usage/requests' && 'pagination_unit' in body && body.pagination_unit === 'request') {
        value = { meta: metadata, pagination_unit: 'request', items: logicalRows, next_cursor: null } satisfies W.RequestLogPage;
        packets[packets.length - 1].schema = 'UsageRequestQuery';
      }
      else if (path === '/api/v1/usage/requests') value = { meta: metadata, pagination_unit: 'attempt', next_cursor: next,
        items: pageRows.map((row) => ({ ...requestPage(body.scope).items[0], request_id: body.scope.mode === 'project' ? null : row.logical_request_id,
          attempt_id: body.scope.mode === 'project' ? null : row.attempt_id, state: row.state,
          session_type: row.session_id ? 'cognis' : 'proxy', selection_ref: row.selection_ref,
          started_at: row.started_at, drill_filters: body.scope.mode === 'project' ? {} : { logical_request_ids: [row.logical_request_id!] } })) };
      else value = { meta: metadata, next_cursor: next, items: visibleRows };
    }
    packets.push({ schema: schemas[path][1], value });
    return value as T;
  };
  return { send, packets, calls };
}

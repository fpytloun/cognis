import { ApiError, request } from '$lib/api/client';
import type { UsageDataSource } from './api';
import { formatDecimalMoney } from './decimal';
import type { ModelParameter, ParameterName, ParameterProvenance, TimingMeasure, UsageAttemptDetail, UsageAvailability, UsageCost, UsageCostBand, UsageDimension, UsageLoadState, UsagePerformance, UsageQuery, UsageTotals, UsageView } from './model';
import { emptyGroupLabel } from './model';
import * as W from './wire';
import { dimensions as dimension, wireQuery, wireScope, detailBody, requireFilters, requireSummaryPopulation, sessionWindow, sessionWindowIssue, intersectFilters, UsageQueryIssue } from './query-contract';
export { wireQuery, wireScope } from './query-contract';

export type UsageTransport = typeof request;

/**
 * Forecast history: short selections fit the last 3 days hourly, longer ones
 * the last 30 days (or the selection, up to 90) daily, ending now. A fit needs
 * history even when the user is looking at today only.
 */
export function forecastHistory(body: Pick<W.Query, 'start' | 'end'>, now = new Date()): Pick<W.Query, 'start' | 'end' | 'bucket'> {
  const hour = 3_600_000, day = 24 * hour;
  const span = Date.parse(body.end) - Date.parse(body.start);
  const hourly = span <= 3 * day;
  const unit = hourly ? hour : day;
  const end = Math.ceil(now.getTime() / unit) * unit;
  const length = hourly ? 3 * day : Math.min(90 * day, Math.max(30 * day, Math.ceil(span / day) * day));
  return { start: new Date(end - length).toISOString(), end: new Date(end).toISOString(), bucket: hourly ? 'hour' : 'day' };
}
const unavailable: UsageCost = { amount: null, currency: null, basis: 'unknown' };
const noTiming = (): TimingMeasure => ({ totalMs: null, samples: 0 });
const parameterNames: ParameterName[] = ['temperature', 'top_p', 'top_k', 'max_tokens', 'max_output_tokens', 'max_completion_tokens', 'seed', 'frequency_penalty', 'presence_penalty', 'reasoning_effort', 'reasoning_budget_tokens', 'service_tier', 'response_format', 'tool_choice'];
const provenance: Record<W.Parameter['provenance'], ParameterProvenance> = { explicit: 'explicit', configured_default: 'configured default', provider_default: 'provider default', adapter_normalized: 'adapter normalized', unsupported: 'unsupported', unknown: 'unknown' };
function checkScope(meta: Pick<W.Meta, 'scope'>, query: UsageQuery) {
  if (!meta || meta.scope.mode !== query.mode || (meta.scope.project_id ?? null) !== (query.projectScope?.id ?? null)) throw new Error('Usage scope mismatch');
}
export function mapAvailability(value?: W.Availability): UsageAvailability | undefined {
  return value ? { resolution: value.resolution, cutoff: value.detail_retention_cutoff, configuredDays: value.configured_detail_days,
    grain: value.archive_grain, supportedDimensions: value.supported_dimensions, unsupportedFields: value.unsupported_fields } : undefined;
}
export function mapCost(value: W.Cost): UsageCost & { basis: UsageCostBand['basis'] } {
  if (value.known_amount !== null) formatDecimalMoney(value.known_amount, value.currency);
  return { amount: value.known_amount, currency: value.currency, basis: value.basis === 'provider_reported' ? 'provider_reported' : value.basis === 'configured_equivalent' ? 'api_equivalent' : 'api_estimate' };
}
function representative(costs: W.Cost[]): UsageCost { return costs.length === 1 ? mapCost(costs[0]) : { ...unavailable }; }
export function mapTotals(value: W.Metrics, availability?: W.Availability): UsageTotals {
  return { attempts: value.finalized_attempts, partial: value.partial_attempts, unknown: value.unknown_usage_attempts,
    unpriced: value.unpriced_attempts, partiallyPriced: value.partially_priced_attempts ?? null, unallocatable: value.unallocatable_cost_attempts ?? null,
    input: value.tokens.input, output: value.tokens.output, reasoning: value.tokens.reasoning,
    cacheRead: value.tokens.cache_read, cacheWrite: value.tokens.cache_write, cacheRatioRead: value.prompt_cache.cache_read_tokens,
    cacheEligibleInput: value.prompt_cache.eligible_input_tokens, cacheEligibleRequests: value.prompt_cache.eligible_requests,
    cacheHitRequests: value.prompt_cache.hit_requests, cost: representative(value.costs), costs: value.costs.map(mapCost), availability: mapAvailability(availability) };
}
/** All four marginals and their denominators come exclusively from this response. */
function mapBreakdowns(response: W.QueryResponse, catalog: W.Catalog): UsageCostBand[] {
  const breakdowns = response.cost_breakdowns;
  const fields = [
    { raw: 'subject', ui: 'user', filter: 'subject_keys' },
    { raw: 'agent', ui: 'agent', filter: 'agent_ids' },
    { raw: 'provider', ui: 'provider', filter: 'provider_ids' },
    { raw: 'model', ui: 'model', filter: 'model_ids' }
  ] as const;
  if (!response.total || breakdowns?.measure !== 'known_priced_subtotal' || breakdowns.complete_groups !== true
    || breakdowns.dimensions.length !== 4 || fields.some((field) => breakdowns.dimensions.filter((entry) => entry.dimension === field.raw).length !== 1)) {
    throw new UsageQueryIssue('The fixed complete cost breakdown response is unavailable. No ranking or broader population was substituted.');
  }
  const total = response.total;
  const key = (cost: W.Cost) => `${cost.currency}:${cost.basis}`;
  if (new Set(total.costs.map(key)).size !== total.costs.length) throw new Error('Duplicate cost bands');
  return total.costs.map((cost): UsageCostBand => ({
    ...mapCost(cost), currency: cost.currency, basis: mapCost(cost).basis,
    coverage: { priced: cost.priced_attempts, partial: cost.partially_priced_attempts ?? null, unpriced: total.unpriced_attempts, unallocatable: total.unallocatable_cost_attempts ?? null },
    dimensions: Object.fromEntries(fields.map((field) => {
      const marginal = breakdowns.dimensions.find((entry) => entry.dimension === field.raw)!;
      const identities = marginal.groups.map((group) => group.key);
      if (new Set(identities).size !== identities.length) throw new Error('Duplicate cost marginal identities');
      return [field.ui, { state: 'ready', complete: true, groups: marginal.groups.map((group, index) => {
        if (new Set(group.metrics.costs.map(key)).size !== group.metrics.costs.length
          || group.metrics.costs.some((band) => !total.costs.some((totalBand) => key(totalBand) === key(band)))) throw new Error('Incompatible marginal cost bands');
        const match = group.metrics.costs.find((band) => key(band) === key(cost));
        const amount = match ? mapCost(match).amount : null;
        const id = group.key;
        const representable = id !== null && id.length > 0 && id.length <= 254 && !/[\s\x00-\x1f\x7f]/u.test(id)
          && (catalog.detail_filters.includes(field.filter) || catalog.detail_filters.includes(field.raw));
        return { key: `${field.raw}:${index}`, id, label: group.label ?? id ?? emptyGroupLabel(field.raw), amount,
          drill: representable ? { detailFilters: { [field.filter]: [id] } } : null };
      }) }];
    })) as UsageCostBand['dimensions']
  }));
}
const duration = (value?: W.Duration): TimingMeasure => value ? { totalMs: value.seconds_sum === null ? null : value.seconds_sum * 1000, samples: value.known_samples } : noTiming();
export function mapPerformance(provider: string, value: W.Metrics): UsagePerformance {
  const p = value.performance;
  const logical = value.logical_performance;
  return { provider, serverLatency: duration(logical?.server_boundary), serverTtft: duration(logical?.server_first_send),
    providerSpan: { totalMs: p.latency_seconds_sum === null ? null : p.latency_seconds_sum * 1000, samples: p.latency_known_samples },
    providerTtft: { totalMs: p.ttft_seconds_sum === null ? null : p.ttft_seconds_sum * 1000, samples: p.ttft_known_samples },
    executorSpan: duration(logical?.executor_wait), clientLatency: noTiming(), clientTtft: noTiming(),
    cognisOverhead: duration(logical?.cognis_overhead),
    spans: logical ? (['routing', 'queue', 'provider_wait', 'executor_wait', 'accounting_hook', 'finalization', 'delivery'] as const).map((kind) => ({ kind, timing: duration(logical[kind]) })) : [],
    clientSources: logical ? [
      { source: 'Cognis UI · client monotonic', first: duration(logical.cognis_ui.first_chunk), last: duration(logical.cognis_ui.last_chunk) },
      { source: 'Authenticated client report · client monotonic', first: duration(logical.authenticated_client.first_chunk), last: duration(logical.authenticated_client.last_chunk) }
    ] : [],
    timingSource: 'Provider observations; server and actual client boundaries reported separately',
    generation: { outputTokens: p.generation_output_tokens, durationMs: p.generation_seconds_sum === null ? null : p.generation_seconds_sum * 1000, samples: p.generation_known_samples },
    endToEnd: { outputTokens: p.end_to_end_output_tokens, durationMs: p.end_to_end_seconds_sum === null ? null : p.end_to_end_seconds_sum * 1000, samples: p.end_to_end_known_samples },
    dispatchedAttempts: p.dispatched_attempts, failedAttempts: p.failed_dispatched_attempts,
    logicalCompleted: p.logical_completed_requests, logicalFailed: p.logical_failed_requests };
}
function parameter(value?: W.Parameter, name?: ParameterName) {
  if (!value) return null;
  if (typeof value.value === 'number') return Number.isFinite(value.value) ? value.value : null;
  if (value.value === null) return null;
  const allowed: Partial<Record<ParameterName, string[]>> = {
    reasoning_effort: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'auto'],
    service_tier: ['auto', 'default', 'standard', 'flex', 'priority', 'scale', 'batch'], response_format: ['text', 'json_object', 'json_schema'], tool_choice: ['auto', 'none', 'required', 'named']
  };
  return name && allowed[name]?.includes(value.value) ? value.value : null;
}
export function mapParameters(value?: W.Attempt['parameters']): ModelParameter[] {
  if (!value) return [];
  return parameterNames.filter((name) => value.requested[name] || value.effective[name]).map((name) => ({
    name, requested: parameter(value.requested[name], name), effective: parameter(value.effective[name], name),
    requestedProvenance: provenance[value.requested[name]?.provenance ?? 'unknown'] ?? 'unknown', provenance: provenance[value.effective[name]?.provenance ?? 'unknown'] ?? 'unknown'
  }));
}
export function mapAttempt(item: W.Attempt, query: UsageQuery) {
  const detail: UsageAttemptDetail = {
    mediaUsage: item.media_usage ?? [],
    inferenceOperation: item.inference_operation,
    lifecycle: item.state === 'succeeded' ? 'completed' : item.state, dispatched: item.dispatched_at ? true : null,
    service: item.service_id, operation: item.operation, sessionId: item.session_id,
    requestedModel: item.requested_model, provider: item.provider_id ?? 'Unknown', retry: item.retry, fallback: item.fallback, exceptionClass: item.exception_class,
    parameters: mapParameters(item.parameters),
    error: item.error_category || item.error_summary ? { category: item.error_category ?? 'unknown', code: item.provider_error_code ?? (item.status_code === null ? null : String(item.status_code)), summary: item.error_summary ?? 'Error details unavailable.' } : null,
    identifiers: query.mode === 'project' ? null : { client: item.client_id ?? null, key: item.key_id ?? null, request: item.provider_request_id ?? null },
    timings: {
      provider: item.provider_id ?? 'Unknown', serverLatency: noTiming(), serverTtft: noTiming(),
      providerSpan: { totalMs: item.latency_seconds === null ? null : item.latency_seconds * 1000, samples: item.latency_seconds === null ? 0 : 1 },
      providerTtft: { totalMs: item.ttft_seconds === null ? null : item.ttft_seconds * 1000, samples: item.ttft_seconds === null ? 0 : 1 },
      executorSpan: noTiming(), clientLatency: noTiming(), clientTtft: noTiming(), timingSource: item.provider_timing_source ?? 'unknown',
      generation: { outputTokens: item.tokens.output, durationMs: item.generation_seconds === null ? null : item.generation_seconds * 1000, samples: item.generation_tokens_per_second === null ? 0 : 1, rate: item.generation_tokens_per_second },
      endToEnd: { outputTokens: item.tokens.output, durationMs: null, samples: item.end_to_end_tokens_per_second === null ? 0 : 1, rate: item.end_to_end_tokens_per_second },
      dispatchedAttempts: item.dispatched_at ? 1 : null, failedAttempts: item.dispatched_at ? item.state === 'failed' ? 1 : 0 : null, logicalCompleted: null, logicalFailed: null
    }
  };
  const timing = item.logical_timing;
  if (timing?.server) {
    const s = timing.server;
    detail.timings.serverLatency = { totalMs: s.elapsed_seconds === null ? null : s.elapsed_seconds * 1000, samples: s.elapsed_seconds === null ? 0 : 1 };
    detail.timings.serverTtft = { totalMs: s.first_send_seconds === null ? null : s.first_send_seconds * 1000, samples: s.first_send_seconds === null ? 0 : 1 };
    detail.timings.cognisOverhead = { totalMs: s.cognis_overhead_seconds === null ? null : s.cognis_overhead_seconds * 1000, samples: s.cognis_overhead_seconds === null ? 0 : 1 };
    detail.serverPartition = { source: s.source, complete: s.partition_complete, elapsed: s.elapsed_seconds, overhead: s.cognis_overhead_seconds, firstSend: s.first_send_seconds, lastSend: s.last_send_seconds,
      spans: s.spans.map((span) => ({ kind: span.kind, start: span.start_seconds, end: span.end_seconds })) };
  }
  if (timing?.client?.correlation_verified === true && timing.client.clock === 'client_monotonic') {
    const c = timing.client;
    detail.timings.clientSources = [{ source: `${c.source} · ${c.clock}`, first: { totalMs: c.first_chunk_seconds === null ? null : c.first_chunk_seconds * 1000, samples: c.first_chunk_seconds === null ? 0 : 1 },
      last: { totalMs: c.last_chunk_seconds === null ? null : c.last_chunk_seconds * 1000, samples: c.last_chunk_seconds === null ? 0 : 1 } }];
  }
  return { id: item.attempt_id, startedAt: item.started_at, finishedAt: item.finished_at ?? null, sessionId: item.session_id, cycleId: item.cycle_id, logicalId: item.logical_request_id ?? null,
    serviceId: item.service_id, operation: item.operation, providerId: item.provider_id, status: item.state, cacheWrite: item.tokens.cache_write,
    source: query.selectionRef && query.selectionType ? (query.selectionType === 'proxy' ? 'Proxy selection' : 'Cognis selection') : item.session_id ? 'Verified Cognis session' : 'Not exposed for this attempt',
    cycle: item.cycle_id ?? 'Cycle unavailable', model: item.reported_model ?? item.resolved_model ?? item.requested_model ?? 'Unknown model',
    effort: item.effort, outcome: item.state === 'succeeded' ? 'Completed' : item.state.replaceAll('_', ' '), input: item.tokens.input,
    output: item.tokens.output, cacheRead: item.tokens.cache_read, cost: representative(item.costs), costs: item.costs.map(mapCost), detail };
}
function rankingRefusal(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null;
  if (error.code === 'mixed_cost_ranking') return withUsageReference(error, 'Costs are in more than one currency or price basis. Filter to one provider to rank by cost.');
  if (error.code === 'resource_detail_expired') return withUsageReference(error, 'Older work details expired. Choose a recent range.');
  return error.code === 'incomplete_cost_ranking'
    ? withUsageReference(error, 'Some requests are not priced. Filter to priced models to rank by cost.') : null;
}
function withUsageReference(error: ApiError, message: string): string {
  return typeof error.details?.operation === 'string' && typeof error.details?.correlation_id === 'string'
    ? `${error.details.operation} failed: ${message} · Reference ${error.details.correlation_id}` : message;
}
/** Preserve supported trends, but never soften authority or protected-selection failures. */
function rankingSectionError(error: unknown, signal: AbortSignal): string {
  signal.throwIfAborted();
  const refusal = rankingRefusal(error);
  if (refusal) return refusal;
  if (error instanceof ApiError && error.status === 422 && error.code === 'unsupported_usage_dimension') return withUsageReference(error, 'This breakdown is unavailable. Choose another grouping.');
  if (error instanceof ApiError && error.status >= 500) return usageErrorMessage(error);
  throw error;
}
/**
 * When the shown data was current: the projection lag reported with this
 * response, measured from now. Without a lag the timestamp is unknown.
 */
export function dataFreshness(meta: W.Meta, now = Date.now()): string {
  if (meta.observed_through) return meta.observed_through;
  const lag = meta.projection?.lag_seconds;
  if (typeof lag === 'number' && Number.isFinite(lag) && lag >= 0) return new Date(now - lag * 1000).toISOString();
  // A ready projection with no reported lag has applied every captured observation.
  return meta.projection?.state === 'ready' ? new Date(now).toISOString() : 'Unknown';
}
export function usageErrorMessage(error: unknown): string {
  if (error instanceof UsageQueryIssue) return error.message;
  if (error instanceof ApiError) {
    if (typeof error.details?.operation === 'string' && typeof error.details?.correlation_id === 'string') return withUsageReference(error, error.message);
    if ([401, 403].includes(error.status)) return 'Load usage failed: access denied. Choose My usage.';
    const ranking = rankingRefusal(error);
    if (ranking) return ranking;
    if ([404, 501, 503].includes(error.status)) return 'Load usage failed: service unavailable. Try again.';
    if ([400, 410, 422].includes(error.status)) return 'This selection has expired or is invalid. Clear the filters or go back.';
    return `Load usage failed (HTTP ${error.status}). Try again or narrow the date range.`;
  }
  // No HTTP response: the request never reached Cognis.
  return 'Load usage failed: Cognis could not be reached. Check the connection and try again.';
}
export function createUsageDataSource(send: UsageTransport = request): UsageDataSource {
  const post = <T>(path: string, body: unknown, signal: AbortSignal) => send<T>(path, { method: 'POST', body: JSON.stringify(body), signal });
  const queryCall = async (body: W.Query, signal: AbortSignal) => {
    signal.throwIfAborted();
    const response = await post<W.QueryResponse>('/api/v1/usage/query', body, signal);
    signal.throwIfAborted();
    return response;
  };
  const readPage = async (query: UsageQuery, target: { sessionId?: string; service?: string; operation?: string }, cursor: string | null, signal: AbortSignal) => {
    const filters = intersectFilters(wireQuery(query).filters, { ...(target.sessionId ? { session_ids: [target.sessionId] } : {}), ...(target.service ? { service_ids: [target.service] } : {}), ...(target.operation ? { operations: [target.operation] } : {}) });
    const page = await post<W.AttemptPage>('/api/v1/usage/attempts/search', detailBody(query, filters, cursor ?? undefined), signal);
    checkScope(page.meta, query);
    const expired = ['detail_expired', 'aggregate_only'].includes(page.meta.availability?.resolution ?? '');
    return { meta: page.meta, rows: expired ? [] : page.items.map((item) => mapAttempt(item, query)), nextCursor: expired ? null : page.next_cursor, availability: mapAvailability(page.meta.availability) };
  };
  const metaView = (query: UsageQuery, meta: W.Meta): UsageView => ({
    status: meta.accounting_degraded ? 'degraded' : meta.projection?.state !== 'ready' ? 'partial' : 'ready',
    scopeLabel: query.projectScope?.label ?? (query.mode === 'admin' ? 'All users' : 'My usage'),
    scopeDescription: query.mode === 'project' ? 'Current project-visible resources only; private resources, including your own, are excluded.' : query.mode === 'admin' ? 'Explicit all-user usage permission only. This does not grant transcript or delegated-tool access.' : 'Own usage across authorized providers. Provider ownership does not reveal other users.',
    freshness: dataFreshness(meta), coverageStart: meta.coverage_start ?? 'Unknown', pricing: 'historical',
    totals: null, facets: { user: [], agent: [], provider: [], model: [], effort: [], purpose: [], project: [] }, groups: [], sessions: [], buckets: [], quota: null,
    availability: mapAvailability(meta.availability),
    coverage: { captureState: meta.capture?.state ?? 'unknown', dropped: meta.capture?.dropped_observations ?? null, gaps: meta.capture?.gap_incidents ?? null, epoch: meta.capture?.epoch_started_at ?? null,
      projectionState: meta.projection?.state ?? 'unavailable', capturedThrough: meta.projection?.captured_through ?? null, projectedThrough: meta.projection?.projected_through ?? null,
      projectionLagSeconds: meta.projection?.lag_seconds ?? null, outstanding: meta.projection?.unprojected_observations ?? null, limitations: meta.limitations }
  });
  return {
    async query(query, signal): Promise<UsageLoadState> {
      try {
        const body = wireQuery(query);
        const surface = query.surface ?? 'overview';
        const catalog = await post<W.Catalog>('/api/v1/usage/catalog', { scope: body.scope }, signal);
        checkScope(catalog, query);
        if (surface === 'explorer') {
          requireFilters(catalog, body.filters, 'detail');
          const detail = detailBody(query);
          const mode = query.explorerMode ?? 'sessions';
          if (mode === 'sessions') {
            const kind = query.sessionType ?? 'cognis';
            if (!catalog.selections.session_kinds.includes(kind) || !catalog.session_types[kind]) throw new UsageQueryIssue('This session source is unavailable in the current scope catalogue.');
            const issue = sessionWindowIssue(query, detail);
            if (issue) throw new UsageQueryIssue(issue);
            if (kind === 'proxy' && query.sessionSearch && query.mode === 'project') throw new UsageQueryIssue('Proxy-label search is private-scope only. The label was not treated as a Cognis session ID or discarded.');
            const page = await post<W.SessionPage>('/api/v1/usage/sessions', { ...sessionWindow(detail), session_type: kind, ...(query.sessionSearch ? { search: query.sessionSearch } : {}) } satisfies W.SessionBody, signal);
            checkScope(page.meta, query);
            const expired = ['detail_expired', 'aggregate_only'].includes(page.meta.availability?.resolution ?? '');
            return { state: 'ready', view: { ...metaView(query, page.meta), catalog,
              sessions: expired ? [] : page.items.map((item) => ({ ...mapTotals(item.metrics, page.meta.availability),
                id: item.session_type === 'cognis' ? item.session_id : null,
                label: item.session_type === 'proxy' ? item.client_session_id ?? 'API client session' : item.session_label ?? item.session_id ?? 'Unnamed session',
                agent: 'Not provided', purpose: 'Not provided', transcriptHref: null,
                sessionType: item.session_type, sessionLabel: item.client_session_id, selectionRef: item.selection_ref,
                totalsScope: item.totals_scope, drillFilters: item.drill_filters, retainedStart: item.retained_start, retainedEnd: item.retained_end })),
              sessionPage: { nextCursor: expired ? null : page.next_cursor, availability: mapAvailability(page.meta.availability) } } };
          }
          if (query.sessionSearch) throw new UsageQueryIssue('Resolve this exact session search in Sessions and select a returned row, or clear the search explicitly. No broader request query was sent.');
          if (mode === 'attempts') {
            const page = await readPage(query, {}, query.cursor ?? null, signal);
            return { state: 'ready', view: { ...metaView(query, page.meta), catalog, requestPage: page } };
          }
          const page = await post<W.RequestPage>('/api/v1/usage/requests', detail, signal);
          checkScope(page.meta, query);
          const expired = ['detail_expired', 'aggregate_only'].includes(page.meta.availability?.resolution ?? '');
          return { state: 'ready', view: { ...metaView(query, page.meta), catalog, logicalRequestPage: {
            rows: expired ? [] : page.items.map((item) => ({
              requestId: item.request_id, attemptId: item.attempt_id,
              sessionType: item.session_type, selectionRef: item.selection_ref,
              logicalState: item.logical_outcome, logicalStartedAt: item.logical_request_started_at, startedAt: item.started_at,
              attemptState: item.state, drillFilters: item.drill_filters,
            })), nextCursor: expired ? null : page.next_cursor, paginationUnit: page.pagination_unit,
            availability: mapAvailability(page.meta.availability) } } };
        }
        if (surface === 'limits') throw new Error('Limits requires an explicit stored-history request');
        requireSummaryPopulation(query, 'summary');
        if (!catalog.summary_population_available) throw new UsageQueryIssue('The current scope catalogue reports that summary accounting is unavailable. No empty or partial global population was substituted.');
        requireFilters(catalog, body.filters, 'summary');
        const group = surface === 'companions' ? query.serviceId ? 'operation' : 'service' : dimension[query.groupBy];
        if (surface !== 'overview' && surface !== 'forecast' && surface !== 'work' && !catalog.grouping_dimensions.includes(group)) throw new UsageQueryIssue('This grouping is unavailable in the current scope catalogue. Choose a supported grouping explicitly.');
        const primary: W.Query = surface === 'companions' && !query.serviceId ? { ...body, view: 'ranking', group_by: 'service', metric: 'attempts' }
          : surface === 'overview' ? { ...body, view: 'cost_breakdowns' }
          : surface === 'forecast' ? { ...body, ...forecastHistory(body), view: 'trend' }
          // Work loads its own rankings; the shared totals keep the header and freshness current.
          : surface === 'work' ? { ...body, view: 'overview' }
          // Top groups are chosen by request count: cost ranking refuses whenever any request is unpriced.
          : { ...body, view: 'trend', group_by: group, metric: 'attempts' };
        let response: W.QueryResponse;
        let primaryRefusal: string | null = null;
        let ungroupedRecovery = false;
        try {
          response = await queryCall(primary, signal);
        } catch (error) {
          signal.throwIfAborted();
          primaryRefusal = rankingRefusal(error);
          if (!primaryRefusal || surface === 'overview') throw error;
          // Shape-only recovery: preserve the complete request population and metric.
          // All-service observations must retain service grouping, not become overall traffic.
          const { group_by: _group, ...unranked } = primary;
          ungroupedRecovery = !(surface === 'companions' && !query.serviceId);
          response = await queryCall({ ...unranked, view: 'trend', ...(ungroupedRecovery ? {} : { group_by: 'service' }) }, signal);
        }
        checkScope(response.meta, query);
        const meta = response.meta;
        const facets: UsageView['facets'] = { user: [], agent: [], provider: [], model: [], effort: [], purpose: [], project: [] };
        const view: UsageView = {
          status: meta.accounting_degraded ? 'degraded' : meta.projection && meta.projection.state !== 'ready' ? 'partial' : 'ready',
          scopeLabel: query.projectScope?.label ?? (query.mode === 'admin' ? 'All users' : 'My usage'),
          scopeDescription: query.mode === 'project' ? 'Current project-visible authorized resources only. Private resources, including your own, are excluded.' : query.mode === 'admin' ? 'Explicit all-user usage permission only. This does not grant transcript or delegated-tool access.' : 'Own usage across authorized providers. Provider ownership does not reveal other users.',
          freshness: dataFreshness(meta), coverageStart: meta.coverage_start ?? 'Unknown', pricing: 'historical',
          totals: response.total ? mapTotals(response.total, meta.availability) : null, facets, groups: [], sessions: [], quota: null, catalog,
          groupBy: ungroupedRecovery ? undefined : surface === 'companions' ? query.serviceId ? 'operation' : 'service' : query.groupBy,
          sectionErrors: primaryRefusal ? [primaryRefusal] : undefined,
          buckets: response.buckets.map((bucket) => ({ start: bucket.start, end: bucket.end, complete: bucket.complete, source: bucket.source === 'archive' || bucket.source === 'mixed' ? bucket.source : 'retained',
            totals: mapTotals(bucket.metrics), performance: mapPerformance('Observed population', bucket.metrics), tokens: bucket.metrics.tokens.input === null || bucket.metrics.tokens.output === null ? null : bucket.metrics.tokens.input + bucket.metrics.tokens.output,
            cost: representative(bucket.metrics.costs), costs: bucket.metrics.costs.map(mapCost), costComplete: bucket.metrics.costs.length === 1 && bucket.metrics.costs[0].quality === 'complete',
            groups: bucket.groups?.map((item) => ({ id: item.key, label: item.label ?? item.key ?? emptyGroupLabel(group),
              totals: mapTotals(item.metrics), performance: mapPerformance(item.label ?? item.key ?? emptyGroupLabel(group), item.metrics),
              costComplete: item.metrics.costs.length === 1 && item.metrics.costs[0].quality === 'complete' })) })),
          availability: mapAvailability(meta.availability),
          coverage: { captureState: meta.capture?.state ?? 'unknown', dropped: meta.capture?.dropped_observations ?? null, gaps: meta.capture?.gap_incidents ?? null, epoch: meta.capture?.epoch_started_at ?? null,
            projectionState: meta.projection?.state ?? 'unavailable', capturedThrough: meta.projection?.captured_through ?? null, projectedThrough: meta.projection?.projected_through ?? null,
            projectionLagSeconds: meta.projection?.lag_seconds ?? null, outstanding: meta.projection?.unprojected_observations ?? null, limitations: meta.limitations },
          lifecycle: response.total ? { observedAttempts: response.total.observed_attempts ?? null, observedStart: response.total.states.observed_start ?? null, inflight: response.total.states.inflight,
            dispatchedKnown: response.total.performance.dispatched_attempts, completed: response.total.states.succeeded, failed: response.total.states.failed, cancelled: response.total.states.cancelled, unknown: response.total.states.unknown } : undefined,
          forecast: response.forecast ? { sampleStart: response.forecast.sample_start, sampleEnd: response.forecast.sample_end, horizonSeconds: response.forecast.horizon_seconds,
            tokens: Object.entries(response.forecast.tokens).map(([field, value]) => ({ field, value: value!.available ? value!.projected_tokens : null, reason: value!.reason })),
            cost: response.forecast.cost.available && response.forecast.cost.projected_cost ? mapCost(response.forecast.cost.projected_cost) : null, costReason: response.forecast.cost.reason, limitations: response.forecast.limitations } : null
        };
        if (surface === 'overview') {
          view.groupBy = undefined;
          view.costBreakdowns = mapBreakdowns(response, catalog);
          // Separate bounded trend observation; never replace the canonical marginal total.
          view.buckets = [];
          try {
            const trend = await queryCall({ ...body, view: 'trend' }, signal);
            checkScope(trend.meta, query);
            view.buckets = trend.buckets.map((bucket) => ({
              start: bucket.start, end: bucket.end, complete: bucket.complete,
              source: bucket.source === 'archive' || bucket.source === 'mixed' ? bucket.source : 'retained',
              totals: mapTotals(bucket.metrics), cost: representative(bucket.metrics.costs), costs: bucket.metrics.costs.map(mapCost),
              tokens: bucket.metrics.tokens.input === null || bucket.metrics.tokens.output === null ? null : bucket.metrics.tokens.input + bucket.metrics.tokens.output
            }));
            view.overviewTrendNote = `Timeline is a separate ungrouped observation through ${trend.meta.observed_through ?? 'an unknown timestamp'}; it is not the four marginal charts' read snapshot.`;
          } catch (error) {
            if (signal.aborted || error instanceof ApiError && [401, 403].includes(error.status)) throw error;
            // A scope mismatch is not a recoverable section failure.
            if (!(error instanceof ApiError) || error.status < 500) throw error;
            view.overviewTrendNote = usageErrorMessage(error);
          }
        }
        if (surface === 'companions') {
          const group = query.serviceId ? 'operation' : 'service';
          try {
            const ranking = query.serviceId && !primaryRefusal ? await queryCall({ ...body, view: 'ranking', group_by: group, metric: 'attempts' }, signal) : response;
            checkScope(ranking.meta, query);
            if (query.serviceId && !primaryRefusal && catalog.grouping_dimensions.includes('model')) {
              // Which models the service spends on, beside which operations.
              const models = await queryCall({ ...body, view: 'ranking', group_by: 'model', metric: 'attempts', limit: 20 }, signal);
              checkScope(models.meta, query);
              view.serviceModels = models.groups.map((g) => ({ ...mapTotals(g.metrics), id: g.key, label: g.label ?? g.key ?? emptyGroupLabel('model'), performance: mapPerformance(g.label ?? g.key ?? emptyGroupLabel('model'), g.metrics) }));
            }
            view.groups = ranking.groups.map((g) => ({ ...mapTotals(g.metrics), id: g.key, label: g.label ?? g.key ?? emptyGroupLabel(group), performance: mapPerformance(g.label ?? g.key ?? emptyGroupLabel(group), g.metrics) }));
            view.companions = ranking.groups.filter((g) => g.key !== null).map((g) => ({ service: query.serviceId ?? g.key!, operation: query.serviceId ? g.key! : '', finalized: g.metrics.finalized_attempts,
              pending: g.metrics.states.inflight === null || g.metrics.states.observed_start == null ? null : g.metrics.states.inflight + g.metrics.states.observed_start,
              sessionless: null, input: g.metrics.tokens.input, output: g.metrics.tokens.output, cost: representative(g.metrics.costs), costs: g.metrics.costs.map(mapCost) }));
          } catch (error) {
            // Scope/protected-selector failures are not partial section results.
            if (!(error instanceof ApiError) || !rankingRefusal(error)) throw error;
            signal.throwIfAborted();
            (view.sectionErrors ??= []).push(rankingRefusal(error)!);
          }
        } else if (!primaryRefusal && (surface === 'performance' || surface === 'costs')) {
          const supported = (key: W.Dimension) => meta.availability?.resolution !== 'aggregate_only' || meta.availability.supported_dimensions.includes(key);
          const empty = () => Promise.resolve({ ...response, groups: [] });
          const results = await Promise.allSettled([
            supported(dimension[query.groupBy]) ? queryCall({ ...body, view: 'ranking', group_by: dimension[query.groupBy], limit: 100, metric: 'attempts' }, signal) : empty()
          ]);
          for (const r of results) if (r.status === 'rejected' && r.reason instanceof ApiError && [401, 403].includes(r.reason.status)) throw r.reason;
          results.forEach((r) => {
            if (r.status === 'rejected') {
              (view.sectionErrors ??= []).push(rankingSectionError(r.reason, signal)); return;
            }
            checkScope(r.value.meta, query);
            view.groups = r.value.groups.map((g) => ({ ...mapTotals(g.metrics), id: g.key, label: g.label ?? g.key ?? emptyGroupLabel(dimension[query.groupBy]), performance: mapPerformance(g.label ?? g.key ?? emptyGroupLabel(dimension[query.groupBy]), g.metrics) }));
            if (surface === 'performance') view.performance = r.value.groups.map((g) => mapPerformance(g.label ?? g.key ?? emptyGroupLabel(dimension[query.groupBy]), g.metrics));
          });
        }
        signal.throwIfAborted();
        return { state: 'ready', view };
      } catch (error) { if (signal.aborted) throw error; return { state: 'error', message: usageErrorMessage(error) }; }
    },
    async attempts(query, target, cursor, signal) {
      return readPage(query, target, cursor, signal);
    },
    async sidebar(selectors, signal) {
      const response = await post<W.SummaryResponse>('/api/v1/usage/session-summaries', { scope: { mode: 'personal' }, items: selectors, inclusion: 'self', pricing_basis: 'historical', include_pending: false }, signal);
      if (response.meta.scope.mode !== 'personal') throw new Error('Unexpected sidebar usage scope');
      const allowed = new Set(selectors.map((s) => `${s.kind}:${s.id}`));
      return Object.fromEntries(response.items.filter((item) => item.inclusion === 'self' && allowed.has(`${item.selector.kind}:${item.selector.id}`))
        .map((item) => [`${item.selector.kind}:${item.selector.id}`, item.available && item.metrics ? mapTotals(item.metrics, item.availability) : null]));
    },
    async facets(query, field, signal) {
      requireSummaryPopulation(query, 'facet');
      const body = wireQuery(query);
      if (query.clientId || query.keyId) throw new Error('Key/client filters are detail-only; clear them for aggregate facet discovery');
      const response = await post<W.FacetResponse>('/api/v1/usage/facets', { scope: body.scope, start: body.start, end: body.end, filters: body.filters, dimension: dimension[field], limit: 100 }, signal);
      checkScope(response.meta, query);
      if (response.dimension !== dimension[field]) throw new Error('Unexpected facet');
      return response.items.filter((item) => item.key !== null).map((item) => ({ value: item.key!, label: item.label ?? item.key! }));
    },
    async requestLog(query, filters, scoped, cursor, signal) {
      const base = detailBody(query, intersectFilters(wireQuery(query).filters, filters), cursor ?? undefined);
      const body = { ...base, filters: { ...base.filters, ...scoped }, pagination_unit: 'request' as const, limit: 50 };
      const page = await post<W.RequestLogPage>('/api/v1/usage/requests', body, signal);
      checkScope(page.meta, query);
      return { items: page.items, nextCursor: page.next_cursor, availability: mapAvailability(page.meta.availability) };
    },
    async requestDetail(query, selectionRef, signal) {
      const base = detailBody(query, wireQuery(query).filters);
      const detail = await post<W.RequestLogDetail>('/api/v1/usage/requests/detail',
        { scope: base.scope, start: base.start, end: base.end, pricing_basis: base.pricing_basis, selection_ref: selectionRef, limit: 20 }, signal);
      checkScope(detail.meta, query);
      return { request: detail.request, attempts: detail.attempts.map((item) => mapAttempt(item, query)) };
    },
    async ranking(query, group, filters, scoped, signal) {
      requireSummaryPopulation(query, 'ranking');
      const body = wireQuery(query);
      // Cost order uses the known priced subtotal; unpriced counts stay on each group.
      const response = await queryCall({ ...body, filters: { ...intersectFilters(body.filters, filters), ...scoped } as W.Filters, view: 'ranking', group_by: group, metric: 'cost', limit: 50 }, signal);
      checkScope(response.meta, query);
      return response.groups.map((g) => ({ ...mapTotals(g.metrics), id: g.key, label: g.label ?? g.key ?? emptyGroupLabel(group),
        performance: mapPerformance(g.label ?? g.key ?? emptyGroupLabel(group), g.metrics) }));
    },
    async providerHistory(query, provider, cursor, signal) {
      if (query.mode !== 'personal') throw new Error('Stored provider history requires My usage and provider ownership; usage-admin and project scopes do not grant this UI access');
      if (!provider || provider !== query.filters.provider) throw new Error('Select this provider explicitly before loading its stored history');
      const body = wireQuery(query);
      const params = new URLSearchParams({ limit: '100', scope: JSON.stringify(body.scope), start: body.start, end: body.end }); if (cursor) params.set('cursor', cursor);
      const response = await send<W.QuotaResponse>(`/api/v1/usage/providers/${encodeURIComponent(provider)}/history?${params}`, { signal });
      if (response.provider_id !== provider) throw new Error('Provider history scope mismatch');
      return { provider: response.provider_id, window: 'Provider-reported units', nextCursor: response.next_cursor, limitations: response.limitations,
        samples: response.samples.map((sample, i, samples) => ({ at: sample.observed_at, usedPercent: null, utilization: sample.utilization, remaining: sample.remaining,
          sampleId: sample.sample_id, limit: sample.limit, maximumUtilization: sample.maximum_utilization, firstObservedAt: sample.first_observed_at,
          account: sample.account_generation, source: sample.source, window: sample.window, sampleCount: sample.sample_count, resetAt: sample.reset_at,
          reset: i > 0 && (sample.account_generation !== samples[i - 1].account_generation || sample.reset_at !== samples[i - 1].reset_at) })) };
    }
  };
}

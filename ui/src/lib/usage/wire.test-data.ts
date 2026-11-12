import type { Attempt, Catalog, Meta, Metrics, QueryResponse, RequestPage, Scope, SessionPage, SessionType } from './wire';
import type { Conversation } from '$lib/types/api';
export function metrics(): Metrics {
  return { finalized_attempts: 12, observed_attempts: 14, pending_complete: null,
    states: { observed_start: null, inflight: null, succeeded: 10, failed: 1, cancelled: 0, unknown: 1 },
    tokens: { input: 2000, output: 300, cache_read: 1000, cache_write: null, reasoning: null },
    token_known_attempts: { input: 12, output: 12, cache_read: 10, cache_write: 0, reasoning: 0 },
    costs: [{ known_amount: '0.123456789012345678', currency: 'USD', basis: 'api_estimate', quality: 'partial', priced_attempts: 10, unpriced_attempts: 2, partially_priced_attempts: 1 }],
    reported_attempts: 10, partial_attempts: 1, unknown_usage_attempts: 1, unpriced_attempts: 2, partially_priced_attempts: 1, unallocatable_cost_attempts: 1,
    performance: { ttft_known_samples: 10, ttft_seconds_sum: 9, latency_known_samples: 10, latency_seconds_sum: 30,
      generation_known_samples: 8, generation_seconds_sum: 10, generation_output_tokens: 250,
      end_to_end_known_samples: 10, end_to_end_seconds_sum: 30, end_to_end_output_tokens: 300,
      dispatched_attempts: 12, failed_dispatched_attempts: 1, logical_completed_requests: null, logical_failed_requests: null,
      ttft_seconds_mean: 0.9, latency_seconds_mean: 3, generation_tokens_per_second: 25, end_to_end_tokens_per_second: 10,
      attempt_failure_rate: 1 / 12, logical_failure_rate: null },
    prompt_cache: { eligible_requests: 10, hit_requests: 5, eligible_input_tokens: 1500, cache_read_tokens: 800 } };
}
export function meta(): Meta {
  return { scope: { mode: 'personal' }, precision: 'day', coverage_start: '2026-10-01T00:00:00Z', observed_through: '2026-10-29T18:00:00Z',
    complete: false, accounting_degraded: false, private_resources_excluded: false, excluded_fields: [], limitations: ['Asynchronous capture does not guarantee completeness.'],
    capture: { state: 'gaps', epoch_started_at: '2026-10-01T00:00:00Z', dropped_observations: 3, gap_incidents: 1 },
    projection: { state: 'lagging', captured_through: '2026-10-29T18:00:00Z', projected_through: '2026-10-29T17:59:00Z', lag_seconds: 60, unprojected_observations: 2 },
    availability: { resolution: 'retained', detail_retention_cutoff: '2026-09-29T00:00:00Z', configured_detail_days: 30, archive_grain: 'day',
      supported_dimensions: ['provider', 'model', 'agent', 'session', 'service', 'operation'], unsupported_fields: [] } };
}
export function response(): QueryResponse & { total: Metrics } {
  const total = metrics();
  return { meta: meta(), total, cost_breakdowns: { measure: 'known_priced_subtotal', complete_groups: true,
    dimensions: (['subject', 'agent', 'provider', 'model'] as const).map((dimension) => ({ dimension, groups: [{ key: `authorized-${dimension}`, label: `Authorized ${dimension}`, metrics: total }] })) },
    groups: [{ key: 'visible-resource', label: 'Visible resource', metrics: metrics() }],
    buckets: [{ start: '2026-10-28T00:00:00Z', end: '2026-10-29T00:00:00Z', complete: false, metrics: metrics(), source_grain: 'day', source: 'retained', groups: [] }],
    forecast: { sample_start: null, sample_end: null, horizon_seconds: 604800,
      tokens: { input: { available: false, projected_tokens: null, reason: 'capture_gap' } }, cost: { available: false, projected_cost: null, reason: 'unpriced' }, limitations: ['Capture gaps; no fabricated projection.'] } };
}
/** Synthetic public-contract fixtures, not captured or shipping API responses. */
export const sessionRef = 'v2.usage-contract-session-fixture';
export const requestRef = 'v2.usage-contract-request-fixture';
export function catalog(scope: Scope = { mode: 'personal' }): Catalog {
  const filters = ['subject_emails', 'subject_keys', 'provider_owner_emails', 'provider_ids', 'model_ids', 'agent_ids', 'profile_ids', 'efforts', 'purposes', 'hook_ids', 'service_ids', 'operations', 'project_ids', 'conversation_ids', 'session_ids', 'task_ids', 'step_run_ids', 'turn_ids', 'cycle_ids', 'key_ids', 'client_ids', 'logical_request_ids', 'attempt_ids', 'error_categories', 'states'];
  return { scope, grouping_dimensions: ['subject', 'agent', 'profile', 'provider', 'model', 'effort', 'purpose', 'project', 'hook', 'service', 'operation'],
    summary_filters: filters.filter((field) => !['cycle_ids', 'key_ids', 'client_ids', 'logical_request_ids', 'attempt_ids', 'error_categories', 'states'].includes(field)),
    detail_filters: filters, summary_population_available: true, unavailable_metrics: {},
    session_types: { cognis: 'Verified Cognis metadata sessions', proxy: 'Authenticated proxy labels; retained-detail totals only' },
    selections: { token_format: 'aead_v2', expires_after_seconds: 3600, session_kinds: ['cognis', 'proxy'],
      proxy_totals: 'retained_detail', proxy_search: 'exact_label_private_scope', max_proxy_page: 100,
      detail_query: 'UsageAttemptQuery.selection_ref', team_identifiers: 'encrypted_refs_only' } };
}
export function sessionPage(kind: SessionType = 'cognis', scope: Scope = { mode: 'personal' }): SessionPage {
  return { meta: { ...meta(), scope }, next_cursor: 'usage-fixture-session-next', items: [{
    session_type: kind, session_id: kind === 'cognis' ? 'usage-fixture-cognis-session' : null,
    conversation_id: kind === 'cognis' ? 'usage-fixture-conversation' : null,
    client_id: scope.mode === 'project' ? null : 'usage-fixture-client',
    client_session_id: kind === 'proxy' && scope.mode !== 'project' ? 'usage_fixture_unverified_label' : null,
    selection_ref: sessionRef, totals_scope: kind === 'proxy' ? 'retained_detail' : 'cumulative',
    retained_start: '2026-10-01T00:00:00Z', retained_end: '2026-10-30T00:00:00Z',
    metrics: metrics(), drill_filters: kind === 'cognis' ? { session_ids: ['usage-fixture-cognis-session'] } : {},
  }] };
}
export function requestPage(scope: Scope = { mode: 'personal' }): RequestPage {
  return { meta: { ...meta(), scope }, next_cursor: 'usage-fixture-request-next', pagination_unit: 'attempt', items: [{
    request_id: scope.mode === 'project' ? null : 'usage-fixture-logical-1',
    attempt_id: scope.mode === 'project' ? null : 'usage-fixture-attempt-1',
    cycle_id: scope.mode === 'project' ? null : 'usage-fixture-cycle-1', session_type: 'proxy',
    selection_ref: requestRef, state: 'failed', logical_outcome: null,
    logical_request_started_at: null, started_at: '2026-10-29T17:59:59Z', timing: null,
    drill_filters: scope.mode === 'project' ? {} : { logical_request_ids: ['usage-fixture-logical-1'] },
  }] };
}
export function attempt(): Attempt {
  return { attempt_id: 'usage-fixture-attempt-1', started_at: '2026-10-29T17:59:59Z', finished_at: '2026-10-29T18:00:03Z', state: 'failed', subject_email: 'usage-fixture@test.invalid', session_id: null, cycle_id: 'usage-fixture-cycle-1', logical_request_id: 'usage-fixture-logical-1',
    provider_id: 'usage-fixture-provider', requested_model: 'requested-model', resolved_model: 'effective-model', reported_model: null, effort: 'high',
    service_id: 'Mnemory', operation: 'memory.extract', dispatched_at: '2026-10-29T18:00:00Z',
    ttft_seconds: null, latency_seconds: 3, generation_seconds: null, generation_tokens_per_second: null, end_to_end_tokens_per_second: null,
    retry: true, fallback: false, status_code: 429, exception_class: 'RateLimitError', provider_error_code: 'rate_limit', error_category: 'rate_limit', error_summary: 'Provider rate limit exceeded.',
    tokens: { input: null, output: null, cache_read: null, cache_write: null, reasoning: null }, usage_quality: 'unknown', costs: [],
    parameters: { requested: { temperature: { value: 0.7, provenance: 'explicit' }, response_format: { value: 'json_schema', provenance: 'explicit' } },
      effective: { temperature: { value: null, provenance: 'unknown' } } },
    client_id: 'private-client', key_id: 'private-key', provider_request_id: 'private-request',
    logical_timing: { server: { source: 'controller_monotonic', elapsed_seconds: 4, first_send_seconds: 1, last_send_seconds: 3.5,
      partition_complete: true, cognis_overhead_seconds: 1, spans: [{ kind: 'routing', start_seconds: 0, end_seconds: 0.2 }, { kind: 'provider_wait', start_seconds: 0.2, end_seconds: 3.2 }, { kind: 'delivery', start_seconds: 3.2, end_seconds: 4 }] }, client: null } };
}
export function conversation(id: string): Conversation {
  return { conversation_id: id, user_email: 'usage-fixture@test.invalid', agent_id: 'usage-fixture-agent', project_id: 'usage-fixture-project',
    title: `Usage fixture ${id}`, title_source: 'user', context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} }, active_session_id: null, active_executor_id: null,
    active_executor_assigned_at: null, active_executor_expires_at: null, active_executor_source: null, active_session_status: null,
    active_session_completion_reason: null, active_turn_chat_mode: null, active_turn_chat_mode_source: null, pending_notification_types: [],
    starred_at: null, status: 'active', last_message_at: null, last_read_at: null, has_unread: false, has_active_turn: false, created_at: null, updated_at: null };
}

/**
 * Public DTOs synchronized with cognis.core.usage.query_models.
 * Export exact schemas using scripts/export_usage_query_contract.py.
 */
export type Scope = { mode: 'personal' | 'project' | 'admin'; project_id?: string | null };
export type Dimension = 'subject' | 'agent' | 'profile' | 'provider' | 'model' | 'effort' | 'purpose' | 'hook' | 'service' | 'operation' | 'project' | 'conversation' | 'session'
  | 'traffic_origin' | 'auth_type' | 'client' | 'key' | 'task' | 'schedule';
export type TrafficOrigin = 'cognis' | 'api' | 'unknown';
export type AuthType = 'api_key' | 'user_jwt' | 'service' | 'browser_session' | 'unknown';
export interface Availability {
  resolution: 'retained' | 'aggregate_only' | 'detail_expired' | 'mixed' | 'unavailable';
  detail_retention_cutoff: string | null; configured_detail_days: number | null; archive_grain: 'day' | null;
  supported_dimensions: Dimension[]; unsupported_fields: string[];
}
export interface Meta {
  scope: Scope; precision: 'hour' | 'day' | 'exact' | 'lifetime';
  coverage_start: string | null; observed_through: string | null; complete: boolean;
  accounting_degraded: boolean; private_resources_excluded: boolean; excluded_fields: string[]; limitations: string[];
  capture?: { state: 'observed' | 'gaps' | 'unknown'; epoch_started_at: string | null; dropped_observations: number | null; gap_incidents: number | null };
  projection?: { state: 'ready' | 'lagging' | 'building' | 'unavailable'; captured_through: string | null; projected_through: string | null; lag_seconds: number | null; unprojected_observations: number | null };
  availability?: Availability;
}
export interface Tokens { input: number | null; output: number | null; cache_read: number | null; cache_write: number | null; reasoning: number | null }
export interface Cost {
  known_amount: string | null; currency: string; basis: 'api_estimate' | 'configured_equivalent' | 'provider_reported';
  quality: 'complete' | 'partial' | 'unknown'; priced_attempts: number; unpriced_attempts: number;
  partially_priced_attempts?: number;
}
export interface Duration { known_samples: number; seconds_sum: number | null }
export interface Performance {
  ttft_known_samples: number; ttft_seconds_sum: number | null;
  latency_known_samples: number; latency_seconds_sum: number | null;
  generation_known_samples: number; generation_seconds_sum: number | null; generation_output_tokens: number | null;
  end_to_end_known_samples: number; end_to_end_seconds_sum: number | null; end_to_end_output_tokens: number | null;
  dispatched_attempts: number | null; failed_dispatched_attempts: number | null;
  ttft_seconds_mean: number | null; latency_seconds_mean: number | null;
  generation_tokens_per_second: number | null; end_to_end_tokens_per_second: number | null;
  attempt_failure_rate: number | null; logical_failure_rate: number | null;
  logical_completed_requests: number | null; logical_failed_requests: number | null;
}
export type SpanKind = 'routing' | 'queue' | 'provider_wait' | 'executor_wait' | 'accounting_hook' | 'finalization' | 'delivery' | 'other_cognis';
export interface LogicalPerformance {
  server_boundary: Duration; server_first_send: Duration; server_last_send: Duration; cognis_overhead: Duration;
  routing: Duration; queue: Duration; provider_wait: Duration; executor_wait: Duration;
  accounting_hook: Duration; finalization: Duration; delivery: Duration;
  cognis_ui: { first_chunk: Duration; last_chunk: Duration };
  authenticated_client: { first_chunk: Duration; last_chunk: Duration };
}
export interface Metrics {
  finalized_attempts: number; observed_attempts?: number | null; pending_complete: boolean | null;
  states: { observed_start?: number | null; inflight: number | null; succeeded: number; failed: number; cancelled: number; unknown: number };
  tokens: Tokens; token_known_attempts: { [K in keyof Tokens]: number }; costs: Cost[];
  reported_attempts: number; partial_attempts: number; unknown_usage_attempts: number; unpriced_attempts: number;
  partially_priced_attempts?: number | null; unallocatable_cost_attempts?: number | null;
  performance: Performance; logical_performance?: LogicalPerformance;
  prompt_cache: { eligible_requests: number | null; hit_requests: number | null; eligible_input_tokens: number | null; cache_read_tokens: number | null };
}
export interface Group { key: string | null; label: string | null; metrics: Metrics }
export interface Forecast {
  sample_start: string | null; sample_end: string | null; horizon_seconds: number;
  tokens: Partial<Record<keyof Tokens, { available: boolean; projected_tokens: number | null; reason: string | null }>>;
  cost: { available: boolean; projected_cost: Cost | null; reason: string | null }; limitations: string[];
}
export interface QueryResponse {
  meta: Meta; total: Metrics | null; groups: Group[];
  ranking_measure?: 'known_priced_subtotal' | 'attempts' | 'input' | 'output' | null;
  buckets: { start: string; end: string; complete: boolean; metrics: Metrics; groups?: Group[]; source_grain?: 'hour' | 'day' | null; source?: string }[];
  forecast: Forecast | null;
  cost_breakdowns?: CostBreakdowns | null;
}
export interface CostBreakdown {
  dimension: 'subject' | 'agent' | 'provider' | 'model'; groups: Group[];
}
export interface CostBreakdowns {
  measure: 'known_priced_subtotal'; complete_groups: true; dimensions: CostBreakdown[];
}
export type Filters = Partial<Record<'subject_emails' | 'subject_keys' | 'provider_owner_emails' | 'provider_ids' | 'model_ids' | 'agent_ids' | 'profile_ids' | 'efforts' | 'purposes' | 'project_ids' | 'service_ids' | 'operations' | 'conversation_ids' | 'session_ids' | 'task_ids' | 'step_run_ids' | 'turn_ids' | 'cycle_ids' | 'hook_ids' | 'key_ids' | 'client_ids' | 'states' | 'error_categories' | 'logical_request_ids' | 'attempt_ids' | 'schedule_ids' | 'traffic_origins' | 'auth_types', string[] | null>>;
/** Scalar/numeric filters sent beside the list filters; never intersected as ID lists. */
export interface ScopedFilters { task_origin?: 'scheduled' | 'one_off' | null; agent_scope?: 'system' | null; http_status_codes?: number[] | null }
export interface Query {
  scope: Scope; start: string; end: string; filters: Filters; view: 'overview' | 'ranking' | 'trend' | 'companions' | 'cost_breakdowns';
  group_by?: Dimension; bucket: 'day' | 'hour'; limit: number; pricing_basis: 'historical'; include_pending: boolean;
  forecast?: { horizon_seconds: number };
  metric?: 'attempts' | 'cost' | 'input' | 'output';
}
export type ParameterName = 'temperature' | 'top_p' | 'top_k' | 'max_tokens' | 'max_output_tokens' | 'max_completion_tokens' | 'seed' | 'frequency_penalty' | 'presence_penalty' | 'reasoning_effort' | 'reasoning_budget_tokens' | 'service_tier' | 'response_format' | 'tool_choice';
export type Parameter = { value: number | string | null; provenance: 'explicit' | 'configured_default' | 'provider_default' | 'adapter_normalized' | 'unsupported' | 'unknown' };
export interface Attempt {
  media_usage?: MediaQuantity[];
  inference_operation?: string | null;
  attempt_id: string | null; state: 'observed_start' | 'inflight' | 'succeeded' | 'failed' | 'cancelled' | 'unknown'; subject_email: string | null;
  selection_ref?: string | null;
  started_at?: string; finished_at?: string | null;
  session_id: string | null; cycle_id: string | null; logical_request_id?: string | null; provider_id: string | null;
  requested_model: string | null; resolved_model: string | null; reported_model: string | null; effort: string | null;
  service_id: string | null; operation: string | null; dispatched_at: string | null;
  ttft_seconds: number | null; latency_seconds: number | null; generation_seconds: number | null;
  generation_tokens_per_second: number | null; end_to_end_tokens_per_second: number | null;
  provider_timing_source?: string; retry: boolean; fallback: boolean; status_code: number | null;
  exception_class: string | null; provider_error_code: string | null; error_category: string | null; error_summary: string | null;
  tokens: Tokens; usage_quality: 'complete' | 'partial' | 'unknown'; costs: Cost[];
  parameters?: { requested: Partial<Record<ParameterName, Parameter>>; effective: Partial<Record<ParameterName, Parameter>>; service_tier_applicability?: import('$lib/types/api').UsageServiceTierApplicability | null };
  logical_timing?: {
    server: { source: string; elapsed_seconds: number | null; first_send_seconds: number | null; last_send_seconds: number | null; partition_complete: boolean; cognis_overhead_seconds: number | null; spans: { kind: SpanKind; start_seconds: number; end_seconds: number }[] } | null;
    client: { source: 'cognis_ui' | 'authenticated_client_report'; clock: 'client_monotonic'; correlation_verified: true; first_chunk_seconds: number | null; last_chunk_seconds: number | null } | null;
  } | null;
  client_id?: string | null; key_id?: string | null; provider_request_id?: string | null;
}
export interface AttemptPage { meta: Meta; items: Attempt[]; next_cursor: string | null }
export type SessionType = 'cognis' | 'proxy';
export interface Session {
  session_id: string | null; conversation_id: string | null;
  /** Authorized chat name for the session, when known. */
  session_label?: string | null;
  session_type: SessionType; client_id: string | null; client_session_id: string | null; selection_ref: string | null;
  retained_start: string | null; retained_end: string | null;
  totals_scope: 'cumulative' | 'retained_detail'; metrics: Metrics; drill_filters: Filters;
}
export interface Request {
  request_id: string | null; cycle_id: string | null;
  session_type: SessionType | null; selection_ref: string | null; session_label?: string | null;
  logical_request_started_at: string | null; started_at: string;
  attempt_id: string | null; state: Attempt['state']; logical_outcome: Attempt['state'] | null;
  timing: Attempt['logical_timing'];
  drill_filters: Filters;
}
export interface SessionPage { items: Session[]; next_cursor: string | null; meta: Meta }
export interface RequestPage { items: Request[]; next_cursor: string | null; pagination_unit: 'attempt'; meta: Meta }
/** One logical request (retries folded in), `pagination_unit: 'request'`. */
export interface RequestLog extends Request {
  operations?: string[]; media_usage?: MediaQuantity[];
  traffic_origin: TrafficOrigin; auth_type: AuthType | null; completed_at: string | null;
  requested_model: string | null; provider_ids: string[]; resolved_models: string[];
  request_path: string | null; streaming: boolean | null; http_status_code: number | null; finish_reason: string | null;
  service_id: string | null; error_category: string | null; error_summary: string | null;
  latency_seconds: number | null; ttft_seconds: number | null; tokens: Tokens;
  usage_quality: 'complete' | 'partial' | 'unknown'; costs: Cost[]; unpriced_attempts: number;
  attempt_count: number; retry_count: number; fallback_count: number;
  /** Private detail only (personal/admin scope). */
  client_id?: string | null; key_id?: string | null; user_agent?: string | null; app_name?: string | null; app_version?: string | null;
  client_labels?: Record<string, string | null> | null;
}
export interface RequestLogPage { items: RequestLog[]; next_cursor: string | null; pagination_unit: 'request'; meta: Meta }
export interface MediaQuantity {
  name: 'input_audio_tokens' | 'output_audio_tokens' | 'input_image_tokens' | 'output_image_tokens'
    | 'input_text_tokens' | 'output_text_tokens' | 'cache_read_image_tokens' | 'cache_read_audio_tokens'
    | 'input_audio_duration_ms' | 'output_audio_duration_ms' | 'input_video_duration_ms' | 'output_video_duration_ms'
    | 'input_image_count' | 'output_image_count' | 'input_characters' | 'output_characters' | 'billed_characters';
  value: number; unit: 'tokens' | 'milliseconds' | 'images' | 'characters' | null;
  source: 'provider' | 'native_sdk' | 'adapter' | null; status: 'reported' | 'derived' | 'partial';
}
export interface RequestLogDetail { meta: Meta; request: RequestLog | null; attempts: Attempt[]; next_cursor: string | null }
export interface DetailBody {
  scope: Scope; start: string; end: string; filters: Filters; pricing_basis: 'historical' | 'current';
  limit: number; cursor?: string; selection_ref?: string;
}
export interface SessionBody extends DetailBody { session_type: SessionType; search?: string }
export interface Catalog {
  scope: Scope; grouping_dimensions: string[]; summary_filters: string[]; detail_filters: string[];
  summary_population_available: boolean; unavailable_metrics: Record<string, string>;
  session_types: Record<string, string>;
  selections: {
    token_format: 'aead_v2'; expires_after_seconds: 3600; session_kinds: SessionType[];
    proxy_totals: 'retained_detail'; proxy_search: 'exact_label_private_scope';
    max_proxy_page: 100; detail_query: 'UsageAttemptQuery.selection_ref'; team_identifiers: 'encrypted_refs_only';
  };
}
export interface FacetResponse { meta: Meta; dimension: Dimension; items: Group[] }
export interface Selector { kind: 'conversation' | 'activity' | 'session' | 'execution'; id: string }
export type SummaryInclusion = 'self' | 'direct' | 'inclusive';
export interface SummaryRequest {
  scope?: Scope; items: Selector[]; inclusion?: SummaryInclusion;
  pricing_basis?: 'historical' | 'current'; include_pending?: boolean;
}
export interface SummaryResponse { meta: Meta; items: { selector: Selector; available: boolean; inclusion: SummaryInclusion; physical_sessions?: number | null; metrics: Metrics | null; availability?: Availability }[] }
export interface QuotaResponse {
  provider_id: string; next_cursor: string | null; limitations: string[];
  samples: { sample_id: string; observed_at: string; account_generation: string; reset_at: string | null; window: string; limit: string | null; utilization: string | null; maximum_utilization: string | null; first_observed_at: string | null; remaining: string | null; source: string; sample_count: number }[];
}

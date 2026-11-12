import { formatDecimalMoney } from './decimal';
import type { Catalog, Filters as DetailFilters, SessionType } from './wire';
/** UI view models, not wire contracts. The API adapter owns transport mapping. */
export type UsageMode = 'personal' | 'admin' | 'project';
export type UsageDimension = 'provider' | 'model' | 'effort' | 'agent' | 'user' | 'purpose' | 'project';
export type UsageGrouping = UsageDimension | 'hook' | 'service' | 'operation' | 'client' | 'key' | 'auth_type' | 'traffic_origin';
export type UsageSurface = 'overview' | 'costs' | 'performance' | 'forecast' | 'work' | 'limits' | 'companions' | 'explorer';
export type UsageFilters = Record<UsageDimension, string> & { start: string; end: string };
export interface UsageOption { value: string; label: string }
export interface UsageQuery {
  mode: UsageMode;
  filters: UsageFilters;
  groupBy: UsageGrouping;
  projectScope?: { id: string; label: string };
  surface?: UsageSurface;
  includePending?: boolean;
  serviceId?: string;
  operation?: string;
  explorerMode?: 'sessions' | 'requests' | 'attempts';
  sessionType?: SessionType;
  sessionSearch?: string;
  errorCategories?: string[];
  logicalRequestIds?: string[];
  /** Opaque references live in request bodies/history state, never URL text. */
  selectionRef?: string;
  selectionType?: SessionType | null;
  requiresSelection?: boolean;
  requiresCursor?: boolean;
  requiresSessionSearch?: boolean;
  detailFilters?: DetailFilters;
  returnState?: UsageReturnState;
  sessionId?: string;
  cycleId?: string;
  state?: string;
  clientId?: string;
  keyId?: string;
  cursor?: string;
  selectedAttempt?: string;
  selectedQuota?: string;
  hookId?: string;
  metric?: string;
  contextLabel?: string;
  returnTo?: string;
  preciseStart?: string;
  preciseEnd?: string;
}
export interface UsageReturnState {
  selectionRef?: string; selectionType?: SessionType | null; detailFilters?: DetailFilters; cursor?: string; sessionSearch?: string;
  contextLabel?: string; returnTo?: string; previous?: UsageReturnState;
}
export type UsageDrillPatch = Omit<Partial<UsageQuery>, 'filters'> & { filters?: Partial<UsageFilters> };
export interface UsageCost {
  amount: string | null;
  currency: string | null;
  basis: 'api_estimate' | 'api_equivalent' | 'provider_reported' | 'unknown';
}
export type UsageAttributionDimension = 'user' | 'agent' | 'provider' | 'model';
export interface UsageCostDrill {
  filters?: Partial<Record<UsageDimension, string>>;
  detailFilters?: DetailFilters;
  selectionRef?: string;
  selectionType?: SessionType | null;
  requiresSelection?: boolean;
}
/** Internal presentation contract. Only the adapter may supply authorized drills. */
export interface UsageCostCohort {
  key: string;
  /** Group value (provider, model, agent or user); null for the empty group. */
  id?: string | null;
  label: string;
  amount: string | null;
  /** Null for redacted, unallocated or otherwise non-representable populations. */
  drill: UsageCostDrill | null;
}
export type UsageCostMarginal =
  | { state: 'ready'; complete: boolean; groups: UsageCostCohort[] }
  | { state: 'unavailable'; reason: string };
export interface UsageCostBand {
  currency: string;
  basis: Exclude<UsageCost['basis'], 'unknown'>;
  amount: string | null;
  /** Counts may overlap; they are not monetary sectors or additive populations. */
  coverage: { priced: number | null; partial: number | null; unpriced: number | null; unallocatable: number | null };
  dimensions: Record<UsageAttributionDimension, UsageCostMarginal>;
}
export interface UsageTotals {
  input: number | null;
  output: number | null;
  cacheRead: number | null;
  cacheWrite: number | null;
  /** Provider-normalized eligible input, not input + output or all attempts. */
  cacheEligibleInput: number | null;
  cacheEligibleRequests?: number | null;
  cacheHitRequests?: number | null;
  reasoning: number | null;
  attempts: number;
  unknown: number;
  partial: number;
  unpriced: number | null;
  partiallyPriced?: number | null;
  unallocatable?: number | null;
  cacheRatioRead?: number | null;
  cost: UsageCost;
  /** Distinct currencies/bases are never collapsed into the representative cost. */
  costs?: UsageCost[];
  availability?: UsageAvailability;
}
export interface UsageBucket {
  start: string;
  end?: string;
  complete: boolean;
  tokens: number | null;
  cost: UsageCost;
  costs?: UsageCost[];
  totals?: UsageTotals;
  performance?: UsagePerformance;
  source?: 'retained' | 'archive' | 'mixed';
  /** Certified complete pricing coverage, not merely a known priced subtotal. */
  costComplete?: boolean;
  groups?: { id: string | null; label: string; totals: UsageTotals; performance: UsagePerformance; costComplete: boolean }[];
}
export interface UsageGroup extends UsageTotals {
  id: string | null;
  label: string;
  performance?: UsagePerformance;
}
export interface UsageAttempt {
  id: string | null;
  cycle: string;
  model: string;
  effort: string | null;
  outcome: string;
  input: number | null;
  output: number | null;
  cacheRead: number | null;
  cost: UsageCost;
  costs?: UsageCost[];
  startedAt?: string;
  finishedAt?: string | null;
  source?: string;
  serviceId?: string | null;
  operation?: string | null;
  providerId?: string | null;
  sessionId?: string | null;
  cycleId?: string | null;
  logicalId?: string | null;
  status?: string;
  cacheWrite?: number | null;
  detail?: UsageAttemptDetail;
  sessionType?: SessionType;
  selectionRef?: string | null;
}
export interface UsageSession extends UsageTotals {
  id: string | null;
  label: string;
  agent: string;
  purpose: string;
  /** Only supplied when ordinary content authorization separately permits it. */
  transcriptHref: string | null;
  sessionType?: SessionType;
  sessionLabel?: string | null;
  selectionRef?: string | null;
  totalsScope?: 'cumulative' | 'retained_detail';
  drillFilters?: DetailFilters;
  retainedStart?: string | null;
  retainedEnd?: string | null;
}
export interface UsageRequest {
  requestId: string | null; attemptId: string | null;
  sessionType: SessionType | null; selectionRef: string | null;
  logicalState: string | null; logicalStartedAt: string | null; startedAt: string; attemptState: string; drillFilters: DetailFilters;
}
export interface QuotaHistory {
  provider: string;
  window: string;
  samples: { at: string; usedPercent: number | null; reset: boolean; sampleId?: string; limit?: string | null; maximumUtilization?: string | null; firstObservedAt?: string | null; utilization?: string | null; remaining?: string | null; account?: string; window?: string; source?: string; sampleCount?: number; resetAt?: string | null }[];
  nextCursor?: string | null;
  limitations?: string[];
}
export interface UsageView {
  scopeLabel: string;
  scopeDescription: string;
  freshness: string;
  coverageStart: string;
  pricing: 'historical' | 'current';
  status: 'ready' | 'partial' | 'repricing' | 'degraded';
  totals: UsageTotals | null;
  facets: Record<UsageDimension, { value: string; label: string }[]>;
  buckets: UsageBucket[];
  groups: UsageGroup[];
  sessions: UsageSession[];
  /** Already permission-scoped; null is not an empty provider account. */
  quota: QuotaHistory | null;
  lifecycle?: UsageLifecycle;
  performance?: UsagePerformance[];
  companions?: CompanionUsage[];
  /** Model breakdown of the selected companion service. */
  serviceModels?: UsageGroup[];
  forecast?: UsageServerForecast | null;
  /** Fixed marginal breakdowns from one authorized backend operation/generation. */
  costBreakdowns?: UsageCostBand[];
  overviewTrendNote?: string;
  availability?: UsageAvailability;
  coverage?: {
    captureState: string; dropped: number | null; gaps: number | null; epoch: string | null;
    projectionState: string; capturedThrough: string | null; projectedThrough: string | null;
    projectionLagSeconds: number | null; outstanding: number | null; limitations: string[];
  };
  sectionErrors?: string[];
  requestPage?: { rows: UsageAttempt[]; nextCursor: string | null; availability?: UsageAvailability };
  sessionPage?: { nextCursor: string | null; availability?: UsageAvailability };
  logicalRequestPage?: { rows: UsageRequest[]; nextCursor: string | null; availability?: UsageAvailability; paginationUnit: 'attempt' };
  catalog?: Catalog;
  groupBy?: UsageQuery['groupBy'];
}
export type UsageLoadState =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'ready'; view: UsageView };

export function blankFilters(): UsageFilters {
  return { start: '', end: '', provider: '', model: '', effort: '', agent: '', user: '', purpose: '', project: '' };
}
export function count(value: number | null): string {
  return value === null ? 'Unknown' : new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 2 }).format(value);
}
export function money(cost: UsageCost): string {
  return cost.amount === null ? 'Estimate unavailable' : cost.currency === null ? `${cost.amount} · currency unknown` : formatDecimalMoney(cost.amount, cost.currency);
}
export function costBasis(cost: UsageCost): string {
  return cost.basis === 'provider_reported' ? 'Provider-reported cost' : cost.basis === 'api_equivalent' ? 'API-equivalent estimate' : cost.basis === 'api_estimate' ? 'Configured cost estimate' : 'Estimate unavailable';
}
export interface UsageForecast {
  tokens: number;
  cost: UsageCost;
  sampleStart: string;
  sampleEnd: string;
  samples: number;
  horizonDays: number;
}
export interface UsageLifecycle {
  observedAttempts: number | null;
  observedStart: number | null;
  inflight: number | null;
  dispatchedKnown: number | null;
  completed: number;
  failed: number;
  cancelled: number;
  unknown: number;
}
export interface TimingMeasure { totalMs: number | null; samples: number }
export interface ThroughputMeasure { outputTokens: number | null; durationMs: number | null; samples: number; rate?: number | null }
export interface UsagePerformance {
  provider: string;
  serverLatency: TimingMeasure;
  serverTtft: TimingMeasure;
  providerSpan: TimingMeasure;
  executorSpan: TimingMeasure;
  clientLatency: TimingMeasure;
  clientTtft: TimingMeasure;
  generation: ThroughputMeasure;
  endToEnd: ThroughputMeasure;
  failedAttempts: number | null;
  dispatchedAttempts: number | null;
  logicalFailed: number | null;
  logicalCompleted: number | null;
  providerTtft?: TimingMeasure;
  cognisOverhead?: TimingMeasure;
  spans?: { kind: string; timing: TimingMeasure }[];
  clientSources?: { source: string; first: TimingMeasure; last: TimingMeasure }[];
  timingSource?: string;
}
export interface CompanionUsage {
  service: string;
  operation: string;
  pending?: number | null;
  sessionless: number | null;
  finalized: number;
  input: number | null;
  output: number | null;
  cost: UsageCost;
  costs?: UsageCost[];
}
export type ParameterName = 'temperature' | 'top_p' | 'top_k' | 'max_tokens' | 'max_completion_tokens' | 'max_output_tokens' | 'seed' | 'frequency_penalty' | 'presence_penalty' | 'reasoning_effort' | 'reasoning_budget_tokens' | 'service_tier' | 'response_format' | 'tool_choice';
export type ParameterProvenance = 'explicit' | 'configured default' | 'provider default' | 'adapter normalized' | 'unsupported' | 'unknown';
export interface ModelParameter {
  name: ParameterName;
  requested: number | string | null;
  effective: number | string | null;
  provenance: ParameterProvenance;
  requestedProvenance?: ParameterProvenance;
}
export interface UsageAttemptDetail {
  mediaUsage?: import('./wire').MediaQuantity[];
  inferenceOperation?: string | null;
  lifecycle: 'observed_start' | 'inflight' | 'completed' | 'failed' | 'cancelled' | 'unknown';
  dispatched: boolean | null;
  service: string | null;
  operation: string | null;
  sessionId: string | null;
  requestedModel: string | null;
  provider: string;
  timings: UsagePerformance;
  parameters: ModelParameter[];
  error: { category: string; code: string | null; summary: string } | null;
  /** Only populated by own-personal/admin adapters, never project-team DTOs. */
  identifiers: { client: string | null; key: string | null; request: string | null } | null;
  retry?: boolean;
  fallback?: boolean;
  exceptionClass?: string | null;
  serverPartition?: { source: string; complete: boolean; elapsed: number | null; overhead: number | null; firstSend: number | null; lastSend: number | null; spans: { kind: string; start: number; end: number }[] } | null;
}
export interface UsageAvailability {
  resolution: 'retained' | 'aggregate_only' | 'detail_expired' | 'mixed' | 'unavailable';
  cutoff: string | null;
  configuredDays: number | null;
  grain: string | null;
  supportedDimensions: string[];
  unsupportedFields: string[];
}
export interface UsageServerForecast {
  sampleStart: string | null; sampleEnd: string | null; horizonSeconds: number;
  tokens: { field: string; value: number | null; reason: string | null }[];
  cost: UsageCost | null; costReason: string | null; limitations: string[];
}
export function averageMs(value: TimingMeasure): string {
  return value.totalMs === null || value.samples <= 0 ? 'Unknown' : `${(value.totalMs / value.samples).toLocaleString('en', { maximumFractionDigits: 0 })} ms`;
}
export function throughput(value: ThroughputMeasure): string {
  if (value.rate !== undefined) return value.rate === null ? 'Unknown' : `${value.rate.toFixed(1)} tok/s`;
  return value.outputTokens === null || value.durationMs === null || value.durationMs <= 0 || value.samples <= 0
    ? 'Unknown' : `${(value.outputTokens / value.durationMs * 1000).toFixed(1)} tok/s`;
}
export function attemptErrorRate(value: UsagePerformance): string {
  return value.dispatchedAttempts === null || value.failedAttempts === null || value.dispatchedAttempts <= 0 ? 'Unknown' : `${(value.failedAttempts / value.dispatchedAttempts * 100).toFixed(1)}%`;
}

const EMPTY_GROUP: Record<string, string> = {
  subject: 'Unknown user', user: 'Unknown user', agent: 'Cognis system', provider: 'Unknown provider', model: 'Unknown model',
  effort: 'Default effort', purpose: 'Unspecified', project: 'No project', service: 'Cognis', operation: 'Other operations',
  conversation: 'Outside conversations', task: 'Not a task', schedule: 'Not scheduled', session: 'No session',
  client: 'Cognis', key: 'No API key', auth_type: 'Internal', traffic_origin: 'Unknown origin', hook: 'No hook'
};
/** Readable name for the group of requests without a value in this dimension. */
export function emptyGroupLabel(dimension: string | undefined): string {
  return (dimension && EMPTY_GROUP[dimension]) || 'Other';
}

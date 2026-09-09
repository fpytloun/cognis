import type { ActivityOverviewResponse, ChatSnapshot, TimelineScope, WorkMutationEvent } from '$lib/chat-v2/types';
import { chatV2Api, type ClientPerformanceMetric } from '$lib/chat-v2/api';

const MAX_ENTRIES = 16;
const MAX_FOCUSED_PER_CONVERSATION = 8;
const FRESH_MS = 30_000;
const STALE_MS = 120_000;
const HARD_EVICTION_MS = 300_000;
const MAX_COMMAND_ROWS = 10;
const MAX_COMMAND_TEXT = 4_096;
const MAX_FILE_ROWS = 20;
const MAX_CONCURRENT_REQUESTS = 4;
const INITIAL_RETRY_DELAYS_MS = [250, 500, 1_000] as const;
const INITIAL_RETRY_DEADLINE_MS = 6_000;

type CacheEntry = {
  value: ActivityOverviewResponse;
  storedAt: number;
  invalidated: boolean;
};

type RerunConsumers = {
  controller: AbortController;
  hasUnscopedConsumer: boolean;
  subscribers: Set<AbortSignal>;
};

export type ActivityOverviewCacheHit = {
  value: ActivityOverviewResponse;
  state: 'fresh' | 'stale';
};

const entries = new Map<string, CacheEntry>();
type ScopedRequest = {
  admitted?: ActivityOverviewResponse;
  cacheEpoch: number;
  controller: AbortController;
  epoch: number;
  exactCallbacks: Set<(value: ActivityOverviewResponse) => void>;
  exactLoader?: ActivityOverviewLoader;
  exactRequired: boolean;
  exactStarted: boolean;
  hasUnscopedConsumer: boolean;
  loader: ActivityOverviewLoader;
  rerunPromise?: Promise<ActivityOverviewResponse>;
  rerunConsumers?: RerunConsumers;
  scope: TimelineScope;
  subscribers: Set<AbortSignal>;
  promise: Promise<ActivityOverviewResponse>;
};
export type ActivityOverviewLoader = (signal: AbortSignal) => Promise<ActivityOverviewResponse>;
const requests = new Map<string, ScopedRequest>();
type ScheduledRequest = {
  controller: AbortController;
  deadlineAt: number;
  exactCallbacks: Set<(value: ActivityOverviewResponse) => void>;
  exactLoader?: ActivityOverviewLoader;
  hasUnscopedConsumer: boolean;
  loader: ActivityOverviewLoader;
  promise: Promise<ActivityOverviewResponse>;
  reject: (error: unknown) => void;
  resolve: (value: ActivityOverviewResponse) => void;
  scope: TimelineScope;
  subscribers: Set<AbortSignal>;
  timer: ReturnType<typeof setTimeout>;
};
const scheduledRequests = new Map<string, ScheduledRequest>();
const REVALIDATE_DELAY_MS = 500;
const REVALIDATE_MAX_DELAY_MS = 1_500;
const scopeEpochs = new Map<string, number>();
const requestQueue: Array<() => void> = [];
const signaledRevisions = new Map<string, number>();
const initialRetryWakers = new Map<string, () => void>();
const MAX_SIGNALED_REVISIONS = MAX_ENTRIES * 2;
let activeRequests = 0;
let cacheEpoch = 0;

export function activityOverviewIsVisible(
  inspectorOpen: boolean,
  inspectorMode: 'overview' | 'work' | 'session' | 'context',
): boolean {
  // Work mode still relies on live topology (workstream tree, execution
  // states, todo progress) even though it renders WorkView instead of
  // InspectorOverview, so its refresh/subscription must not freeze.
  return inspectorOpen && (
    inspectorMode === 'overview' || inspectorMode === 'context' || inspectorMode === 'work'
  );
}

function metricNow(): number {
  return typeof performance === 'undefined' ? Date.now() : performance.now();
}

function recordClientMetric(metric: ClientPerformanceMetric, startedAt: number): void {
  try {
    void chatV2Api.clientPerformance(metric, Math.max(0, metricNow() - startedAt));
  } catch {
    // Client observability must never affect cache admission or rendering.
  }
}

function scopeEpoch(scopeKey: string): number {
  return scopeEpochs.get(scopeKey) ?? 0;
}

function supersedeScope(scopeKey: string): void {
  scopeEpochs.set(scopeKey, scopeEpoch(scopeKey) + 1);
}

function rememberSignaledRevision(scopeKey: string, revision: number): void {
  signaledRevisions.delete(scopeKey);
  signaledRevisions.set(scopeKey, revision);
  while (signaledRevisions.size > MAX_SIGNALED_REVISIONS) {
    const oldest = signaledRevisions.keys().next().value;
    if (typeof oldest !== 'string') break;
    signaledRevisions.delete(oldest);
  }
}

function abortError(): DOMException {
  return new DOMException('Activity overview request aborted', 'AbortError');
}

function retryableInitialError(error: unknown): boolean {
  if (isActivityOverviewAbort(error)) return true;
  if (typeof error !== 'object' || error === null) return false;
  const code = String('code' in error ? error.code : '');
  if ([
    'request_timeout',
    'activity_overview_timeout',
    'work_request_timeout',
    'network_error',
  ].includes(code)) return true;
  const status = Number('status' in error ? error.status : NaN);
  return Number.isFinite(status) && status >= 500;
}

function waitForInitialRetry(
  scopeKey: string,
  delayMs: number,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<void>((resolve, reject) => {
    const finish = (): void => {
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      if (initialRetryWakers.get(scopeKey) === finish) initialRetryWakers.delete(scopeKey);
      resolve();
    };
    const timer = setTimeout(() => {
      finish();
    }, delayMs);
    const abort = (): void => {
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      if (initialRetryWakers.get(scopeKey) === finish) initialRetryWakers.delete(scopeKey);
      reject(abortError());
    };
    initialRetryWakers.set(scopeKey, finish);
    signal.addEventListener('abort', abort, { once: true });
  });
}

async function loadInitialWithRetry(
  scopeKey: string,
  loader: ActivityOverviewLoader,
  signal: AbortSignal,
): Promise<ActivityOverviewResponse> {
  const deadline = Date.now() + INITIAL_RETRY_DEADLINE_MS;
  let attempt = 0;
  while (true) {
    try {
      return await loader(signal);
    } catch (error) {
      if (
        signal.aborted
        || !retryableInitialError(error)
        || attempt >= INITIAL_RETRY_DELAYS_MS.length
      ) throw error;
      const delay = INITIAL_RETRY_DELAYS_MS[attempt];
      attempt += 1;
      if (Date.now() + delay > deadline) throw error;
      await waitForInitialRetry(scopeKey, delay, signal);
    }
  }
}

export function isActivityOverviewAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

function responseRevision(value: ActivityOverviewResponse): number | null {
  const revision = Number(value.work_revision);
  return Number.isSafeInteger(revision) && revision >= 0 ? revision : null;
}

function sameScope(requested: TimelineScope, returned: TimelineScope): boolean {
  if (requested.key !== returned.key || requested.kind !== returned.kind) return false;
  if ((requested.conversation_id ?? null) !== (returned.conversation_id ?? null)) return false;
  if (requested.kind === 'session') {
    return (requested.session_id ?? null) === (returned.session_id ?? null);
  }
  if (requested.kind === 'task_step') {
    return (requested.step_run_id ?? null) === (returned.step_run_id ?? null);
  }
  return true;
}

export function admitsActivityOverviewResponse(
  scope: TimelineScope,
  value: ActivityOverviewResponse,
): boolean {
  return Boolean(value?.scope && sameScope(scope, value.scope));
}

function truncate(value: string | null | undefined): string | null | undefined {
  if (typeof value !== 'string' || value.length <= MAX_COMMAND_TEXT) return value;
  return `${value.slice(0, MAX_COMMAND_TEXT)}…`;
}

function normalizeCommand(command: ActivityOverviewResponse['recent_work'] extends infer Recent
  ? Recent extends { commands: Array<infer Item> } ? Item : never
  : never) {
  return {
    ...command,
    preview: truncate(command.preview),
    error: truncate(command.error),
    arguments: undefined,
    evaluation: undefined,
  };
}

function normalizeMutation(mutation: WorkMutationEvent): WorkMutationEvent {
  const rawPaths = Array.isArray(mutation.paths) ? mutation.paths : [];
  const rawStats = Array.isArray(mutation.file_stats) ? mutation.file_stats : [];
  const rawDiffs = Array.isArray(mutation.file_diffs) ? mutation.file_diffs : [];
  const totalFileCount = mutation.total_file_count
    ?? Math.max(rawPaths.length, rawStats.length, rawDiffs.length);
  const omittedFileCount = Math.max(
    mutation.omitted_file_count ?? 0,
    totalFileCount - MAX_FILE_ROWS,
  );
  return {
    id: mutation.id,
    call_id: mutation.call_id,
    sort_key: mutation.sort_key,
    created_at: mutation.created_at,
    updated_at: mutation.updated_at,
    tool_name: mutation.tool_name,
    display_name: mutation.display_name,
    category: mutation.category,
    operation_kind: mutation.operation_kind,
    status: mutation.status,
    duration_ms: mutation.duration_ms,
    arguments: {},
    output_size: mutation.output_size,
    truncated: mutation.truncated,
    has_full_output: mutation.has_full_output,
    recovery_call_id: mutation.recovery_call_id,
    tool_output_artifact_id: mutation.tool_output_artifact_id,
    paths: rawPaths.slice(0, MAX_FILE_ROWS),
    file_stats: rawStats.slice(0, MAX_FILE_ROWS).map((stat) => ({
      path: stat.path,
      path_id: stat.path_id,
      path_generation_id: stat.path_generation_id,
      relative_path: stat.relative_path,
      root_label: stat.root_label,
      root_name: stat.root_name,
      root_id: stat.root_id,
      additions: stat.additions,
      deletions: stat.deletions,
      preview_available: stat.preview_available,
    })),
    file_diffs: rawDiffs.slice(0, MAX_FILE_ROWS).map((diff) => ({
      path: diff.path,
      diff: '',
      path_id: diff.path_id,
      path_generation_id: diff.path_generation_id,
      relative_path: diff.relative_path,
      root_label: diff.root_label,
      root_name: diff.root_name,
      root_id: diff.root_id,
      additions: diff.additions,
      deletions: diff.deletions,
      content_truncated: Boolean(diff.diff) || diff.content_truncated,
      old_path: diff.old_path,
      status: diff.status,
      binary: diff.binary,
      generated: diff.generated,
      truncated: diff.truncated,
    })),
    diffs_truncated: mutation.diffs_truncated || rawDiffs.length > MAX_FILE_ROWS,
    total_file_count: totalFileCount,
    omitted_file_count: omittedFileCount,
    omitted_file_stat_count: Math.max(
      mutation.omitted_file_stat_count ?? 0,
      rawStats.length - MAX_FILE_ROWS,
    ),
    file_stats_recoverable: mutation.file_stats_recoverable,
    additions: mutation.additions,
    deletions: mutation.deletions,
  };
}

/** Keep navigation overviews small even when an older server returns full Work records. */
export function normalizeActivityOverview(value: ActivityOverviewResponse): ActivityOverviewResponse {
  const recentWork = value.recent_work;
  return {
    ...value,
    summary: { ...value.summary },
    materialization: value.materialization ? { ...value.materialization } : value.materialization,
    workstreams: value.workstreams.map((workstream) => ({
      ...workstream,
      summary: workstream.summary ? { ...workstream.summary } : workstream.summary,
    })),
    recent: Object.fromEntries(
      Object.entries(value.recent).map(([category, items]) => [
        category,
        (items ?? []).slice(0, category === 'commands' ? MAX_COMMAND_ROWS : 10).map((item) => ({ ...item })),
      ]),
    ),
    recent_work: recentWork ? {
      commands: recentWork.commands.slice(0, MAX_COMMAND_ROWS).map(normalizeCommand),
      files: recentWork.files.slice(0, 10).map(normalizeMutation),
      mutations: recentWork.mutations.slice(0, 10).map(normalizeMutation),
      artifacts: recentWork.artifacts.slice(0, 10).map((artifact) => ({ ...artifact })),
      deliverables: recentWork.deliverables.slice(0, 10).map((deliverable) => ({
        ...deliverable,
        content: undefined,
        render_metadata: undefined,
        export_metadata: undefined,
      })),
    } : recentWork,
  };
}

function touch(key: string, entry: CacheEntry): void {
  entries.delete(key);
  entries.set(key, entry);
}

function prune(now = Date.now()): void {
  for (const [key, entry] of entries) {
    if (now - entry.storedAt >= HARD_EVICTION_MS) entries.delete(key);
  }
}

function enforceBounds(scope: TimelineScope): void {
  const conversationId = scope.conversation_id;
  if (conversationId && scope.kind !== 'conversation') {
    const focused = [...entries].filter(([, entry]) =>
      entry.value.scope.conversation_id === conversationId && entry.value.scope.kind !== 'conversation'
    );
    while (focused.length > MAX_FOCUSED_PER_CONVERSATION) {
      const oldest = focused.shift();
      if (oldest) entries.delete(oldest[0]);
    }
  }
  while (entries.size > MAX_ENTRIES) {
    const oldest = entries.keys().next().value;
    if (typeof oldest !== 'string') break;
    entries.delete(oldest);
  }
}

export function getActivityOverviewEntry(scope: TimelineScope): ActivityOverviewCacheHit | null {
  const metricStartedAt = metricNow();
  const now = Date.now();
  prune(now);
  const entry = entries.get(scope.key);
  if (!entry || now - entry.storedAt >= STALE_MS) {
    recordClientMetric('activity_overview_cache_miss_ms', metricStartedAt);
    return null;
  }
  touch(scope.key, entry);
  const state = !entry.invalidated && now - entry.storedAt < FRESH_MS ? 'fresh' : 'stale';
  recordClientMetric(
    state === 'fresh'
      ? 'activity_overview_cache_fresh_ms'
      : 'activity_overview_cache_stale_ms',
    metricStartedAt,
  );
  return {
    value: entry.value,
    state,
  };
}

export function getActivityOverview(scope: TimelineScope): ActivityOverviewResponse | null {
  return getActivityOverviewEntry(scope)?.value ?? null;
}

export function setActivityOverview(scope: TimelineScope, value: ActivityOverviewResponse): ActivityOverviewResponse {
  if (!admitsActivityOverviewResponse(scope, value)) {
    throw new Error('Activity overview response scope does not match the request');
  }
  let normalized = normalizeActivityOverview(value);
  const existing = entries.get(scope.key);
  const hasEvidence = (candidate: ActivityOverviewResponse): boolean => Boolean(
    candidate.summary.changed_files
    || candidate.summary.commands
    || candidate.summary.mutations
    || candidate.summary.artifacts
    || candidate.summary.deliverables
    || candidate.workstreams.length
    || Object.values(candidate.recent).some((items) => (items?.length ?? 0) > 0)
    || (candidate.recent_work && Object.values(candidate.recent_work).some((items) => items.length > 0))
  );
  if (
    existing
    && normalized.materialization?.state !== 'live'
    && hasEvidence(existing.value)
  ) {
    const workstreams = new Map(existing.value.workstreams.map((item) => [item.key, item]));
    for (const item of normalized.workstreams) workstreams.set(item.key, item);
    const mergeRecent = <T extends { id: string; occurred_at?: string }>(prior: T[] = [], incoming: T[] = []): T[] => {
      const items = new Map(prior.map((item) => [item.id, item]));
      for (const item of incoming) items.set(item.id, item);
      return [...items.values()].sort((left, right) =>
        (right.occurred_at ?? '').localeCompare(left.occurred_at ?? '')
      );
    };
    const mergeBy = <T extends { sort_key?: string; created_at?: string | null }>(
      prior: T[],
      incoming: T[],
      key: (item: T) => string,
    ): T[] => {
      const items = new Map(prior.map((item) => [key(item), item]));
      for (const item of incoming) items.set(key(item), item);
      return [...items.values()].sort((left, right) =>
        (right.sort_key ?? right.created_at ?? '').localeCompare(left.sort_key ?? left.created_at ?? '')
      );
    };
    const categories = new Set([
      ...Object.keys(existing.value.recent),
      ...Object.keys(normalized.recent),
    ]);
    normalized = {
      ...existing.value,
      projection_version: normalized.projection_version,
      work_revision: normalized.work_revision,
      graph_revision: normalized.graph_revision,
      server_time: normalized.server_time,
      materialization: normalized.materialization,
      summary: {
        ...normalized.summary,
        mutations: Math.max(existing.value.summary.mutations, normalized.summary.mutations),
        commands: Math.max(existing.value.summary.commands, normalized.summary.commands),
        changed_files: Math.max(existing.value.summary.changed_files, normalized.summary.changed_files),
        artifacts: Math.max(existing.value.summary.artifacts, normalized.summary.artifacts),
        deliverables: Math.max(existing.value.summary.deliverables ?? 0, normalized.summary.deliverables ?? 0),
        additions: Math.max(existing.value.summary.additions ?? 0, normalized.summary.additions ?? 0),
        deletions: Math.max(existing.value.summary.deletions ?? 0, normalized.summary.deletions ?? 0),
      },
      workstreams: [...workstreams.values()],
      recent: Object.fromEntries([...categories].map((category) => [
        category,
        mergeRecent(
          existing.value.recent[category as keyof typeof existing.value.recent] ?? [],
          normalized.recent[category as keyof typeof normalized.recent] ?? [],
        ),
      ])),
      recent_work: existing.value.recent_work && normalized.recent_work ? {
        commands: mergeBy(existing.value.recent_work.commands, normalized.recent_work.commands, (item) => item.id),
        files: mergeBy(existing.value.recent_work.files, normalized.recent_work.files, (item) => item.id),
        mutations: mergeBy(existing.value.recent_work.mutations, normalized.recent_work.mutations, (item) => item.id),
        artifacts: mergeBy(existing.value.recent_work.artifacts, normalized.recent_work.artifacts, (item) => item.artifact_id),
        deliverables: mergeBy(existing.value.recent_work.deliverables, normalized.recent_work.deliverables, (item) => item.deliverable_id),
      } : normalized.recent_work ?? existing.value.recent_work,
    };
  }
  if (existing?.value.detail === 'full' && normalized.detail === 'lightweight') {
    const exactWorkstreams = new Map(
      existing.value.workstreams.map((item) => [item.key, item.summary]),
    );
    normalized = {
      ...normalized,
      detail: 'full',
      summary: existing.value.summary,
      workstreams: normalized.workstreams.map((item) => ({
        ...item,
        summary: exactWorkstreams.get(item.key) ?? item.summary,
      })),
    };
  }
  const existingRevision = existing ? responseRevision(existing.value) : null;
  const incomingRevision = responseRevision(normalized);
  if (
    existing
    && existingRevision !== null
    && incomingRevision !== null
    && incomingRevision < existingRevision
  ) {
    touch(scope.key, existing);
    return existing.value;
  }
  const signaledRevision = signaledRevisions.get(scope.key);
  const invalidated = signaledRevision !== undefined
    && (incomingRevision === null || incomingRevision < signaledRevision);
  entries.delete(scope.key);
  entries.set(scope.key, { value: normalized, storedAt: Date.now(), invalidated });
  enforceBounds(scope);
  return normalized;
}

export function invalidateActivityOverview(scopeKey: string, revision?: number): boolean {
  const safeRevision = Number.isSafeInteger(revision) && Number(revision) >= 0
    ? Number(revision)
    : null;
  const direct = entries.get(scopeKey);
  const cachedRevision = direct ? responseRevision(direct.value) : null;
  const previousSignal = signaledRevisions.get(scopeKey);
  const knownRevision = Math.max(previousSignal ?? -1, cachedRevision ?? -1);
  if (safeRevision !== null && safeRevision <= knownRevision) return false;
  if (safeRevision !== null) rememberSignaledRevision(scopeKey, safeRevision);
  initialRetryWakers.get(scopeKey)?.();
  if (direct) {
    direct.invalidated = true;
    supersedeScope(scopeKey);
    requests.get(scopeKey)?.controller.abort();
  } else {
    const request = requests.get(scopeKey);
    if (safeRevision === null || !request) {
      supersedeScope(scopeKey);
      request?.controller.abort();
    }
  }
  return true;
}

function pumpQueue(): void {
  while (activeRequests < MAX_CONCURRENT_REQUESTS) {
    const next = requestQueue.shift();
    if (!next) return;
    next();
  }
}

function runBounded<T>(loader: () => Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let started = false;
    let settled = false;
    let slotHeld = false;
    const releaseSlot = (): void => {
      if (!slotHeld) return;
      slotHeld = false;
      activeRequests -= 1;
      pumpQueue();
    };
    const settleRejected = (error: unknown): void => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    const run = (): void => {
      if (signal.aborted) {
        settleRejected(abortError());
        return;
      }
      started = true;
      slotHeld = true;
      activeRequests += 1;
      void loader().then((value) => {
        if (signal.aborted) settleRejected(abortError());
        else if (!settled) {
          settled = true;
          resolve(value);
        }
      }, settleRejected).finally(() => {
        signal.removeEventListener('abort', abortQueued);
        releaseSlot();
      });
    };
    const abortQueued = (): void => {
      if (started) {
        settleRejected(abortError());
        releaseSlot();
        return;
      }
      const index = requestQueue.indexOf(run);
      if (index >= 0) requestQueue.splice(index, 1);
      settleRejected(abortError());
      pumpQueue();
    };
    signal.addEventListener('abort', abortQueued, { once: true });
    if (activeRequests < MAX_CONCURRENT_REQUESTS) run();
    else requestQueue.push(run);
  });
}

function abortRequestWithoutConsumers(request: ScopedRequest): void {
  if (request.hasUnscopedConsumer || request.subscribers.size > 0) return;
  if (requests.get(request.scope.key) === request) {
    requests.delete(request.scope.key);
    supersedeScope(request.scope.key);
  }
  request.controller.abort();
}

function attachRequestConsumer(
  request: ScopedRequest,
  promise: Promise<ActivityOverviewResponse>,
  signal?: AbortSignal,
): Promise<ActivityOverviewResponse> {
  if (!signal) {
    request.hasUnscopedConsumer = true;
    return promise;
  }
  if (signal.aborted) {
    abortRequestWithoutConsumers(request);
    return Promise.reject(abortError());
  }
  request.subscribers.add(signal);
  return new Promise<ActivityOverviewResponse>((resolve, reject) => {
    let settled = false;
    const cleanup = (): void => {
      signal.removeEventListener('abort', abortConsumer);
      request.subscribers.delete(signal);
    };
    const abortConsumer = (): void => {
      if (settled) return;
      settled = true;
      cleanup();
      abortRequestWithoutConsumers(request);
      reject(abortError());
    };
    signal.addEventListener('abort', abortConsumer, { once: true });
    void promise.then(
      (value) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(value);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      },
    );
  });
}

function attachRerunConsumer(
  consumers: RerunConsumers,
  promise: Promise<ActivityOverviewResponse>,
  signal?: AbortSignal,
): Promise<ActivityOverviewResponse> {
  if (!signal) {
    consumers.hasUnscopedConsumer = true;
    return promise;
  }
  if (signal.aborted) return Promise.reject(abortError());
  consumers.subscribers.add(signal);
  return new Promise<ActivityOverviewResponse>((resolve, reject) => {
    let settled = false;
    const cleanup = (): void => {
      signal.removeEventListener('abort', abortConsumer);
      consumers.subscribers.delete(signal);
    };
    const abortConsumer = (): void => {
      if (settled) return;
      settled = true;
      cleanup();
      if (!consumers.hasUnscopedConsumer && consumers.subscribers.size === 0) {
        consumers.controller.abort();
      }
      reject(abortError());
    };
    signal.addEventListener('abort', abortConsumer, { once: true });
    void promise.then(
      (value) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(value);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      },
    );
  });
}

export function requestActivityOverview(
  scope: TimelineScope,
  loader: ActivityOverviewLoader,
  options: {
    signal?: AbortSignal;
    immediate?: boolean;
    exactLoader?: ActivityOverviewLoader;
    onExact?: (value: ActivityOverviewResponse) => void;
  } = {},
): Promise<ActivityOverviewResponse> {
  if (options.signal?.aborted) return Promise.reject(abortError());
  const cached = entries.get(scope.key);
  if (
    !options.immediate
    && cached?.invalidated
    && signaledRevisions.has(scope.key)
  ) {
    return scheduleActivityOverviewRevalidation(scope, loader, options);
  }
  const requestScopeEpoch = scopeEpoch(scope.key);
  const existing = requests.get(scope.key);
  if (existing?.cacheEpoch === cacheEpoch && existing.epoch === requestScopeEpoch) {
    if (options.exactLoader && !existing.exactLoader) {
      existing.exactLoader = options.exactLoader;
    }
    if (options.onExact) existing.exactCallbacks.add(options.onExact);
    if (existing.admitted) launchScopedExact(existing);
    recordClientMetric('activity_overview_request_deduplicated_ms', metricNow());
    const targetRevision = signaledRevisions.get(scope.key);
    if (existing.rerunPromise && existing.rerunConsumers) {
      if (!existing.rerunConsumers.controller.signal.aborted) {
        return attachRerunConsumer(
          existing.rerunConsumers,
          existing.rerunPromise,
          options.signal,
        );
      }
      existing.rerunPromise = undefined;
      existing.rerunConsumers = undefined;
    }
    if (
      targetRevision !== undefined
      && !entries.has(scope.key)
      && existing.rerunPromise === undefined
    ) {
      const rerunConsumers: RerunConsumers = {
        controller: new AbortController(),
        hasUnscopedConsumer: false,
        subscribers: new Set(),
      };
      existing.rerunConsumers = rerunConsumers;
      existing.rerunPromise = existing.promise.then((value) => {
        const returnedRevision = responseRevision(value);
        if (returnedRevision !== null && returnedRevision >= targetRevision) return value;
        if (requests.get(scope.key) === existing) requests.delete(scope.key);
        supersedeScope(scope.key);
        return requestActivityOverview(
          scope,
          loader,
          {
            signal: rerunConsumers.controller.signal,
            exactLoader: existing.exactLoader,
            onExact: (exact) => {
              for (const callback of existing.exactCallbacks) callback(exact);
            },
          },
        );
      });
      return attachRerunConsumer(
        rerunConsumers,
        existing.rerunPromise,
        options.signal,
      );
    }
    return attachRequestConsumer(existing, existing.promise, options.signal);
  }
  const controller = new AbortController();
  const requestEpoch = cacheEpoch;
  const metricStartedAt = metricNow();
  let scopedRequest: ScopedRequest;
  const request = runBounded(
    () => cached
      ? loader(controller.signal)
      : loadInitialWithRetry(scope.key, loader, controller.signal),
    controller.signal,
  )
    .then((value) => {
      if (!admitsActivityOverviewResponse(scope, value)) {
        throw new Error('Activity overview response scope does not match the request');
      }
      if (controller.signal.aborted) throw abortError();
      const admitted = requestEpoch === cacheEpoch && requestScopeEpoch === scopeEpoch(scope.key)
        ? setActivityOverview(scope, value)
        : normalizeActivityOverview(value);
      scopedRequest.admitted = admitted;
      scopedRequest.exactRequired = value.detail !== 'full';
      launchScopedExact(scopedRequest);
      return admitted;
    })
    .then(
      (value) => {
        recordClientMetric('activity_overview_request_success_ms', metricStartedAt);
        return value;
      },
      (error: unknown) => {
        recordClientMetric(
          isActivityOverviewAbort(error)
            ? 'activity_overview_request_aborted_ms'
            : 'activity_overview_request_error_ms',
          metricStartedAt,
        );
        throw error;
      },
    )
    .finally(() => {
      if (requests.get(scope.key)?.promise === request) requests.delete(scope.key);
    });
  scopedRequest = {
    cacheEpoch: requestEpoch,
    controller,
    epoch: requestScopeEpoch,
    exactCallbacks: new Set(options.onExact ? [options.onExact] : []),
    exactLoader: options.exactLoader,
    exactRequired: false,
    exactStarted: false,
    hasUnscopedConsumer: false,
    loader,
    scope,
    subscribers: new Set(),
    promise: request,
  };
  requests.set(scope.key, scopedRequest);
  return attachRequestConsumer(scopedRequest, request, options.signal);
}

function launchScopedExact(request: ScopedRequest): void {
  if (
    request.exactStarted
    || !request.exactLoader
    || !request.admitted
    || !request.exactRequired
  ) return;
  request.exactStarted = true;
  const expectedWorkRevision = responseRevision(request.admitted);
  const expectedGraphRevision = request.admitted.graph_revision ?? null;
  const exactController = new AbortController();
  void request.exactLoader(exactController.signal).then((exact) => {
    const current = entries.get(request.scope.key)?.value;
    if (
      request.cacheEpoch !== cacheEpoch
      || request.epoch !== scopeEpoch(request.scope.key)
      || responseRevision(current ?? exact) !== expectedWorkRevision
      || (current?.graph_revision ?? null) !== expectedGraphRevision
      || exact.work_revision !== expectedWorkRevision
      || (exact.graph_revision ?? null) !== expectedGraphRevision
    ) return;
    const admittedExact = setActivityOverview(request.scope, exact);
    for (const callback of request.exactCallbacks) callback(admittedExact);
  }).catch(() => undefined);
}

function scheduleActivityOverviewRevalidation(
  scope: TimelineScope,
  loader: ActivityOverviewLoader,
  options: {
    signal?: AbortSignal;
    exactLoader?: ActivityOverviewLoader;
    onExact?: (value: ActivityOverviewResponse) => void;
  },
): Promise<ActivityOverviewResponse> {
  let scheduled = scheduledRequests.get(scope.key);
  const now = Date.now();
  if (!scheduled) {
    let resolve!: (value: ActivityOverviewResponse) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<ActivityOverviewResponse>((ok, fail) => {
      resolve = ok;
      reject = fail;
    });
    const controller = new AbortController();
    scheduled = {
      controller,
      deadlineAt: now + REVALIDATE_MAX_DELAY_MS,
      exactCallbacks: new Set(options.onExact ? [options.onExact] : []),
      exactLoader: options.exactLoader,
      hasUnscopedConsumer: false,
      loader,
      promise,
      reject,
      resolve,
      scope,
      subscribers: new Set(),
      timer: setTimeout(() => undefined, 0),
    };
    const run = (): void => {
      scheduledRequests.delete(scope.key);
      void requestActivityOverview(scope, scheduled!.loader, {
        signal: controller.signal,
        immediate: true,
        exactLoader: scheduled!.exactLoader,
        onExact: (exact) => {
          for (const callback of scheduled!.exactCallbacks) callback(exact);
        },
      }).then(resolve, reject);
    };
    scheduled.timer = setTimeout(run, REVALIDATE_DELAY_MS);
    scheduledRequests.set(scope.key, scheduled);
  } else {
    scheduled.loader = loader;
    if (options.exactLoader && !scheduled.exactLoader) {
      scheduled.exactLoader = options.exactLoader;
    }
    if (options.onExact) scheduled.exactCallbacks.add(options.onExact);
    clearTimeout(scheduled.timer);
    const wait = Math.max(
      0,
      Math.min(REVALIDATE_DELAY_MS, scheduled.deadlineAt - now),
    );
    scheduled.timer = setTimeout(() => {
      scheduledRequests.delete(scope.key);
      void requestActivityOverview(scope, scheduled!.loader, {
        signal: scheduled!.controller.signal,
        immediate: true,
        exactLoader: scheduled!.exactLoader,
        onExact: (exact) => {
          for (const callback of scheduled!.exactCallbacks) callback(exact);
        },
      }).then(scheduled!.resolve, scheduled!.reject);
    }, wait);
  }
  if (!options.signal) {
    scheduled.hasUnscopedConsumer = true;
    return scheduled.promise;
  }
  const signal = options.signal;
  scheduled.subscribers.add(signal);
  return new Promise((resolve, reject) => {
    const abort = (): void => {
      scheduled!.subscribers.delete(signal);
      if (!scheduled!.hasUnscopedConsumer && scheduled!.subscribers.size === 0) {
        clearTimeout(scheduled!.timer);
        scheduledRequests.delete(scope.key);
        scheduled!.controller.abort();
        scheduled!.reject(abortError());
      }
      reject(abortError());
    };
    signal.addEventListener('abort', abort, { once: true });
    void scheduled!.promise.then(
      (value) => {
        signal.removeEventListener('abort', abort);
        scheduled!.subscribers.delete(signal);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener('abort', abort);
        scheduled!.subscribers.delete(signal);
        reject(error);
      },
    );
  });
}

export function abortActivityOverviewScope(scopeKey: string): void {
  const scheduled = scheduledRequests.get(scopeKey);
  if (scheduled) {
    clearTimeout(scheduled.timer);
    scheduled.controller.abort();
    scheduled.reject(abortError());
    scheduledRequests.delete(scopeKey);
  }
  requests.get(scopeKey)?.controller.abort();
  supersedeScope(scopeKey);
}

export function markAllActivityOverviewsStale(revision?: number): void {
  const scopeKeys = new Set([
    ...entries.keys(),
    ...requests.keys(),
    ...scheduledRequests.keys(),
  ]);
  for (const scopeKey of scopeKeys) {
    invalidateActivityOverview(scopeKey, revision);
  }
}

export function seedActivityOverviewFromSnapshot(snapshot: ChatSnapshot): ActivityOverviewResponse | null {
  const overview = snapshot.activity_overview ?? null;
  if (!overview) return null;
  const conversationId = snapshot.conversation?.conversation_id
    ?? (snapshot as unknown as { conversation_id?: string }).conversation_id;
  if (!conversationId) return null;
  if (
    overview.scope.kind !== 'conversation'
    || overview.scope.conversation_id !== conversationId
    || overview.scope.key !== `conversation:${conversationId}`
  ) return null;
  return setActivityOverview(overview.scope, overview);
}

export function visibleSnapshotOverview(
  snapshot: ChatSnapshot,
  currentScopeKey: string | null | undefined,
): ActivityOverviewResponse | null {
  const overview = seedActivityOverviewFromSnapshot(snapshot);
  return overview && overview.scope.key === currentScopeKey ? overview : null;
}

export function clearActivityOverview(scopeKey?: string): void {
  if (!scopeKey) {
    for (const scheduled of scheduledRequests.values()) {
      clearTimeout(scheduled.timer);
      scheduled.controller.abort();
      scheduled.reject(abortError());
    }
    scheduledRequests.clear();
    for (const request of requests.values()) request.controller.abort();
    entries.clear();
    cacheEpoch += 1;
    scopeEpochs.clear();
    signaledRevisions.clear();
    return;
  }
  const scheduled = scheduledRequests.get(scopeKey);
  if (scheduled) {
    clearTimeout(scheduled.timer);
    scheduled.controller.abort();
    scheduled.reject(abortError());
    scheduledRequests.delete(scopeKey);
  }
  requests.get(scopeKey)?.controller.abort();
  supersedeScope(scopeKey);
  entries.delete(scopeKey);
  signaledRevisions.delete(scopeKey);
}

import type { ActivityOverviewResponse, ChatSnapshot, TimelineScope, WorkMutationEvent } from '$lib/chat-v2/types';

const MAX_ENTRIES = 16;
const MAX_FOCUSED_PER_CONVERSATION = 8;
const FRESH_MS = 30_000;
const STALE_MS = 120_000;
const HARD_EVICTION_MS = 300_000;
const MAX_COMMAND_ROWS = 10;
const MAX_COMMAND_TEXT = 4_096;
const MAX_FILE_ROWS = 20;
const MAX_CONCURRENT_REQUESTS = 4;

type CacheEntry = {
  value: ActivityOverviewResponse;
  storedAt: number;
  invalidated: boolean;
};

export type ActivityOverviewCacheHit = {
  value: ActivityOverviewResponse;
  state: 'fresh' | 'stale';
};

const entries = new Map<string, CacheEntry>();
type ScopedRequest = {
  cacheEpoch: number;
  epoch: number;
  scope: TimelineScope;
  promise: Promise<ActivityOverviewResponse>;
};
const requests = new Map<string, ScopedRequest>();
const scopeEpochs = new Map<string, number>();
const requestQueue: Array<() => void> = [];
let activeRequests = 0;
let cacheEpoch = 0;

function scopeEpoch(scopeKey: string): number {
  return scopeEpochs.get(scopeKey) ?? 0;
}

function supersedeScope(scopeKey: string): void {
  scopeEpochs.set(scopeKey, scopeEpoch(scopeKey) + 1);
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
    materialization: { ...value.materialization },
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
  const now = Date.now();
  prune(now);
  const entry = entries.get(scope.key);
  if (!entry || now - entry.storedAt >= STALE_MS) return null;
  touch(scope.key, entry);
  return {
    value: entry.value,
    state: !entry.invalidated && now - entry.storedAt < FRESH_MS ? 'fresh' : 'stale',
  };
}

export function getActivityOverview(scope: TimelineScope): ActivityOverviewResponse | null {
  return getActivityOverviewEntry(scope)?.value ?? null;
}

export function setActivityOverview(scope: TimelineScope, value: ActivityOverviewResponse): ActivityOverviewResponse {
  const normalized = normalizeActivityOverview(value);
  entries.delete(scope.key);
  entries.set(scope.key, { value: normalized, storedAt: Date.now(), invalidated: false });
  enforceBounds(scope);
  return normalized;
}

export function invalidateActivityOverview(scopeKey: string): void {
  supersedeScope(scopeKey);
  const direct = entries.get(scopeKey);
  if (direct) direct.invalidated = true;
  const conversationId = scopeKey.startsWith('conversation:') ? scopeKey.slice('conversation:'.length) : null;
  if (!conversationId) return;
  const descendantKeys = new Set<string>();
  for (const [key, entry] of entries) {
    if (entry.value.scope.conversation_id === conversationId) descendantKeys.add(key);
  }
  for (const [key, request] of requests) {
    if (request.scope.conversation_id === conversationId) descendantKeys.add(key);
  }
  for (const key of descendantKeys) {
    if (key !== scopeKey) supersedeScope(key);
  }
  for (const entry of entries.values()) {
    if (entry.value.scope.conversation_id === conversationId) entry.invalidated = true;
  }
}

function runBounded<T>(loader: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const run = (): void => {
      activeRequests += 1;
      void loader().then(resolve, reject).finally(() => {
        activeRequests -= 1;
        requestQueue.shift()?.();
      });
    };
    if (activeRequests < MAX_CONCURRENT_REQUESTS) run();
    else requestQueue.push(run);
  });
}

export function requestActivityOverview(
  scope: TimelineScope,
  loader: () => Promise<ActivityOverviewResponse>,
): Promise<ActivityOverviewResponse> {
  const requestScopeEpoch = scopeEpoch(scope.key);
  const existing = requests.get(scope.key);
  if (existing?.cacheEpoch === cacheEpoch && existing.epoch === requestScopeEpoch) {
    return existing.promise;
  }
  const requestEpoch = cacheEpoch;
  const request = runBounded(loader)
    .then((value) =>
      requestEpoch === cacheEpoch && requestScopeEpoch === scopeEpoch(scope.key)
        ? setActivityOverview(scope, value)
        : normalizeActivityOverview(value)
    )
    .finally(() => {
      if (requests.get(scope.key)?.promise === request) requests.delete(scope.key);
    });
  requests.set(scope.key, { cacheEpoch: requestEpoch, epoch: requestScopeEpoch, scope, promise: request });
  return request;
}

export function seedActivityOverviewFromSnapshot(snapshot: ChatSnapshot): ActivityOverviewResponse | null {
  const overview = snapshot.activity_overview ?? null;
  if (!overview) return null;
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
    entries.clear();
    cacheEpoch += 1;
    scopeEpochs.clear();
    return;
  }
  supersedeScope(scopeKey);
  entries.delete(scopeKey);
  const conversationId = scopeKey.startsWith('conversation:') ? scopeKey.slice('conversation:'.length) : null;
  if (conversationId) {
    for (const [key, request] of requests) {
      if (request.scope.conversation_id === conversationId && key !== scopeKey) supersedeScope(key);
    }
    for (const [key, entry] of entries) {
      if (entry.value.scope.conversation_id === conversationId) {
        supersedeScope(key);
        entries.delete(key);
      }
    }
  }
}

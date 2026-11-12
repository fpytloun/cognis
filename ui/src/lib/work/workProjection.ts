import type {
  FileDiffRef,
  WorkDeliverable,
  WorkMutationEvent,
  WorkProjectionResponse,
} from '$lib/chat-v2/types';

export const WORK_PAGE_CACHE_LIMIT = 4;

export function retainExactWorkSummary(
  latest: WorkProjectionResponse,
  retained: WorkProjectionResponse | null | undefined,
): WorkProjectionResponse {
  if (latest.detail !== 'lightweight' || retained?.detail !== 'full') return latest;
  return { ...latest, summary: retained.summary };
}

export function applyExactWorkSummary(
  current: WorkProjectionResponse,
  exact: WorkProjectionResponse,
): WorkProjectionResponse {
  if (
    exact.detail !== 'full'
    || current.work_revision == null
    || exact.work_revision !== current.work_revision
  ) return current;
  return { ...current, detail: 'full', summary: exact.summary };
}

export interface AccumulatedWorkState {
  projection: WorkProjectionResponse;
  beforeCursor: string | null;
  freshRestartCursor: string | null;
  exhausted: boolean;
  loadedPages: number;
  rootsByRelative: Record<string, RootCandidate[]>;
  olderPages: WorkProjectionResponse[];
  removedCallIds: string[];
}

function newestFirst<T extends { sort_key?: string }>(items: T[]): T[] {
  return [...items].sort((left, right) => (right.sort_key ?? '').localeCompare(left.sort_key ?? ''));
}

/** Revisions of one deliverable share a lineage; Work shows one entry per lineage. */
export function workDeliverableLineageKey(item: Pick<WorkDeliverable, 'deliverable_id' | 'lineage_id'>): string {
  return item.lineage_id || item.deliverable_id;
}

function newerRevision(current: WorkDeliverable | undefined, candidate: WorkDeliverable, preferCandidate: boolean): boolean {
  if (!current) return true;
  const currentRevision = current.revision ?? 1;
  const candidateRevision = candidate.revision ?? 1;
  if (candidateRevision !== currentRevision) return candidateRevision > currentRevision;
  return preferCandidate;
}

function mergeDeliverables(
  current: WorkDeliverable[],
  incoming: WorkDeliverable[],
  preferIncoming: boolean,
): WorkDeliverable[] {
  const merged = new Map<string, WorkDeliverable>();
  for (const item of current) {
    const key = workDeliverableLineageKey(item);
    if (newerRevision(merged.get(key), item, true)) merged.set(key, item);
  }
  for (const item of incoming) {
    const key = workDeliverableLineageKey(item);
    if (newerRevision(merged.get(key), item, preferIncoming)) merged.set(key, item);
  }
  return newestFirst([...merged.values()]);
}

/**
 * Work lists one artifact per document: the newest revision it saw, with how many revisions of
 * that document the work produced. Older revisions stay reachable from the viewer's switcher.
 * Documents are ordered by their most recent revision, newest first; equal or missing times
 * keep the input order.
 */
export function latestWorkArtifacts<T extends {
  artifact_id: string; lineage_id?: string | null; revision?: number | null;
  created_at?: string | null; updated_at?: string | null;
}>(
  items: T[],
): Array<T & { revisions_here: number }> {
  const latest = new Map<string, T & { revisions_here: number }>();
  const newest = new Map<string, number>();
  const order = new Map<string, number>();
  items.forEach((item, index) => {
    const key = item.lineage_id || item.artifact_id;
    if (!order.has(key)) order.set(key, index);
    newest.set(key, Math.max(newest.get(key) ?? 0, activityTime(item)));
    const current = latest.get(key);
    const count = (current?.revisions_here ?? 0) + 1;
    if (!current || (item.revision ?? 1) > (current.revision ?? 1)) latest.set(key, { ...item, revisions_here: count });
    else current.revisions_here = count;
  });
  return [...latest.entries()]
    .sort(([a], [b]) => (newest.get(b)! - newest.get(a)!) || (order.get(a)! - order.get(b)!))
    .map(([, item]) => item);
}

/** Milliseconds of the last change of a Work item, or 0 when unknown. */
export function activityTime(item: { created_at?: string | null; updated_at?: string | null }): number {
  const value = Date.parse(item.updated_at ?? item.created_at ?? '');
  return Number.isFinite(value) ? value : 0;
}

export function orderedWorkDeliverables(
  items: WorkDeliverable[],
  primary: WorkDeliverable | null | undefined,
): WorkDeliverable[] {
  const merged = new Map<string, WorkDeliverable>();
  if (primary) merged.set(workDeliverableLineageKey(primary), primary);
  for (const item of items) {
    const key = workDeliverableLineageKey(item);
    if (newerRevision(merged.get(key), item, true)) merged.set(key, item);
  }
  // Newest change first; deliverables without a timestamp fall back to their sort key.
  // An item with neither keeps its place, so an unkeyed primary stays where it was put.
  return [...merged.values()].sort((left, right) => {
    const byTime = activityTime(right) - activityTime(left);
    if (byTime && activityTime(right) && activityTime(left)) return byTime;
    if (!left.sort_key || !right.sort_key) return byTime;
    return right.sort_key.localeCompare(left.sort_key);
  });
}

function mergeEvidence<T>(
  current: T[],
  incoming: T[],
  identity: (item: T) => string,
  preferIncoming = true,
): T[] {
  const merged = new Map(current.map((item) => [identity(item), item]));
  for (const item of incoming) {
    const key = identity(item);
    if (preferIncoming || !merged.has(key)) merged.set(key, item);
  }
  return newestFirst([...merged.values()] as (T & { sort_key?: string })[]) as T[];
}

function mergeWorkProjections(
  current: WorkProjectionResponse,
  incoming: WorkProjectionResponse,
  preferIncoming = true,
): WorkProjectionResponse {
  const removedCallIds = new Set(incoming.removed_call_ids ?? []);
  current = {
    ...current,
    commands: current.commands.filter((item) => !removedCallIds.has(item.call_id)),
  };
  const workstreams = new Map(
    (current.workstreams ?? []).map((item) => [item.key, item]),
  );
  for (const item of incoming.workstreams ?? []) {
    if (preferIncoming || !workstreams.has(item.key)) workstreams.set(item.key, item);
  }
  const deliverables = mergeDeliverables(
    current.deliverables ?? [],
    incoming.deliverables ?? [],
    preferIncoming,
  );
  return {
    ...(preferIncoming ? { ...current, ...incoming } : { ...incoming, ...current }),
    graph_fingerprint: incoming.graph_fingerprint ?? current.graph_fingerprint,
    workstreams: [...workstreams.values()].sort((left, right) => left.ordinal - right.ordinal),
    mutations: mergeEvidence(current.mutations, incoming.mutations, (item) => item.id, preferIncoming),
    commands: mergeEvidence(current.commands, incoming.commands, (item) => item.id || item.call_id, preferIncoming),
    removed_call_ids: incoming.removed_call_ids ?? [],
    artifacts: mergeEvidence(current.artifacts, incoming.artifacts, (item) => item.artifact_id, preferIncoming),
    deliverables,
    final_deliverable: (preferIncoming ? incoming.final_deliverable : current.final_deliverable)
      ?? (preferIncoming ? current.final_deliverable : incoming.final_deliverable)
      ?? deliverables[0]
      ?? null,
  };
}

export function createAccumulatedWorkState(page: WorkProjectionResponse): AccumulatedWorkState {
  const projection = mergeWorkProjections(
    { ...page, mutations: [], commands: [], artifacts: [], deliverables: [] },
    page,
  );
  return {
    projection,
    beforeCursor: page.has_more_before ? page.before_cursor ?? null : null,
    freshRestartCursor: page.has_more_before ? page.before_cursor ?? null : null,
    exhausted: !page.has_more_before || !page.before_cursor,
    loadedPages: 1,
    rootsByRelative: collectPageRootCandidates(projection),
    olderPages: [],
    removedCallIds: page.removed_call_ids ?? [],
  };
}

export function appendOlderWorkPage(
  state: AccumulatedWorkState,
  page: WorkProjectionResponse,
): AccumulatedWorkState {
  const removedCallIds = [...new Set([
    ...state.removedCallIds,
    ...(page.removed_call_ids ?? []),
  ])];
  const projection = {
    ...mergeWorkProjections(state.projection, page, false),
    graph_fingerprint: state.projection.graph_fingerprint ?? page.graph_fingerprint,
  };
  projection.commands = projection.commands.filter((item) => !removedCallIds.includes(item.call_id));
  return {
    projection,
    beforeCursor: page.has_more_before ? page.before_cursor ?? null : null,
    freshRestartCursor: state.freshRestartCursor,
    exhausted: !page.has_more_before || !page.before_cursor,
    loadedPages: state.loadedPages + 1,
    rootsByRelative: collectPageRootCandidates(projection),
    olderPages: [...state.olderPages, page],
    removedCallIds,
  };
}

export function refreshNewestWorkPage(
  state: AccumulatedWorkState,
  page: WorkProjectionResponse,
  options: { replaceCumulativeFiles?: boolean } = {},
): AccumulatedWorkState {
  if (page.materialization?.state && page.materialization.state !== 'live') {
    const projection = mergeWorkProjections(state.projection, page);
    projection.summary = {
      ...page.summary,
      mutations: Math.max(state.projection.summary.mutations, page.summary.mutations),
      commands: Math.max(state.projection.summary.commands, page.summary.commands),
      changed_files: Math.max(state.projection.summary.changed_files, page.summary.changed_files),
      artifacts: Math.max(state.projection.summary.artifacts, page.summary.artifacts),
      deliverables: Math.max(state.projection.summary.deliverables ?? 0, page.summary.deliverables ?? 0),
      additions: Math.max(state.projection.summary.additions ?? 0, page.summary.additions ?? 0),
      deletions: Math.max(state.projection.summary.deletions ?? 0, page.summary.deletions ?? 0),
    };
    return {
      ...state,
      projection,
      rootsByRelative: collectPageRootCandidates(projection),
    };
  }
  if (
    state.projection.graph_fingerprint
    && page.graph_fingerprint
    && state.projection.graph_fingerprint !== page.graph_fingerprint
  ) return createAccumulatedWorkState(page);
  const onlyNewestLoaded = state.loadedPages === 1 || options.replaceCumulativeFiles === true;
  let refreshed = createAccumulatedWorkState(page);
  for (const older of state.olderPages) refreshed = appendOlderWorkPage(refreshed, older);
  const presentCallIds = new Set(page.commands.map((item) => item.call_id));
  const removedCallIds = [...new Set([
    ...state.removedCallIds.filter((callId) => !presentCallIds.has(callId)),
    ...(page.removed_call_ids ?? []),
  ])];
  const merged = {
    ...refreshed.projection,
    commands: refreshed.projection.commands.filter((item) => !removedCallIds.includes(item.call_id)),
    removed_call_ids: removedCallIds,
  };
  const projection = options.replaceCumulativeFiles
    ? { ...merged, mutations: newestFirst(page.mutations) }
    : merged;
  return {
    projection,
    beforeCursor: onlyNewestLoaded
      ? (page.has_more_before ? page.before_cursor ?? null : null)
      : state.beforeCursor,
    freshRestartCursor: page.has_more_before ? page.before_cursor ?? null : null,
    exhausted: onlyNewestLoaded
      ? (!page.has_more_before || !page.before_cursor)
      : state.exhausted,
    loadedPages: options.replaceCumulativeFiles ? 1 : state.loadedPages,
    rootsByRelative: collectPageRootCandidates(projection),
    olderPages: options.replaceCumulativeFiles ? [] : state.olderPages,
    removedCallIds,
  };
}

export function restartAccumulatedWorkTraversal(
  state: AccumulatedWorkState,
): AccumulatedWorkState {
  return {
    ...state,
    beforeCursor: state.freshRestartCursor,
    exhausted: state.freshRestartCursor === null,
    loadedPages: 1,
  };
}

export interface WorkPageSlot {
  requestCursor?: string;
  page?: WorkProjectionResponse;
}

export interface WorkPageState {
  slots: WorkPageSlot[];
  currentIndex: number;
  newerEvidenceAvailable: boolean;
  pendingNewest?: WorkProjectionResponse;
  rootsByRelative: Record<string, RootCandidate[]>;
}

export function createWorkPageState(page: WorkProjectionResponse): WorkPageState {
  return {
    slots: [{ page }],
    currentIndex: 0,
    newerEvidenceAvailable: false,
    rootsByRelative: collectPageRootCandidates(page),
  };
}

export function currentWorkPage(state: WorkPageState): WorkProjectionResponse | null {
  return state.slots[state.currentIndex]?.page ?? null;
}

export function olderRequestCursor(state: WorkPageState): string | null {
  const current = currentWorkPage(state);
  return current?.has_more_before ? current.before_cursor ?? null : null;
}

function trimPageCache(state: WorkPageState): WorkPageState {
  const keep = new Set<number>([
    0,
    state.currentIndex,
    state.currentIndex - 1,
    state.currentIndex + 1,
  ]);
  let retained = 0;
  const slots = state.slots.map((slot, index) => {
    if (!slot.page || !keep.has(index) || retained >= WORK_PAGE_CACHE_LIMIT) {
      return { requestCursor: slot.requestCursor };
    }
    retained += 1;
    return slot;
  });
  return { ...state, slots };
}

export function storeOlderWorkPage(
  state: WorkPageState,
  requestCursor: string,
  page: WorkProjectionResponse,
): WorkPageState {
  const target = state.currentIndex + 1;
  const slots = [...state.slots];
  const existing = slots[target];
  if (existing?.requestCursor === requestCursor) {
    slots[target] = { requestCursor, page };
    slots.length = target + 1;
  } else {
    slots.splice(target, slots.length - target, { requestCursor, page });
  }
  return trimPageCache({
    ...state,
    slots,
    currentIndex: target,
    newerEvidenceAvailable: state.newerEvidenceAvailable,
    rootsByRelative: mergeRootCandidates(
      state.rootsByRelative,
      collectPageRootCandidates(page),
    ),
  });
}

export function storeWorkPageAt(
  state: WorkPageState,
  index: number,
  page: WorkProjectionResponse,
): WorkPageState {
  const slots = [...state.slots];
  slots[index] = { ...slots[index], page };
  return trimPageCache({
    ...state,
    slots,
    currentIndex: index,
    rootsByRelative: mergeRootCandidates(
      state.rootsByRelative,
      collectPageRootCandidates(page),
    ),
  });
}

export function moveToCachedWorkPage(
  state: WorkPageState,
  index: number,
): WorkPageState {
  if (!state.slots[index]?.page) return state;
  return trimPageCache({
    ...state,
    currentIndex: index,
    newerEvidenceAvailable: state.newerEvidenceAvailable,
  });
}

export function replaceNewestWorkPage(
  state: WorkPageState,
  page: WorkProjectionResponse,
): WorkPageState {
  if (
    state.slots[0]?.page?.graph_fingerprint
    && page.graph_fingerprint
    && state.slots[0].page.graph_fingerprint !== page.graph_fingerprint
  ) {
    return createWorkPageState(page);
  }
  const slots = [...state.slots];
  const previous = slots[0]?.page;
  const changed = !previous || pageEvidenceKey(previous) !== pageEvidenceKey(page);
  const rootsByRelative = mergeRootCandidates(
    state.rootsByRelative,
    collectPageRootCandidates(page),
  );
  if (state.currentIndex > 0 && changed) {
    return trimPageCache({
      ...state,
      pendingNewest: page,
      newerEvidenceAvailable: true,
      rootsByRelative,
    });
  }
  if (!changed) {
    slots[0] = { ...slots[0], page };
    return trimPageCache({ ...state, slots, rootsByRelative });
  }
  return trimPageCache({
    ...state,
    slots: [{ page }],
    currentIndex: 0,
    pendingNewest: undefined,
    newerEvidenceAvailable: false,
    rootsByRelative,
  });
}

function pageEvidenceKey(page: WorkProjectionResponse): string {
  return JSON.stringify({
    mutations: page.mutations.map((item) => [
      item.id,
      item.status,
      item.updated_at,
      item.file_diffs.map((diff) => diff.path_id ?? diff.path),
    ]),
    commands: page.commands.map((item) => [
      item.id,
      item.status,
      item.updated_at,
      item.output_size,
    ]),
    artifacts: page.artifacts.map((item) => [item.artifact_id, item.sort_key]),
    deliverables: (page.deliverables ?? []).map((item) => [
      item.deliverable_id,
      item.sort_key,
    ]),
  });
}

export function applyPendingNewestWorkPage(state: WorkPageState): WorkPageState {
  if (!state.pendingNewest || state.currentIndex !== 0) return state;
  const newest = state.pendingNewest;
  return {
    slots: [
      { page: newest },
      ...(newest.has_more_before && newest.before_cursor
        ? [{ requestCursor: newest.before_cursor }]
        : []),
    ],
    currentIndex: 0,
    newerEvidenceAvailable: false,
    pendingNewest: undefined,
    rootsByRelative: mergeRootCandidates(
      state.rootsByRelative,
      collectPageRootCandidates(newest),
    ),
  };
}

type FileIdentity = Pick<
  FileDiffRef,
  'path' | 'path_id' | 'relative_path' | 'root_label' | 'root_name' | 'root_id'
>;

interface RootCandidate {
  rootId: string;
  rootLabel: string;
}

function collectPageRootCandidates(
  page: WorkProjectionResponse,
): Record<string, RootCandidate[]> {
  const candidates = new Map<string, Map<string, RootCandidate>>();
  for (const event of page.mutations) {
    for (const entry of [...event.file_diffs, ...(event.file_stats ?? [])]) {
      if (!entry.relative_path || !entry.root_id) continue;
      const roots = candidates.get(entry.relative_path) ?? new Map<string, RootCandidate>();
      roots.set(entry.root_id, {
          rootId: entry.root_id,
          rootLabel: entry.root_label ?? entry.root_name ?? 'Work',
      });
      candidates.set(entry.relative_path, roots);
    }
  }
  return Object.fromEntries(
    [...candidates].map(([relativePath, roots]) => [
      relativePath,
      [...roots.values()],
    ]),
  );
}

function mergeRootCandidates(
  current: Record<string, RootCandidate[]>,
  incoming: Record<string, RootCandidate[]>,
): Record<string, RootCandidate[]> {
  const result = { ...current };
  for (const [relativePath, candidates] of Object.entries(incoming)) {
    const roots = new Map(
      (result[relativePath] ?? []).map((candidate) => [candidate.rootId, candidate]),
    );
    for (const candidate of candidates) roots.set(candidate.rootId, candidate);
    result[relativePath] = [...roots.values()];
  }
  return result;
}

function resolveIdentity(
  entry: FileIdentity,
  candidates: Record<string, RootCandidate[]>,
): FileIdentity {
  if (entry.root_id || !entry.relative_path) return entry;
  const matches = candidates[entry.relative_path] ?? [];
  if (matches.length === 1) {
    const root = matches[0];
    return {
      ...entry,
      path: `${root.rootLabel}/${entry.relative_path}`,
      path_id: `${root.rootId}:${entry.relative_path}`,
      root_id: root.rootId,
      root_label: root.rootLabel,
      root_name: root.rootLabel,
    };
  }
  return {
    ...entry,
    path: `Unscoped/${entry.relative_path}`,
    root_id: null,
    root_label: 'Unscoped',
    root_name: 'Unscoped',
  };
}

export function resolvedCurrentWorkPage(
  state: WorkPageState,
): WorkProjectionResponse | null {
  const page = currentWorkPage(state);
  if (!page) return null;
  return resolveProjection(page, state.rootsByRelative);
}

export function resolvedAccumulatedWorkProjection(
  state: AccumulatedWorkState,
): WorkProjectionResponse {
  return resolveProjection(state.projection, state.rootsByRelative);
}

function resolveProjection(
  page: WorkProjectionResponse,
  candidates: Record<string, RootCandidate[]>,
): WorkProjectionResponse {
  const mutations: WorkMutationEvent[] = page.mutations.map((event) => ({
    ...event,
    file_diffs: event.file_diffs.map((diff) => ({
      ...diff,
      ...resolveIdentity(diff, candidates),
    })),
    file_stats: event.file_stats?.map((stat) => {
      const resolved = resolveIdentity(stat, candidates);
      return {
        ...stat,
        ...resolved,
        path_id: resolved.path_id ?? stat.path_id,
      };
    }),
  }));
  return {
    ...page,
    mutations,
    // Summary counts describe the server-filtered result set. Category pages
    // and locally resolved file identities must not turn those totals into
    // loaded-array counts.
    summary: page.summary,
  };
}

export function workWindow<T>(items: T[], pageFromNewest: number, size = 100): {
  items: T[];
  hasOlderLoaded: boolean;
  hasNewerLoaded: boolean;
} {
  const end = Math.max(0, items.length - Math.max(0, pageFromNewest) * size);
  const start = Math.max(0, end - size);
  return {
    items: items.slice(start, end),
    hasOlderLoaded: start > 0,
    hasNewerLoaded: end < items.length,
  };
}

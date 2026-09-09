import { beforeEach, describe, expect, it, vi } from 'vitest';
import { conversationTimelineScope, sessionTimelineScope, type ActivityOverviewResponse, type ChatSnapshot, type TimelineScope } from '$lib/chat-v2/types';
import {
  activityOverviewIsVisible,
  abortActivityOverviewScope,
  clearActivityOverview,
  admitsActivityOverviewResponse,
  getActivityOverview,
  getActivityOverviewEntry,
  invalidateActivityOverview,
  markAllActivityOverviewsStale,
  normalizeActivityOverview,
  requestActivityOverview,
  seedActivityOverviewFromSnapshot,
  setActivityOverview,
  visibleSnapshotOverview,
} from './activityOverviewCache';
import { invalidateWorkScope } from '$lib/work/workViewState';
import { chatV2Api } from '$lib/chat-v2/api';
import {
  backChildViewToRoot,
  closeChildViewToRoot,
} from '$lib/childView';

function overview(scope: TimelineScope): ActivityOverviewResponse {
  return {
    schema_version: 2, projection_version: 'test', scope,
    summary: { mutations: 0, commands: 0, changed_files: 0, artifacts: 0 },
    materialization: { state: 'live' },
    workstreams: [], recent: {}, graph_fingerprint: 'graph', graph_truncated: false,
    work_revision: 1, graph_revision: 1,
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
}

function transientError(status = 504): Error & { status: number } {
  return Object.assign(new Error('temporary overview failure'), { status });
}

function timeoutError(): Error & { status: number; code: string } {
  return Object.assign(new Error('overview request timed out'), {
    status: 0,
    code: 'request_timeout',
  });
}

describe('activityOverviewCache', () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.spyOn(chatV2Api, 'clientPerformance').mockResolvedValue();
    clearActivityOverview();
  });

  it('retains an exact summary and rejects a stale delayed exact completion', async () => {
    const scope = conversationTimelineScope('delayed-exact');
    const prior = {
      ...overview(scope),
      detail: 'full' as const,
      summary: { mutations: 0, commands: 10, changed_files: 0, artifacts: 0 },
    };
    setActivityOverview(scope, prior);
    const exact = deferred<ActivityOverviewResponse>();
    const latest = {
      ...overview(scope),
      detail: 'lightweight' as const,
      work_revision: 2,
      summary: { mutations: 0, commands: 2, changed_files: 0, artifacts: 0 },
    };
    const admitted = await requestActivityOverview(
      scope,
      async () => latest,
      { exactLoader: async () => exact.promise },
    );
    expect(admitted.summary.commands).toBe(10);

    setActivityOverview(scope, {
      ...latest,
      work_revision: 3,
      summary: { ...latest.summary, commands: 3 },
    });
    exact.resolve({
      ...latest,
      detail: 'full',
      summary: { ...latest.summary, commands: 20 },
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(getActivityOverview(scope)).toMatchObject({
      work_revision: 3,
      summary: { commands: 10 },
    });
  });

  it('promotes a matching delayed exact summary to the active consumer', async () => {
    const scope = conversationTimelineScope('matching-delayed-exact');
    const exact = deferred<ActivityOverviewResponse>();
    const onExact = vi.fn();
    const latest = {
      ...overview(scope),
      detail: 'lightweight' as const,
      work_revision: 2,
      graph_revision: 7,
      summary: { mutations: 0, commands: 2, changed_files: 0, artifacts: 0 },
    };

    await requestActivityOverview(
      scope,
      async () => latest,
      {
        exactLoader: async () => exact.promise,
        onExact,
      },
    );
    exact.resolve({
      ...latest,
      detail: 'full',
      summary: { ...latest.summary, commands: 20 },
    });
    await vi.waitFor(() => expect(onExact).toHaveBeenCalledOnce());
    expect(onExact).toHaveBeenCalledWith(expect.objectContaining({
      detail: 'full',
      work_revision: 2,
      summary: expect.objectContaining({ commands: 20 }),
    }));
  });

  it('adds exact demand and all callbacks to an existing lightweight request', async () => {
    const scope = conversationTimelineScope('deduplicated-delayed-exact');
    const lightweight = deferred<ActivityOverviewResponse>();
    const exact = deferred<ActivityOverviewResponse>();
    const loader = vi.fn(async () => lightweight.promise);
    const exactLoader = vi.fn(async () => exact.promise);
    const firstOnExact = vi.fn();
    const secondOnExact = vi.fn();

    const first = requestActivityOverview(scope, loader);
    const second = requestActivityOverview(scope, loader, { exactLoader, onExact: firstOnExact });
    const third = requestActivityOverview(scope, loader, { exactLoader, onExact: secondOnExact });
    lightweight.resolve({
      ...overview(scope),
      detail: 'lightweight',
      work_revision: 2,
    });
    await Promise.all([first, second, third]);
    expect(loader).toHaveBeenCalledOnce();
    expect(exactLoader).toHaveBeenCalledOnce();

    exact.resolve({
      ...overview(scope),
      detail: 'full',
      work_revision: 2,
    });
    await vi.waitFor(() => expect(firstOnExact).toHaveBeenCalledOnce());
    expect(secondOnExact).toHaveBeenCalledOnce();
  });

  it('keeps a closed inspector demand-free, treats Work mode as visible, and deduplicates visible overview consumers', async () => {
    expect(activityOverviewIsVisible(false, 'overview')).toBe(false);
    expect(activityOverviewIsVisible(false, 'work')).toBe(false);
    expect(activityOverviewIsVisible(true, 'session')).toBe(false);
    // Work mode renders WorkView instead of InspectorOverview, but the tree
    // topology, execution states, and todo progress it depends on must keep
    // refreshing/subscribing, so Work mode counts as visible.
    expect(activityOverviewIsVisible(true, 'work')).toBe(true);
    expect(activityOverviewIsVisible(true, 'overview')).toBe(true);
    expect(activityOverviewIsVisible(true, 'context')).toBe(true);

    const scope = conversationTimelineScope('visible-overview');
    const loader = vi.fn(async () => overview(scope));
    const requests: Array<Promise<ActivityOverviewResponse>> = [];
    for (const [open, mode] of [
      [false, 'overview'],
      [false, 'work'],
      [false, 'session'],
      [true, 'session'],
    ] as const) {
      if (activityOverviewIsVisible(open, mode)) {
        requests.push(requestActivityOverview(scope, loader));
      }
    }
    expect(loader).not.toHaveBeenCalled();
    for (const mode of ['overview', 'work'] as const) {
      if (activityOverviewIsVisible(true, mode)) {
        requests.push(requestActivityOverview(scope, loader));
        requests.push(requestActivityOverview(scope, loader));
      }
    }
    await Promise.all(requests);
    expect(loader).toHaveBeenCalledOnce();
  });

  it('keeps presentation round-trips hot within the TTL', () => {
    const scope = conversationTimelineScope('conversation-1');
    const value = overview(scope);
    setActivityOverview(scope, value);
    expect(getActivityOverview(scope)).toEqual(value);
    expect(getActivityOverview(scope)).toEqual(value);
  });

  it('serves fresh, stale, then hard-misses while retaining stale until two minutes', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-07T12:00:00Z'));
    const scope = conversationTimelineScope('conversation-1');
    setActivityOverview(scope, overview(scope));
    vi.advanceTimersByTime(29_999);
    expect(getActivityOverviewEntry(scope)?.state).toBe('fresh');
    vi.advanceTimersByTime(2);
    expect(getActivityOverviewEntry(scope)?.state).toBe('stale');
    vi.advanceTimersByTime(90_000);
    expect(getActivityOverview(scope)).toBeNull();
  });

  it('clears only the exact scope because scope revisions are independent', () => {
    const conversation = conversationTimelineScope('conversation-1');
    const session = sessionTimelineScope('session-1', 'conversation-1');
    const other = conversationTimelineScope('conversation-2');
    setActivityOverview(conversation, overview(conversation));
    setActivityOverview(session, overview(session));
    setActivityOverview(other, overview(other));
    clearActivityOverview(conversation.key);
    expect(getActivityOverview(conversation)).toBeNull();
    expect(getActivityOverview(session)).not.toBeNull();
    expect(getActivityOverview(other)).not.toBeNull();
  });

  it('is explicitly invalidated by a Work lifecycle event', () => {
    const scope = conversationTimelineScope('conversation-1');
    setActivityOverview(scope, overview(scope));
    invalidateWorkScope(scope.key, { workRevision: 2 });
    expect(getActivityOverviewEntry(scope)?.state).toBe('stale');
  });

  it.each(['catching_up', 'partial', 'failed'] as const)(
    'retains prior evidence while surfacing an empty %s lifecycle refresh',
    (state) => {
      const scope = conversationTimelineScope(`live-retained-${state}`);
      const retained = overview(scope);
      retained.summary.changed_files = 4;
      retained.materialization = { state: 'live' };
      setActivityOverview(scope, retained);
      const incoming = overview(scope);
      incoming.materialization = { state };
      incoming.work_revision = (retained.work_revision ?? 0) + 1;
      incoming.workstreams = [];
      incoming.recent = {};
      const result = setActivityOverview(scope, incoming);
      expect(result.summary.changed_files).toBe(4);
      expect(result.materialization?.state).toBe(state);
      expect(getActivityOverview(scope)?.summary.changed_files).toBe(4);
    },
  );

  it.each(['partial', 'failed'] as const)(
    'merges a populated %s subset without dropping prior Overview evidence',
    (state) => {
      const scope = conversationTimelineScope(`subset-${state}`);
      const retained = overview(scope);
      retained.materialization = { state: 'live' };
      retained.summary.changed_files = 4;
      retained.recent.files = [
        { id: 'old-file', category: 'files', session_id: 'root', occurred_at: '2026-01-01T00:00:00Z' },
      ];
      setActivityOverview(scope, retained);
      const subset = overview(scope);
      subset.materialization = { state };
      subset.work_revision = (retained.work_revision ?? 0) + 1;
      subset.summary.changed_files = 1;
      subset.recent.files = [
        { id: 'new-file', category: 'files', session_id: 'root', occurred_at: '2026-01-02T00:00:00Z' },
      ];
      const result = setActivityOverview(scope, subset);
      expect(result.summary.changed_files).toBe(4);
      expect(result.recent.files?.map((item) => item.id)).toEqual(['new-file', 'old-file']);
      expect(result.materialization.state).toBe(state);
    },
  );

  it('keeps incoming partial recent Work evidence ahead of a retained full limit', () => {
    const scope = conversationTimelineScope('subset-ordering');
    const retained = overview(scope);
    retained.materialization = { state: 'live' };
    retained.recent_work = {
      commands: [],
      files: Array.from({ length: 10 }, (_, index) => ({
        id: `retained-${index}`,
        call_id: `retained-call-${index}`,
        sort_key: `00${9 - index}`,
        tool_name: 'apply_patch',
        category: 'file',
        operation_kind: 'mutation',
        status: 'complete',
        arguments: {},
        paths: [],
        file_diffs: [],
        diffs_truncated: false,
      })),
      mutations: [],
      artifacts: [],
      deliverables: [],
    };
    setActivityOverview(scope, retained);
    const partial = overview(scope);
    partial.materialization = { state: 'partial' };
    partial.work_revision = (retained.work_revision ?? 0) + 1;
    partial.recent_work = {
      commands: [],
      files: [{
        id: 'incoming-newest',
        call_id: 'incoming-call',
        sort_key: '999',
        tool_name: 'apply_patch',
        category: 'file',
        operation_kind: 'mutation',
        status: 'complete',
        arguments: {},
        paths: [],
        file_diffs: [],
        diffs_truncated: false,
      }],
      mutations: [],
      artifacts: [],
      deliverables: [],
    };
    const result = setActivityOverview(scope, partial);
    expect(result.recent_work?.files[0].id).toBe('incoming-newest');
  });

  it('deduplicates requests for one scope', async () => {
    const scope = conversationTimelineScope('conversation-1');
    const loader = vi.fn(async () => overview(scope));
    const first = requestActivityOverview(scope, loader);
    const second = requestActivityOverview(scope, loader);
    expect(first).toBe(second);
    await expect(first).resolves.toMatchObject({ scope });
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it('retries a transient first-load failure without exposing an intermediate rejection', async () => {
    const scope = conversationTimelineScope('cold-overview');
    const loader = vi.fn()
      .mockRejectedValueOnce(transientError())
      .mockResolvedValueOnce(overview(scope));

    const first = requestActivityOverview(scope, loader);
    const concurrent = requestActivityOverview(scope, loader);

    expect(concurrent).toBe(first);
    await expect(first).resolves.toMatchObject({ scope });
    expect(loader).toHaveBeenCalledTimes(2);
    expect(getActivityOverview(scope)).not.toBeNull();
  });

  it('retries the API client timeout shape with status zero', async () => {
    const scope = conversationTimelineScope('timeout-overview');
    const loader = vi.fn()
      .mockRejectedValueOnce(timeoutError())
      .mockResolvedValueOnce(overview(scope));

    await expect(requestActivityOverview(scope, loader)).resolves.toMatchObject({ scope });
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('lets a real invalidation wake a pending initial retry immediately', async () => {
    const scope = conversationTimelineScope('invalidated-cold-overview');
    const loader = vi.fn()
      .mockRejectedValueOnce(transientError())
      .mockResolvedValueOnce({ ...overview(scope), work_revision: 2 });
    const request = requestActivityOverview(scope, loader);
    await vi.waitFor(() => expect(loader).toHaveBeenCalledOnce());

    markAllActivityOverviewsStale(2);

    await vi.waitFor(() => expect(loader).toHaveBeenCalledTimes(2), { timeout: 150 });
    await expect(request).resolves.toMatchObject({ work_revision: 2 });
  });

  it('does not retry a failed refresh when a retained snapshot exists', async () => {
    const scope = conversationTimelineScope('retained-overview');
    setActivityOverview(scope, overview(scope));
    invalidateActivityOverview(scope.key, 2);
    const loader = vi.fn().mockRejectedValue(transientError());

    await expect(requestActivityOverview(scope, loader, { immediate: true }))
      .rejects.toMatchObject({ status: 504 });

    expect(loader).toHaveBeenCalledOnce();
    expect(getActivityOverview(scope)).not.toBeNull();
  });

  it('cancels a pending initial retry when its scope demand is aborted', async () => {
    const scope = conversationTimelineScope('aborted-cold-overview');
    const controller = new AbortController();
    const loader = vi.fn().mockRejectedValue(transientError());
    const request = requestActivityOverview(scope, loader, { signal: controller.signal });
    await vi.waitFor(() => expect(loader).toHaveBeenCalledOnce());

    controller.abort();

    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    expect(loader).toHaveBeenCalledOnce();
    expect(getActivityOverview(scope)).toBeNull();
  });

  it('limits global request concurrency to four', async () => {
    let active = 0;
    let peak = 0;
    const releases: Array<() => void> = [];
    const requests = Array.from({ length: 6 }, (_, index) => {
      const scope = conversationTimelineScope(`conversation-${index}`);
      return requestActivityOverview(scope, () => new Promise((resolve) => {
        active += 1;
        peak = Math.max(peak, active);
        releases.push(() => {
          active -= 1;
          resolve(overview(scope));
        });
      }));
    });
    await vi.waitFor(() => expect(releases).toHaveLength(4));
    releases.splice(0, 4).forEach((release) => release());
    await vi.waitFor(() => expect(releases).toHaveLength(2));
    releases.splice(0).forEach((release) => release());
    await Promise.all(requests);
    expect(peak).toBe(4);
  });

  it('keeps at most eight focused entries per conversation and sixteen globally', () => {
    for (let index = 0; index < 10; index += 1) {
      const scope = sessionTimelineScope(`session-${index}`, 'conversation-1');
      setActivityOverview(scope, overview(scope));
    }
    expect(getActivityOverview(sessionTimelineScope('session-0', 'conversation-1'))).toBeNull();
    expect(getActivityOverview(sessionTimelineScope('session-2', 'conversation-1'))).not.toBeNull();
    for (let index = 0; index < 10; index += 1) {
      const scope = conversationTimelineScope(`other-${index}`);
      setActivityOverview(scope, overview(scope));
    }
    expect(getActivityOverview(sessionTimelineScope('session-3', 'conversation-1'))).toBeNull();
  });

  it('bounds command rows and strips heavy overview payloads', () => {
    const scope = conversationTimelineScope('conversation-1');
    const value = overview(scope);
    value.recent_work = {
      commands: Array.from({ length: 12 }, (_, index) => ({
        id: `command-${index}`, call_id: `call-${index}`, sort_key: `${index}`,
        status: 'complete', preview: 'x'.repeat(8_000), preview_truncated: false,
        has_full_output: true, arguments: { secret: 'heavy' },
      })),
      files: [{
        id: 'file-1',
        call_id: 'call-file-1',
        sort_key: '1',
        tool_name: 'apply_patch',
        category: 'filesystem',
        operation_kind: 'write',
        status: 'complete',
        arguments: { patch: 'heavy' },
        paths: ['src/app.ts'],
        file_diffs: [{
          path: '/repo/src/app.ts',
          relative_path: 'src/app.ts',
          path_id: 'root:src/app.ts',
          additions: 3,
          deletions: 1,
          diff: '@@ -1 +1 @@\n-old\n+new',
        }],
        diffs_truncated: false,
      }], mutations: [], artifacts: [],
      deliverables: [{
        deliverable_id: 'deliverable-1', format: 'markdown', content: 'x'.repeat(8_000),
        render_metadata: { heavy: true },
      }],
    };
    (value.recent_work.files[0]!.file_diffs[0] as unknown as Record<string, unknown>)
      .unexpected_nested_payload = { heavy: true };
    const normalized = normalizeActivityOverview(value);
    expect(normalized.recent_work?.commands).toHaveLength(10);
    expect(normalized.recent_work?.commands[0]?.preview?.length).toBeLessThanOrEqual(4_097);
    expect(normalized.recent_work?.commands[0]?.arguments).toBeUndefined();
    expect(normalized.recent_work?.files[0]?.tool_name).toBe('apply_patch');
    expect(normalized.recent_work?.files[0]?.file_diffs).toEqual([expect.objectContaining({
      path: '/repo/src/app.ts',
      relative_path: 'src/app.ts',
      path_id: 'root:src/app.ts',
      additions: 3,
      deletions: 1,
      diff: '',
      content_truncated: true,
    })]);
    expect(normalized.recent_work?.files[0]?.file_diffs[0]).not.toHaveProperty('unexpected_nested_payload');
    expect(normalized.recent_work?.deliverables[0]?.content).toBeUndefined();
  });

  it('degrades safely when an older server returns malformed file metadata', () => {
    const scope = conversationTimelineScope('conversation-1');
    const value = overview(scope);
    value.recent_work = {
      commands: [],
      files: [{
        id: 'file-1', call_id: 'call-1', sort_key: '1', tool_name: 'apply_patch',
        category: 'filesystem', operation_kind: 'write', status: 'complete',
        arguments: {}, paths: ['src/app.ts'], file_diffs: null, diffs_truncated: false,
      }],
      mutations: [], artifacts: [], deliverables: [],
    } as unknown as NonNullable<ActivityOverviewResponse['recent_work']>;

    const normalized = normalizeActivityOverview(value);

    expect(normalized.recent_work?.files[0]?.file_diffs).toEqual([]);
    expect(normalized.recent_work?.files[0]?.paths).toEqual(['src/app.ts']);
  });

  it('does not repopulate cleared cache from an in-flight request', async () => {
    const scope = conversationTimelineScope('conversation-1');
    let release!: (value: ActivityOverviewResponse) => void;
    const pending = requestActivityOverview(scope, () => new Promise((resolve) => { release = resolve; }));
    clearActivityOverview();
    release(overview(scope));
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(getActivityOverview(scope)).toBeNull();
  });

  it('starts a new request after invalidation and ignores the late prior response', async () => {
    const scope = sessionTimelineScope('session-1', 'conversation-1');
    const first = deferred<ActivityOverviewResponse>();
    const second = deferred<ActivityOverviewResponse>();
    const stale = { ...overview(scope), projection_version: 'stale' };
    const fresh = { ...overview(scope), projection_version: 'fresh' };
    const requestOne = requestActivityOverview(scope, () => first.promise);
    invalidateActivityOverview(scope.key);
    const requestTwo = requestActivityOverview(scope, () => second.promise);
    expect(requestTwo).not.toBe(requestOne);
    second.resolve(fresh);
    await expect(requestTwo).resolves.toMatchObject({ projection_version: 'fresh' });
    first.resolve(stale);
    await expect(requestOne).rejects.toMatchObject({ name: 'AbortError' });
    expect(getActivityOverview(scope)?.projection_version).toBe('fresh');
  });

  it('conversation invalidation does not supersede an independent descendant scope', async () => {
    const conversation = conversationTimelineScope('conversation-1');
    const session = sessionTimelineScope('session-1', 'conversation-1');
    const first = deferred<ActivityOverviewResponse>();
    const requestOne = requestActivityOverview(session, () => first.promise);
    invalidateActivityOverview(conversation.key);
    const requestTwo = requestActivityOverview(session, async () => ({
      ...overview(session),
      projection_version: 'fresh',
    }));
    expect(requestTwo).toBe(requestOne);
    first.resolve({ ...overview(session), projection_version: 'stale' });
    await Promise.all([requestOne, requestTwo]);
    expect(getActivityOverview(session)?.projection_version).toBe('stale');
  });

  it('clear resets request epochs without letting a prior epoch dedupe or repopulate', async () => {
    const scope = conversationTimelineScope('conversation-1');
    const first = deferred<ActivityOverviewResponse>();
    const requestOne = requestActivityOverview(scope, () => first.promise);
    clearActivityOverview();
    const requestTwo = requestActivityOverview(scope, async () => ({
      ...overview(scope),
      projection_version: 'after-clear',
    }));
    expect(requestTwo).not.toBe(requestOne);
    first.resolve({ ...overview(scope), projection_version: 'before-clear' });
    await expect(requestOne).rejects.toMatchObject({ name: 'AbortError' });
    await requestTwo;
    expect(getActivityOverview(scope)?.projection_version).toBe('after-clear');
  });

  it('seeds the root overview from a snapshot without an endpoint request', () => {
    const scope = conversationTimelineScope('conversation-1');
    const value = overview(scope);
    const seeded = seedActivityOverviewFromSnapshot({
      schema_version: 2,
      projection_version: 'test',
      conversation_id: 'conversation-1',
      scope,
      timeline: { items: [], has_more_before: false },
      server_time: '2026-01-01T00:00:00Z',
      activity_overview: value,
    } as unknown as ChatSnapshot);
    expect(seeded).toEqual(value);
    expect(getActivityOverview(scope)).toEqual(value);
  });

  it('does not replace a focused session overview with a root snapshot overview', () => {
    const rootScope = conversationTimelineScope('conversation-1');
    const focusedScope = sessionTimelineScope('session-1', 'conversation-1');
    const rootOverview = overview(rootScope);
    const snapshot = {
      schema_version: 2, projection_version: 'test', conversation_id: 'conversation-1',
      scope: rootScope, timeline: { items: [], has_more_before: false },
      server_time: '2026-01-01T00:00:00Z', activity_overview: rootOverview,
    } as unknown as ChatSnapshot;
    expect(visibleSnapshotOverview(snapshot, focusedScope.key)).toBeNull();
    expect(getActivityOverview(rootScope)).toEqual(rootOverview);
  });

  it('rejects a response for another exact scope or conversation', async () => {
    const requested = sessionTimelineScope('session-1', 'conversation-1');
    const returned = sessionTimelineScope('session-2', 'conversation-1');
    expect(admitsActivityOverviewResponse(requested, overview(returned))).toBe(false);
    await expect(requestActivityOverview(requested, async () => overview(returned)))
      .rejects.toThrow('scope does not match');
    expect(getActivityOverview(requested)).toBeNull();
  });

  it('ignores equal and lower revisions and admits one strictly newer signal', () => {
    const scope = conversationTimelineScope('conversation-1');
    setActivityOverview(scope, { ...overview(scope), work_revision: 4 });
    expect(invalidateActivityOverview(scope.key, 3)).toBe(false);
    expect(invalidateActivityOverview(scope.key, 4)).toBe(false);
    expect(getActivityOverviewEntry(scope)?.state).toBe('fresh');
    expect(invalidateActivityOverview(scope.key, 5)).toBe(true);
    expect(invalidateActivityOverview(scope.key, 5)).toBe(false);
    expect(getActivityOverviewEntry(scope)?.state).toBe('stale');
  });

  it('keeps an old-backend signal-before-response usable and runs one bounded revalidation', async () => {
    const scope = conversationTimelineScope('conversation-1');
    const first = deferred<ActivityOverviewResponse>();
    const second = deferred<ActivityOverviewResponse>();
    let calls = 0;
    const loader = () => {
      calls += 1;
      return calls === 1 ? first.promise : second.promise;
    };
    const initial = requestActivityOverview(scope, loader);
    expect(invalidateActivityOverview(scope.key, 2)).toBe(true);
    const revalidated = requestActivityOverview(scope, loader);
    first.resolve({ ...overview(scope), work_revision: 1, projection_version: 'old-replica' });
    await expect(initial).resolves.toMatchObject({ projection_version: 'old-replica' });
    await vi.waitFor(() => expect(calls).toBe(2));
    second.resolve({ ...overview(scope), work_revision: 2, projection_version: 'current' });
    await expect(revalidated).resolves.toMatchObject({ projection_version: 'current' });
    expect(calls).toBe(2);
    expect(getActivityOverview(scope)?.projection_version).toBe('current');
  });

  it('shares the old-backend rerun across current consumers when one consumer leaves', async () => {
    const scope = conversationTimelineScope('mixed-version-consumers');
    const oldReplica = deferred<ActivityOverviewResponse>();
    const currentReplica = deferred<ActivityOverviewResponse>();
    let calls = 0;
    const loader = () => {
      calls += 1;
      return calls === 1 ? oldReplica.promise : currentReplica.promise;
    };
    const initial = requestActivityOverview(scope, loader);
    expect(invalidateActivityOverview(scope.key, 2)).toBe(true);
    const leaving = new AbortController();
    const staying = new AbortController();
    const obsoleteConsumer = requestActivityOverview(
      scope,
      loader,
      { signal: leaving.signal },
    );
    const currentConsumer = requestActivityOverview(
      scope,
      loader,
      { signal: staying.signal },
    );

    leaving.abort();
    await expect(obsoleteConsumer).rejects.toMatchObject({ name: 'AbortError' });
    oldReplica.resolve({
      ...overview(scope),
      work_revision: 1,
      projection_version: 'old-replica',
    });
    await expect(initial).resolves.toMatchObject({ projection_version: 'old-replica' });
    await vi.waitFor(() => expect(calls).toBe(2));

    currentReplica.resolve({
      ...overview(scope),
      work_revision: 2,
      projection_version: 'current-replica',
    });
    await expect(currentConsumer).resolves.toMatchObject({
      projection_version: 'current-replica',
    });
    expect(getActivityOverview(scope)?.projection_version).toBe('current-replica');
  });

  it('aborts a queued navigation request without consuming a limiter slot', async () => {
    const releases: Array<() => void> = [];
    const active = Array.from({ length: 4 }, (_, index) => {
      const scope = conversationTimelineScope(`active-${index}`);
      return requestActivityOverview(scope, () => new Promise((resolve) => {
        releases.push(() => resolve(overview(scope)));
      }));
    });
    await vi.waitFor(() => expect(releases).toHaveLength(4));
    const queuedScope = conversationTimelineScope('queued');
    const controller = new AbortController();
    const queuedLoader = vi.fn(async () => overview(queuedScope));
    const queued = requestActivityOverview(queuedScope, queuedLoader, { signal: controller.signal });
    controller.abort();
    await expect(queued).rejects.toMatchObject({ name: 'AbortError' });
    expect(queuedLoader).not.toHaveBeenCalled();
    releases.forEach((release) => release());
    await Promise.all(active);
  });

  it('reconnect aborts a cold in-flight response instead of caching it as fresh', async () => {
    const scope = conversationTimelineScope('cold-reconnect');
    const pending = deferred<ActivityOverviewResponse>();
    const request = requestActivityOverview(scope, () => pending.promise);

    markAllActivityOverviewsStale();
    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    pending.resolve(overview(scope));
    await Promise.resolve();

    expect(getActivityOverview(scope)).toBeNull();
  });

  it('releases an active limiter slot when navigation aborts a non-settling loader', async () => {
    const active = Array.from({ length: 4 }, (_, index) => {
      const scope = conversationTimelineScope(`non-settling-${index}`);
      const pending = deferred<ActivityOverviewResponse>();
      return {
        scope,
        pending,
        request: requestActivityOverview(scope, () => pending.promise),
      };
    });
    const queuedScope = conversationTimelineScope('after-active-abort');
    const queuedLoader = vi.fn(async () => overview(queuedScope));
    const queued = requestActivityOverview(queuedScope, queuedLoader);

    abortActivityOverviewScope(active[0].scope.key);
    await expect(active[0].request).rejects.toMatchObject({ name: 'AbortError' });
    await vi.waitFor(() => expect(queuedLoader).toHaveBeenCalledOnce());
    await expect(queued).resolves.toMatchObject({ scope: queuedScope });

    for (const item of active) item.pending.resolve(overview(item.scope));
    await Promise.allSettled(active.slice(1).map((item) => item.request));
  });

  it.each([
    ['close', closeChildViewToRoot],
    ['back', backChildViewToRoot],
  ] as const)(
    '%s navigation releases a focused child slot and rejects its late response',
    async (_label, leaveChild) => {
      const blockerReleases: Array<() => void> = [];
      const blockers = Array.from({ length: 3 }, (_, index) => {
        const blockerScope = conversationTimelineScope(`child-exit-blocker-${index}`);
        return requestActivityOverview(blockerScope, () => new Promise((resolve) => {
          blockerReleases.push(() => resolve(overview(blockerScope)));
        }));
      });
      const childScope = sessionTimelineScope('focused-child', 'root-conversation');
      const childResponse = deferred<ActivityOverviewResponse>();
      const childConsumer = new AbortController();
      const childRequest = requestActivityOverview(
        childScope,
        () => childResponse.promise,
        { signal: childConsumer.signal },
      );
      const rootScope = conversationTimelineScope('root-conversation');
      const rootLoader = vi.fn(async () => overview(rootScope));
      const rootRequest = requestActivityOverview(rootScope, rootLoader);

      await vi.waitFor(() => expect(blockerReleases).toHaveLength(3));
      expect(rootLoader).not.toHaveBeenCalled();

      let rootStateApplied = false;
      leaveChild(
        () => childConsumer.abort(),
        () => {
          rootStateApplied = true;
        },
      );
      expect(rootStateApplied).toBe(true);
      await expect(childRequest).rejects.toMatchObject({ name: 'AbortError' });
      await vi.waitFor(() => expect(rootLoader).toHaveBeenCalledOnce());
      await expect(rootRequest).resolves.toMatchObject({ scope: rootScope });

      childResponse.resolve(overview(childScope));
      await Promise.resolve();
      expect(getActivityOverview(childScope)).toBeNull();

      blockerReleases.forEach((release) => release());
      await Promise.all(blockers);
    },
  );

  it('keeps a current exact-scope request alive when one deduplicated consumer leaves', async () => {
    const scope = conversationTimelineScope('shared-exact-scope');
    const pending = deferred<ActivityOverviewResponse>();
    const loaderSignals: AbortSignal[] = [];
    const loader = vi.fn((signal: AbortSignal) => {
      loaderSignals.push(signal);
      return pending.promise;
    });
    const current = requestActivityOverview(scope, loader);
    const leaving = new AbortController();
    const obsoleteConsumer = requestActivityOverview(
      scope,
      loader,
      { signal: leaving.signal },
    );

    leaving.abort();
    await expect(obsoleteConsumer).rejects.toMatchObject({ name: 'AbortError' });
    expect(loaderSignals[0]?.aborted).toBe(false);

    pending.resolve(overview(scope));
    await expect(current).resolves.toMatchObject({ scope });
    expect(getActivityOverview(scope)).not.toBeNull();
    expect(loader).toHaveBeenCalledOnce();
  });

  it('keeps shared demand alive across one mode exit and aborts it after the last visible consumer closes', async () => {
    const scope = conversationTimelineScope('shared-visible-consumers');
    const pending = deferred<ActivityOverviewResponse>();
    const loaderSignals: AbortSignal[] = [];
    const loader = vi.fn((signal: AbortSignal) => {
      loaderSignals.push(signal);
      return pending.promise;
    });
    const overviewConsumer = new AbortController();
    const contextConsumer = new AbortController();
    const overviewRequest = requestActivityOverview(scope, loader, {
      signal: overviewConsumer.signal,
    });
    const contextRequest = requestActivityOverview(scope, loader, {
      signal: contextConsumer.signal,
    });

    overviewConsumer.abort();
    await expect(overviewRequest).rejects.toMatchObject({ name: 'AbortError' });
    expect(loaderSignals[0]?.aborted).toBe(false);

    contextConsumer.abort();
    await expect(contextRequest).rejects.toMatchObject({ name: 'AbortError' });
    expect(loaderSignals[0]?.aborted).toBe(true);
    expect(loader).toHaveBeenCalledOnce();
    pending.resolve(overview(scope));
  });

  it('emits only fixed client cache and request metric names', async () => {
    const scope = conversationTimelineScope('conversation-metrics');
    const metric = vi.mocked(chatV2Api.clientPerformance);
    metric.mockClear();
    expect(getActivityOverviewEntry(scope)).toBeNull();
    await requestActivityOverview(scope, async () => overview(scope));
    expect(getActivityOverviewEntry(scope)?.state).toBe('fresh');
    invalidateActivityOverview(scope.key);
    expect(getActivityOverviewEntry(scope)?.state).toBe('stale');

    expect(new Set(metric.mock.calls.map(([name]) => name))).toEqual(new Set([
      'activity_overview_cache_miss_ms',
      'activity_overview_cache_fresh_ms',
      'activity_overview_cache_stale_ms',
      'activity_overview_request_success_ms',
    ]));
  });

  it('debounces cached revision bursts to one latest exact-scope request', async () => {
    vi.useFakeTimers();
    const scope = conversationTimelineScope('debounced');
    setActivityOverview(scope, { ...overview(scope), work_revision: 1 });
    const loader = vi.fn(async () => ({ ...overview(scope), work_revision: 4 }));
    invalidateActivityOverview(scope.key, 2);
    const first = requestActivityOverview(scope, loader);
    invalidateActivityOverview(scope.key, 3);
    const second = requestActivityOverview(scope, loader);
    invalidateActivityOverview(scope.key, 4);
    const third = requestActivityOverview(scope, loader);
    expect(loader).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(500);
    await expect(Promise.all([first, second, third])).resolves.toHaveLength(3);
    expect(loader).toHaveBeenCalledOnce();
  });

  it('preserves delayed exact loading through scheduled revision revalidation', async () => {
    vi.useFakeTimers();
    const scope = conversationTimelineScope('debounced-exact');
    setActivityOverview(scope, { ...overview(scope), detail: 'full', work_revision: 1 });
    const loader = vi.fn(async () => ({
      ...overview(scope),
      detail: 'lightweight' as const,
      work_revision: 2,
    }));
    const exactLoader = vi.fn(async () => ({
      ...overview(scope),
      detail: 'full' as const,
      work_revision: 2,
    }));
    const onExact = vi.fn();
    invalidateActivityOverview(scope.key, 2);
    const pending = requestActivityOverview(scope, loader, { exactLoader, onExact });

    await vi.advanceTimersByTimeAsync(500);
    await pending;
    await vi.waitFor(() => expect(onExact).toHaveBeenCalledOnce());
    expect(loader).toHaveBeenCalledOnce();
    expect(exactLoader).toHaveBeenCalledOnce();
  });

  it('loads an uncached exact scope immediately', async () => {
    const scope = conversationTimelineScope('initial-miss');
    const loader = vi.fn(async () => overview(scope));
    await requestActivityOverview(scope, loader);
    expect(loader).toHaveBeenCalledOnce();
  });

  it('bounds continuous cached revalidation bursts to 1500ms', async () => {
    vi.useFakeTimers();
    const scope = conversationTimelineScope('max-debounce');
    setActivityOverview(scope, { ...overview(scope), work_revision: 1 });
    const loader = vi.fn(async () => ({ ...overview(scope), work_revision: 5 }));
    invalidateActivityOverview(scope.key, 2);
    const pending = [requestActivityOverview(scope, loader)];
    for (const revision of [3, 4, 5]) {
      await vi.advanceTimersByTimeAsync(400);
      invalidateActivityOverview(scope.key, revision);
      pending.push(requestActivityOverview(scope, loader));
    }
    expect(loader).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(300);
    await Promise.all(pending);
    expect(loader).toHaveBeenCalledOnce();
  });
});

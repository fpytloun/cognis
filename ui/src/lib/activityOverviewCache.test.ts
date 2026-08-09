import { beforeEach, describe, expect, it, vi } from 'vitest';
import { conversationTimelineScope, sessionTimelineScope, type ActivityOverviewResponse, type ChatSnapshot, type TimelineScope } from '$lib/chat-v2/types';
import {
  clearActivityOverview,
  getActivityOverview,
  getActivityOverviewEntry,
  invalidateActivityOverview,
  normalizeActivityOverview,
  requestActivityOverview,
  seedActivityOverviewFromSnapshot,
  setActivityOverview,
  visibleSnapshotOverview,
} from './activityOverviewCache';
import { invalidateWorkScope } from '$lib/work/workViewState';

function overview(scope: TimelineScope): ActivityOverviewResponse {
  return {
    schema_version: 2, projection_version: 'test', scope,
    summary: { mutations: 0, commands: 0, changed_files: 0, artifacts: 0 },
    materialization: { state: 'caught_up', completed_streams: 1, total_streams: 1, covered_events: 1, target_events: 1, failed_streams: 0 },
    workstreams: [], recent: {}, graph_fingerprint: 'graph', graph_truncated: false,
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
}

describe('activityOverviewCache', () => {
  beforeEach(() => {
    vi.useRealTimers();
    clearActivityOverview();
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

  it('invalidates conversation and descendant session entries without touching another conversation', () => {
    const conversation = conversationTimelineScope('conversation-1');
    const session = sessionTimelineScope('session-1', 'conversation-1');
    const other = conversationTimelineScope('conversation-2');
    setActivityOverview(conversation, overview(conversation));
    setActivityOverview(session, overview(session));
    setActivityOverview(other, overview(other));
    clearActivityOverview(conversation.key);
    expect(getActivityOverview(conversation)).toBeNull();
    expect(getActivityOverview(session)).toBeNull();
    expect(getActivityOverview(other)).not.toBeNull();
  });

  it('is explicitly invalidated by a Work lifecycle event', () => {
    const scope = conversationTimelineScope('conversation-1');
    setActivityOverview(scope, overview(scope));
    invalidateWorkScope(scope.key, { workRevision: 2 });
    expect(getActivityOverviewEntry(scope)?.state).toBe('stale');
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
    await pending;
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
    await requestOne;
    expect(getActivityOverview(scope)?.projection_version).toBe('fresh');
  });

  it('conversation invalidation supersedes in-flight descendant requests', async () => {
    const conversation = conversationTimelineScope('conversation-1');
    const session = sessionTimelineScope('session-1', 'conversation-1');
    const first = deferred<ActivityOverviewResponse>();
    const requestOne = requestActivityOverview(session, () => first.promise);
    invalidateActivityOverview(conversation.key);
    const requestTwo = requestActivityOverview(session, async () => ({
      ...overview(session),
      projection_version: 'fresh',
    }));
    first.resolve({ ...overview(session), projection_version: 'stale' });
    await Promise.all([requestOne, requestTwo]);
    expect(getActivityOverview(session)?.projection_version).toBe('fresh');
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
    await Promise.all([requestOne, requestTwo]);
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
});

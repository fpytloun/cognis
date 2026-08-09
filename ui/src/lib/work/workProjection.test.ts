import { describe, expect, it } from 'vitest';

import {
  conversationTimelineScope,
  type WorkMutationEvent,
  type WorkProjectionResponse,
} from '$lib/chat-v2/types';
import {
  createWorkPageState,
  createAccumulatedWorkState,
  appendOlderWorkPage,
  refreshNewestWorkPage,
  restartAccumulatedWorkTraversal,
  resolvedAccumulatedWorkProjection,
  applyPendingNewestWorkPage,
  currentWorkPage,
  moveToCachedWorkPage,
  orderedWorkDeliverables,
  replaceNewestWorkPage,
  resolvedCurrentWorkPage,
  storeOlderWorkPage,
  storeWorkPageAt,
  WORK_PAGE_CACHE_LIMIT,
  workWindow,
} from './workProjection';

function page(
  index: number,
  options: { hasMore?: boolean; cursor?: string; mutation?: WorkMutationEvent } = {},
): WorkProjectionResponse {
  return {
    schema_version: 2,
    projection_version: 'test',
    scope: conversationTimelineScope('conversation-1'),
    mutations: options.mutation ? [options.mutation] : [],
    commands: [{
      id: `command-${index}`,
      call_id: `call-${index}`,
      sort_key: String(index).padStart(4, '0'),
      status: 'complete',
      command: `command ${index}`,
      preview_truncated: false,
      has_full_output: false,
    }],
    artifacts: [],
    deliverables: [],
    summary: {
      mutations: 0,
      commands: 1,
      changed_files: options.mutation ? 1 : 0,
      artifacts: 0,
    },
    has_more_before: options.hasMore ?? false,
    before_cursor: options.cursor,
    server_time: `2026-01-01T00:00:${String(index).padStart(2, '0')}Z`,
  };
}

function mutation(
  id: string,
  identity: {
    path: string;
    pathId: string;
    relativePath: string;
    rootId?: string;
    rootLabel?: string;
  },
): WorkMutationEvent {
  return {
    id,
    call_id: `call-${id}`,
    sort_key: id,
    tool_name: 'write',
    category: 'filesystem',
    operation_kind: 'file_write',
    status: 'complete',
    arguments: {},
    paths: [identity.path],
    file_stats: [{
      path: identity.path,
      path_id: identity.pathId,
      relative_path: identity.relativePath,
      root_id: identity.rootId,
      root_label: identity.rootLabel,
      additions: 1,
      deletions: 0,
      preview_available: true,
    }],
    file_diffs: [{
      path: identity.path,
      path_id: identity.pathId,
      relative_path: identity.relativePath,
      root_id: identity.rootId,
      root_label: identity.rootLabel,
      diff: '+new',
    }],
    diffs_truncated: false,
  };
}

describe('work page navigation', () => {
  it('accumulates newest-first evidence and deduplicates overlapping cursor pages', () => {
    const newest = page(10, { hasMore: true, cursor: 'older' });
    const older = page(9);
    older.commands.push({ ...newest.commands[0], command: 'overlap' });
    let state = createAccumulatedWorkState(newest);
    state = appendOlderWorkPage(state, older);

    expect(resolvedAccumulatedWorkProjection(state).commands.map((item) => item.id)).toEqual([
      'command-10',
      'command-9',
    ]);
    expect(state.exhausted).toBe(true);
  });

  it('keeps deliverables newest-first across older pages and newest refreshes with dedupe', () => {
    const newest = page(10, { hasMore: true, cursor: 'older' });
    newest.deliverables = [
      { deliverable_id: 'new', format: 'markdown', sort_key: '0010' },
      { deliverable_id: 'shared', format: 'markdown', sort_key: '0009' },
    ];
    const older = page(8);
    older.deliverables = [
      { deliverable_id: 'shared', format: 'markdown', sort_key: '0009' },
      { deliverable_id: 'old', format: 'markdown', sort_key: '0008' },
    ];
    let state = appendOlderWorkPage(createAccumulatedWorkState(newest), older);
    expect(state.projection.deliverables?.map((item) => item.deliverable_id)).toEqual([
      'new', 'shared', 'old',
    ]);

    const refreshed = page(11, { hasMore: true, cursor: 'older' });
    refreshed.deliverables = [
      { deliverable_id: 'newest', format: 'markdown', sort_key: '0011' },
      { deliverable_id: 'new', format: 'markdown', sort_key: '0010' },
    ];
    state = refreshNewestWorkPage(state, refreshed);
    expect(state.projection.deliverables?.map((item) => item.deliverable_id)).toEqual([
      'newest', 'new', 'shared', 'old',
    ]);
  });

  it('sorts an older primary by its key and preserves deterministic placement without a key', () => {
    const supporting = [
      { deliverable_id: 'new', format: 'markdown', sort_key: '0010' },
      { deliverable_id: 'old', format: 'markdown', sort_key: '0008' },
    ];
    expect(orderedWorkDeliverables(supporting, {
      deliverable_id: 'primary', format: 'markdown', sort_key: '0009',
    }).map((item) => item.deliverable_id)).toEqual(['new', 'primary', 'old']);
    expect(orderedWorkDeliverables(supporting, {
      deliverable_id: 'unkeyed-primary', format: 'markdown',
    }).map((item) => item.deliverable_id)).toEqual(['unkeyed-primary', 'new', 'old']);
  });

  it('merges a newest refresh but resets accumulated evidence for a graph change', () => {
    const newest = page(2, { hasMore: true, cursor: 'older' });
    newest.graph_fingerprint = 'graph-a';
    let state = appendOlderWorkPage(createAccumulatedWorkState(newest), page(1));
    state = refreshNewestWorkPage(state, page(3, { hasMore: true, cursor: 'new-boundary' }));
    expect(state.projection.commands.map((item) => item.id)).toEqual([
      'command-3', 'command-2', 'command-1',
    ]);
    expect(state.beforeCursor).toBeNull();

    const rebuilt = page(4, { hasMore: true, cursor: 'rebuilt-boundary' });
    rebuilt.graph_fingerprint = 'graph-b';
    state = refreshNewestWorkPage(state, rebuilt);
    expect(state.projection.commands.map((item) => item.id)).toEqual(['command-4']);
    expect(state.beforeCursor).toBe('rebuilt-boundary');
  });

  it('removes a running command when the newest refresh no longer projects it', () => {
    const running = page(2);
    running.commands[0].status = 'running';
    let state = createAccumulatedWorkState(running);

    const rejected = page(3);
    rejected.commands = [];
    rejected.summary.commands = 0;
    rejected.removed_call_ids = ['call-2'];
    state = refreshNewestWorkPage(state, rejected);

    expect(state.projection.commands).toEqual([]);
  });

  it('keeps an absent running command when a partial refresh has no tombstone', () => {
    const running = page(2, { hasMore: true, cursor: 'older' });
    running.commands[0].status = 'running';
    let state = createAccumulatedWorkState(running);

    const partial = page(3, { hasMore: true, cursor: 'new-boundary' });
    state = refreshNewestWorkPage(state, partial);

    expect(state.projection.commands.map((item) => item.id)).toEqual([
      'command-3',
      'command-2',
    ]);
  });
  it('keeps every loaded item and rebuilds root candidates from retained mutations', () => {
    const newest = page(6_001, {
      hasMore: true,
      cursor: 'older',
      mutation: mutation('shared', {
        path: 'old/src/app.ts',
        pathId: 'old-root:src/app.ts',
        relativePath: 'src/app.ts',
        rootId: 'old-root',
        rootLabel: 'old',
      }),
    });
    newest.commands = Array.from({ length: 5_001 }, (_, index) => ({
      ...newest.commands[0],
      id: `command-${index}`,
      call_id: `call-${index}`,
      sort_key: String(index).padStart(6, '0'),
    }));
    let state = createAccumulatedWorkState(newest);
    expect(state.projection.commands).toHaveLength(5_001);

    const refreshed = page(6_002, {
      hasMore: true,
      cursor: 'fresh',
      mutation: mutation('shared', {
        path: 'new/src/app.ts',
        pathId: 'new-root:src/app.ts',
        relativePath: 'src/app.ts',
        rootId: 'new-root',
        rootLabel: 'new',
      }),
    });
    state = refreshNewestWorkPage(state, refreshed);
    expect(state.rootsByRelative['src/app.ts']).toEqual([
      { rootId: 'new-root', rootLabel: 'new' },
    ]);
  });
  it('restarts traversal from the latest refresh cursor without dropping loaded evidence', () => {
    const newest = page(3, { hasMore: true, cursor: 'expired' });
    let state = appendOlderWorkPage(
      createAccumulatedWorkState(newest),
      page(2, { hasMore: true, cursor: 'also-expired' }),
    );
    state = refreshNewestWorkPage(state, page(4, { hasMore: true, cursor: 'fresh' }));
    state = restartAccumulatedWorkTraversal(state);
    expect(state.beforeCursor).toBe('fresh');
    expect(state.projection.commands.map((item) => item.id)).toEqual([
      'command-4', 'command-3', 'command-2',
    ]);
  });
  it('resets page cursors when an empty descendant changes the graph fingerprint', () => {
    const initial = page(2, { hasMore: true, cursor: 'older' });
    initial.graph_fingerprint = 'graph-a';
    let state = createWorkPageState(initial);
    state = storeOlderWorkPage(state, 'older', page(1));
    const rebuilt = page(2, { hasMore: true, cursor: 'rebuilt-older' });
    rebuilt.graph_fingerprint = 'graph-b';
    rebuilt.workstreams = [{
      key: 'session:empty-child',
      kind: 'delegate',
      parent_key: 'session:root',
      root_key: 'session:root',
      edge_kind: 'delegate',
      ordinal: 1,
      session_id: 'empty-child',
      event_store_session_id: 'store-empty-child',
      title: 'New empty child',
      agent_id: 'worker',
      status: 'running',
      current: true,
      superseded: false,
    }];
    state = replaceNewestWorkPage(state, rebuilt);
    expect(state.currentIndex).toBe(0);
    expect(state.slots).toHaveLength(1);
    expect(currentWorkPage(state)?.graph_fingerprint).toBe('graph-b');
    expect(currentWorkPage(state)?.workstreams?.[0].title).toBe('New empty child');
  });
  it('traverses ten pages both ways after a live newest refresh without loss or duplicates', () => {
    const pages = Array.from({ length: 10 }, (_, index) => page(9 - index, {
      hasMore: index < 9,
      cursor: index < 9 ? `cursor-${index + 1}` : undefined,
    }));
    const byCursor = new Map(
      pages.slice(1).map((item, index) => [`cursor-${index + 1}`, item]),
    );
    let state = createWorkPageState(pages[0]);

    for (let index = 1; index < pages.length; index += 1) {
      state = storeOlderWorkPage(state, `cursor-${index}`, pages[index]);
    }
    expect(state.currentIndex).toBe(9);
    expect(state.slots).toHaveLength(10);
    expect(state.slots.filter((slot) => slot.page).length).toBeLessThanOrEqual(
      WORK_PAGE_CACHE_LIMIT,
    );
    expect(currentWorkPage(state)?.commands[0].id).toBe('command-0');

    const refreshed = page(10, { hasMore: true, cursor: 'cursor-1' });
    state = replaceNewestWorkPage(state, refreshed);
    expect(state.currentIndex).toBe(9);
    expect(state.newerEvidenceAvailable).toBe(true);
    expect(currentWorkPage(state)?.commands[0].id).toBe('command-0');

    const newerVisited: string[] = [];
    while (state.currentIndex > 0) {
      const target = state.currentIndex - 1;
      const slot = state.slots[target];
      if (!slot.page) {
        const reloaded = byCursor.get(slot.requestCursor ?? '');
        expect(reloaded).toBeTruthy();
        state = storeWorkPageAt(state, target, reloaded!);
      } else {
        state = moveToCachedWorkPage(state, target);
      }
      newerVisited.push(currentWorkPage(state)!.commands[0].id);
    }
    expect(newerVisited).toEqual([
      'command-1', 'command-2', 'command-3', 'command-4', 'command-5',
      'command-6', 'command-7', 'command-8', 'command-9',
    ]);
    expect(state.newerEvidenceAvailable).toBe(true);
    expect(currentWorkPage(state)?.commands[0].id).toBe('command-9');
    state = applyPendingNewestWorkPage(state);
    expect(currentWorkPage(state)?.commands[0].id).toBe('command-10');
    expect(state.newerEvidenceAvailable).toBe(false);

    const olderVisited: string[] = [];
    while (
      state.currentIndex < state.slots.length - 1
      || currentWorkPage(state)?.has_more_before
    ) {
      const target = state.currentIndex + 1;
      const slot = state.slots[target];
      if (!slot?.page) {
        const cursor = slot?.requestCursor ?? currentWorkPage(state)?.before_cursor ?? '';
        const reloaded = byCursor.get(cursor);
        expect(reloaded).toBeTruthy();
        state = slot
          ? storeWorkPageAt(state, target, reloaded!)
          : storeOlderWorkPage(state, cursor, reloaded!);
      } else {
        state = moveToCachedWorkPage(state, target);
      }
      olderVisited.push(currentWorkPage(state)!.commands[0].id);
    }
    expect(olderVisited).toEqual([
      'command-8', 'command-7', 'command-6', 'command-5', 'command-4',
      'command-3', 'command-2', 'command-1', 'command-0',
    ]);
    expect(new Set([...newerVisited, ...olderVisited])).toEqual(
      new Set(Array.from({ length: 10 }, (_, index) => `command-${index}`)),
    );
  });

  it.each(['rooted-first', 'unbound-first'])(
    'binds one unbound relative identity when exactly one rooted candidate exists: %s',
    (order) => {
      const rooted = page(2, {
        mutation: mutation('rooted', {
          path: 'repo/src/app.py',
          pathId: 'root-a:src/app.py',
          relativePath: 'src/app.py',
          rootId: 'root-a',
          rootLabel: 'repo',
        }),
      });
      const unbound = page(1, {
        mutation: mutation('unbound', {
          path: 'Unscoped/src/app.py',
          pathId: 'unbound:app',
          relativePath: 'src/app.py',
        }),
      });
      const first = order === 'rooted-first' ? rooted : unbound;
      const second = order === 'rooted-first' ? unbound : rooted;
      let state = createWorkPageState(first);
      state = storeOlderWorkPage(state, 'older', second);

      const resolved = resolvedCurrentWorkPage(state)!;
      expect(resolved.mutations[0].file_diffs[0]).toMatchObject({
        path: 'repo/src/app.py',
        path_id: 'root-a:src/app.py',
        relative_path: 'src/app.py',
        root_id: 'root-a',
        root_label: 'repo',
      });
    },
  );

  it.each(['rooted-first', 'unbound-first'])(
    'preserves the server changed-file total after collapsing same-page aliases: %s',
    (order) => {
      const rooted = mutation('rooted', {
        path: 'repo/src/app.py',
        pathId: 'root-a:src/app.py',
        relativePath: 'src/app.py',
        rootId: 'root-a',
        rootLabel: 'repo',
      });
      const unbound = mutation('unbound', {
        path: 'Unscoped/src/app.py',
        pathId: 'unbound:app',
        relativePath: 'src/app.py',
      });
      const projection = page(1, { mutation: order === 'rooted-first' ? rooted : unbound });
      projection.mutations.push(order === 'rooted-first' ? unbound : rooted);
      projection.summary.changed_files = 2;

      const resolved = resolvedCurrentWorkPage(createWorkPageState(projection))!;
      expect(resolved.summary.changed_files).toBe(2);
      expect(new Set(
        resolved.mutations.flatMap((event) => event.file_diffs.map((diff) => diff.path_id)),
      )).toEqual(new Set(['root-a:src/app.py']));
    },
  );

  it('preserves the server total when aliases have partially omitted file statistics', () => {
    const rooted = mutation('rooted', {
      path: 'repo/src/app.py',
      pathId: 'root-a:src/app.py',
      relativePath: 'src/app.py',
      rootId: 'root-a',
      rootLabel: 'repo',
    });
    const unbound = mutation('unbound', {
      path: 'Unscoped/src/app.py',
      pathId: 'unbound:app',
      relativePath: 'src/app.py',
    });
    unbound.file_stats = [];
    unbound.omitted_file_stat_count = 1;
    const projection = page(1, { mutation: rooted });
    projection.mutations.push(unbound);
    projection.summary.changed_files = 2;

    const resolved = resolvedCurrentWorkPage(createWorkPageState(projection))!;
    expect(resolved.summary.changed_files).toBe(2);
    expect(new Set(
      resolved.mutations.flatMap((event) => event.file_diffs.map((diff) => diff.path_id)),
    )).toEqual(new Set(['root-a:src/app.py']));
  });

  it('keeps an unbound identity separate and visibly unscoped when two roots match', () => {
    const rootedA = page(3, {
      mutation: mutation('root-a', {
        path: 'repo/src/app.py',
        pathId: 'root-a:src/app.py',
        relativePath: 'src/app.py',
        rootId: 'root-a',
        rootLabel: 'repo',
      }),
    });
    const rootedB = page(2, {
      mutation: mutation('root-b', {
        path: 'repo/src/app.py',
        pathId: 'root-b:src/app.py',
        relativePath: 'src/app.py',
        rootId: 'root-b',
        rootLabel: 'repo',
      }),
    });
    const unbound = page(1, {
      mutation: mutation('unbound', {
        path: 'Unscoped/src/app.py',
        pathId: 'unbound:app',
        relativePath: 'src/app.py',
      }),
    });
    let state = createWorkPageState(rootedA);
    state = storeOlderWorkPage(state, 'root-b', rootedB);
    state = storeOlderWorkPage(state, 'unbound', unbound);

    const resolved = resolvedCurrentWorkPage(state)!;
    expect(resolved.mutations[0].file_diffs[0]).toMatchObject({
      path: 'Unscoped/src/app.py',
      path_id: 'unbound:app',
      root_id: null,
      root_label: 'Unscoped',
    });
  });

  it('windows deliverables within one bounded server page', () => {
    const items = Array.from({ length: 25 }, (_, index) => index);
    expect(workWindow(items, 0, 10).items).toEqual(items.slice(15));
    expect(workWindow(items, 1, 10).items).toEqual(items.slice(5, 15));
    expect(workWindow(items, 2, 10).items).toEqual(items.slice(0, 5));
  });
});

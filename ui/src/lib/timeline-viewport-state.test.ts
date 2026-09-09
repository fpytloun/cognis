import { describe, expect, it } from 'vitest';

import { TimelineViewportStateStore } from './timeline-viewport-state';

describe('TimelineViewportStateStore', () => {
  it('defaults an unvisited scope to following the tail', () => {
    const store = new TimelineViewportStateStore();

    expect(store.begin('conversation:b', 1).state).toEqual({
      mode: 'following',
      anchor: null,
      scrollTop: 0,
    });
  });

  it('restores a paused scope with its exact row anchor', () => {
    const store = new TimelineViewportStateStore();
    store.save('conversation:a', {
      mode: 'paused',
      anchor: { rowKey: 'message:a-20', offsetTop: 17, scrollTop: 420 },
      scrollTop: 420,
    });

    store.begin('conversation:b', 1);
    expect(store.begin('conversation:a', 2).state).toEqual({
      mode: 'paused',
      anchor: { rowKey: 'message:a-20', offsetTop: 17, scrollTop: 420 },
      scrollTop: 420,
    });
  });

  it('keeps a previously following scope at the current tail', () => {
    const store = new TimelineViewportStateStore();
    store.save('conversation:a', { mode: 'following', anchor: null, scrollTop: 0 });

    expect(store.begin('conversation:a', 4).state.mode).toBe('following');
  });

  it('rejects settlement from an obsolete rapid-switch generation', () => {
    const store = new TimelineViewportStateStore();
    store.begin('conversation:b', 2);
    store.begin('conversation:c', 3);

    expect(store.settle('conversation:b', 2)).toBe(false);
    expect(store.isCurrent('conversation:c', 3)).toBe(true);
    expect(store.settle('conversation:c', 3)).toBe(true);
  });

  it('bounds saved viewport state with least-recently-used eviction', () => {
    const store = new TimelineViewportStateStore(2);
    store.save('conversation:a', { mode: 'paused', anchor: null, scrollTop: 10 });
    store.save('conversation:b', { mode: 'paused', anchor: null, scrollTop: 20 });
    store.begin('conversation:a', 1);
    store.save('conversation:c', { mode: 'paused', anchor: null, scrollTop: 30 });

    expect(store.begin('conversation:a', 2).state.mode).toBe('paused');
    expect(store.begin('conversation:b', 3).state.mode).toBe('following');
  });
});

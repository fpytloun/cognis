import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { clearPersistedScroll, scrollPersist } from './scrollPersist';

describe('scrollPersist', () => {
  const rafCallbacks: FrameRequestCallback[] = [];
  let originalRaf: typeof window.requestAnimationFrame;
  let originalCancel: typeof window.cancelAnimationFrame;

  beforeEach(() => {
    sessionStorage.clear();
    rafCallbacks.length = 0;
    originalRaf = window.requestAnimationFrame;
    originalCancel = window.cancelAnimationFrame;
    window.requestAnimationFrame = (cb: FrameRequestCallback) => {
      rafCallbacks.push(cb);
      return rafCallbacks.length;
    };
    window.cancelAnimationFrame = vi.fn();
  });

  afterEach(() => {
    window.requestAnimationFrame = originalRaf;
    window.cancelAnimationFrame = originalCancel;
    sessionStorage.clear();
  });

  function flushRaf(): void {
    const snapshot = rafCallbacks.splice(0, rafCallbacks.length);
    for (const cb of snapshot) cb(0);
  }

  function makeNode(initialScrollTop = 0): HTMLElement {
    const node = document.createElement('div');
    let value = initialScrollTop;
    Object.defineProperty(node, 'scrollTop', {
      configurable: true,
      get: () => value,
      set: (v: number) => {
        value = v;
      }
    });
    return node;
  }

  it('restores a stored scrollTop on mount', () => {
    sessionStorage.setItem('cognis-scroll:/tasks', '420');
    const node = makeNode();

    scrollPersist(node, { key: '/tasks' });
    flushRaf();

    expect(node.scrollTop).toBe(420);
  });

  it('persists scrollTop updates via the scroll listener', () => {
    const node = makeNode();
    scrollPersist(node, { key: '/tasks' });

    node.scrollTop = 180;
    node.dispatchEvent(new Event('scroll'));
    flushRaf();

    expect(sessionStorage.getItem('cognis-scroll:/tasks')).toBe('180');
  });

  it('saves the old key and restores the new key when the param changes', () => {
    const node = makeNode();
    const action = scrollPersist(node, { key: '/tasks' });

    // Tasks page: user scrolls to 150.
    node.scrollTop = 150;
    node.dispatchEvent(new Event('scroll'));
    flushRaf();
    expect(sessionStorage.getItem('cognis-scroll:/tasks')).toBe('150');

    // Prepare a known scroll for /agents.
    sessionStorage.setItem('cognis-scroll:/agents', '77');

    // Navigate to /agents via update().
    action.update?.({ key: '/agents' });
    flushRaf();

    expect(sessionStorage.getItem('cognis-scroll:/tasks')).toBe('150');
    expect(node.scrollTop).toBe(77);

    action.destroy?.();
  });

  it('clearPersistedScroll removes the stored key', () => {
    sessionStorage.setItem('cognis-scroll:/tasks', '100');
    clearPersistedScroll('/tasks');
    expect(sessionStorage.getItem('cognis-scroll:/tasks')).toBeNull();
  });
});

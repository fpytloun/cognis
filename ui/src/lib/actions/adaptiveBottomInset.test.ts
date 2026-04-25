import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { adaptiveBottomInset } from './adaptiveBottomInset';

describe('adaptiveBottomInset', () => {
  const rafCallbacks: FrameRequestCallback[] = [];
  let originalRaf: typeof window.requestAnimationFrame;
  let originalCancel: typeof window.cancelAnimationFrame;

  beforeEach(() => {
    rafCallbacks.length = 0;
    originalRaf = window.requestAnimationFrame;
    originalCancel = window.cancelAnimationFrame;
    window.requestAnimationFrame = (cb: FrameRequestCallback) => {
      rafCallbacks.push(cb);
      return rafCallbacks.length;
    };
    window.cancelAnimationFrame = vi.fn();
    document.documentElement.style.setProperty('--app-shell-bottom-offset', '40px');
  });

  afterEach(() => {
    window.requestAnimationFrame = originalRaf;
    window.cancelAnimationFrame = originalCancel;
    document.documentElement.style.removeProperty('--app-shell-bottom-offset');
  });

  function flushRaf(): void {
    const snapshot = rafCallbacks.splice(0, rafCallbacks.length);
    for (const cb of snapshot) cb(0);
  }

  function makeNode(clientHeight: number, scrollHeight: number): HTMLElement {
    const node = document.createElement('div');
    Object.defineProperty(node, 'clientHeight', { value: clientHeight, configurable: true });
    Object.defineProperty(node, 'scrollHeight', { value: scrollHeight, configurable: true });
    document.body.appendChild(node);
    return node;
  }

  it('adds bottom padding only when content would sit under the overlay', () => {
    const node = makeNode(400, 390);
    const action = adaptiveBottomInset(node);
    flushRaf();

    expect(node.style.paddingBottom).toBe('40px');

    action.destroy?.();
    document.body.removeChild(node);
  });

  it('does not add padding when content ends above the overlay', () => {
    const node = makeNode(400, 320);
    const action = adaptiveBottomInset(node);
    flushRaf();

    expect(node.style.paddingBottom).toBe('');

    action.destroy?.();
    document.body.removeChild(node);
  });

  it('removes padding when disabled', () => {
    const node = makeNode(400, 390);
    const action = adaptiveBottomInset(node);
    flushRaf();
    expect(node.style.paddingBottom).toBe('40px');

    action.update?.({ disabled: true });
    flushRaf();

    expect(node.style.paddingBottom).toBe('');

    action.destroy?.();
    document.body.removeChild(node);
  });
});

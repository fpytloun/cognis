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

  it('reserves the app-owned overlay', () => {
    const node = makeNode(400, 390);
    const action = adaptiveBottomInset(node);
    flushRaf();

    expect(node.style.paddingBottom).toBe('40px');

    action.destroy?.();
    document.body.removeChild(node);
  });

  it('keeps usable viewport bounds independent of short content', () => {
    const node = makeNode(400, 320);
    const action = adaptiveBottomInset(node);
    flushRaf();

    expect(node.style.paddingBottom).toBe('40px');

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

  it('converges when scrollHeight is floored by the viewport instead of overflowing', async () => {
    const node = makeNode(400, 400);
    Object.defineProperty(node, 'scrollHeight', { get: () => Math.max(400, 300 + (parseFloat(node.style.paddingBottom) || 0)) });
    const action = adaptiveBottomInset(node);
    const padding: string[] = [];
    for (let frame = 0; frame < 8; frame++) {
      await Promise.resolve(); flushRaf(); padding.push(node.style.paddingBottom);
    }
    expect(padding).toEqual(Array(8).fill('40px'));
    action.destroy(); node.remove();
  });
});

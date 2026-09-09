import { afterEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';

import { keyboardAvoidance, keyboardOcclusionForRect } from './keyboard-avoidance';

describe('keyboardOcclusionForRect', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = '';
  });

  it('reserves only the part of a chat surface covered by the keyboard', () => {
    expect(keyboardOcclusionForRect(
      { top: 100, bottom: 874 },
      { visibleBottom: 536, keyboardOpen: true },
    )).toBe(338);
    expect(keyboardOcclusionForRect(
      { top: 100, bottom: 620 },
      { visibleBottom: 536, keyboardOpen: true },
    )).toBe(84);
  });

  it('does not move a chat surface that ends above the keyboard', () => {
    expect(keyboardOcclusionForRect(
      { top: 100, bottom: 500 },
      { visibleBottom: 536, keyboardOpen: true },
    )).toBe(0);
  });

  it('returns zero while the keyboard is closed', () => {
    expect(keyboardOcclusionForRect(
      { top: 100, bottom: 874 },
      { visibleBottom: 874, keyboardOpen: false },
    )).toBe(0);
  });

  it('clamps occlusion to the chat surface height', () => {
    expect(keyboardOcclusionForRect(
      { top: 400, bottom: 500 },
      { visibleBottom: 0, keyboardOpen: true },
    )).toBe(100);
  });

  it('tracks position changes while open and removes local state on destroy', () => {
    let nextFrameId = 0;
    const frames = new Map<number, FrameRequestCallback>();
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      const id = ++nextFrameId;
      frames.set(id, callback);
      return id;
    });
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
      frames.delete(id);
    });
    const runNextFrame = () => {
      const entry = frames.entries().next().value as [number, FrameRequestCallback] | undefined;
      expect(entry).toBeDefined();
      if (!entry) return;
      frames.delete(entry[0]);
      entry[1](0);
    };

    let rect = { top: 100, bottom: 874 };
    const node = document.createElement('section');
    node.getBoundingClientRect = () => rect as DOMRect;
    document.body.append(node);
    const metrics = writable({ height: 536, keyboardOpen: true });
    const action = keyboardAvoidance(node, metrics);

    runNextFrame();
    expect(node.style.getPropertyValue('--app-local-keyboard-occlusion')).toBe('338px');

    rect = { top: 100, bottom: 620 };
    runNextFrame();
    expect(node.style.getPropertyValue('--app-local-keyboard-occlusion')).toBe('84px');

    action.destroy();
    expect(node.style.getPropertyValue('--app-local-keyboard-occlusion')).toBe('');
    expect(frames.size).toBe(0);
  });

  it('uses current visual viewport geometry while the root keyboard state is open', () => {
    let frame: FrameRequestCallback | null = null;
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frame = callback;
      return 1;
    });
    vi.stubGlobal('cancelAnimationFrame', () => {
      frame = null;
    });
    Object.defineProperty(window, 'visualViewport', {
      configurable: true,
      value: {
        height: 520,
        offsetTop: 0,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      },
    });
    document.documentElement.dataset.keyboard = 'open';
    const node = document.createElement('section');
    node.getBoundingClientRect = () => ({ top: 138, bottom: 843 }) as DOMRect;
    document.body.append(node);
    const metrics = writable({ height: 843, keyboardOpen: false });
    const action = keyboardAvoidance(node, metrics);

    const runFrame = () => {
      const scheduled = frame;
      if (!scheduled) throw new Error('Keyboard avoidance frame was not scheduled');
      scheduled(0);
    };
    runFrame();
    expect(node.style.getPropertyValue('--app-local-keyboard-occlusion')).toBe('323px');

    action.destroy();
    delete document.documentElement.dataset.keyboard;
  });
});

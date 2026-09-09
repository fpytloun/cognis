import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { registerOverlay, resetOverlayState } from './overlays';

describe('overlay scroll ownership', () => {
  let scroller: HTMLDivElement;

  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    scroller = document.createElement('div');
    scroller.dataset.appContent = 'true';
    scroller.style.overflowY = 'auto';
    scroller.style.overscrollBehavior = 'contain';
    scroller.scrollTop = 137;
    scroller.scrollLeft = 9;
    document.body.appendChild(scroller);
  });

  afterEach(() => {
    resetOverlayState();
    scroller.remove();
    vi.unstubAllGlobals();
  });

  it('locks the real app scroller and restores exact styles and position', () => {
    const overlay = registerOverlay({ kind: 'blocking', blocksChrome: true });
    expect(scroller.dataset.overlayScrollLocked).toBe('true');
    expect(scroller.style.overflow).toBe('hidden');
    expect(scroller.style.overscrollBehavior).toBe('none');

    scroller.scrollTop = 0;
    overlay.unregister();
    expect(scroller.dataset.overlayScrollLocked).toBeUndefined();
    expect(scroller.style.overflow).toBe('');
    expect(scroller.style.overflowY).toBe('auto');
    expect(scroller.style.overscrollBehavior).toBe('contain');
    expect(scroller.scrollTop).toBe(137);
    expect(scroller.scrollLeft).toBe(9);
  });

  it('keeps the first lock until the final nested overlay closes', () => {
    const first = registerOverlay({ kind: 'blocking', blocksChrome: true });
    const second = registerOverlay({ kind: 'blocking', blocksChrome: true });
    scroller.scrollTop = 0;
    first.unregister();
    expect(scroller.dataset.overlayScrollLocked).toBe('true');
    expect(scroller.style.overflow).toBe('hidden');
    second.unregister();
    expect(scroller.dataset.overlayScrollLocked).toBeUndefined();
    expect(scroller.scrollTop).toBe(137);
  });
});

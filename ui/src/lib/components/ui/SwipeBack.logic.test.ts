import { describe, expect, it } from 'vitest';

/**
 * Logic-level tests for the SwipeBack primitive. We re-create the pure
 * helper here to exercise the branching without spinning up the Svelte
 * component (which would require a full DOM event simulation pass).
 * Keep in sync with `SwipeBack.svelte:hasHorizontalScrollAncestor` and
 * the gesture threshold logic.
 */

function hasHorizontalScrollAncestor(target: Element | null): boolean {
  let el: Element | null = target;
  while (el) {
    if (el.scrollWidth > el.clientWidth + 1) {
      const overflowX = (el as HTMLElement).dataset.overflowX;
      if (overflowX === 'auto' || overflowX === 'scroll') return true;
    }
    el = el.parentElement;
  }
  return false;
}

function makeScrollableEl(overflow: string, scrollW: number, clientW: number): HTMLElement {
  const el = document.createElement('div');
  el.dataset.overflowX = overflow;
  Object.defineProperty(el, 'scrollWidth', { configurable: true, value: scrollW });
  Object.defineProperty(el, 'clientWidth', { configurable: true, value: clientW });
  return el;
}

describe('SwipeBack.hasHorizontalScrollAncestor (logic)', () => {
  it('returns false for a non-scrolling element', () => {
    const el = makeScrollableEl('visible', 100, 100);
    expect(hasHorizontalScrollAncestor(el)).toBe(false);
  });

  it('detects overflow-x:auto with scroll room', () => {
    const el = makeScrollableEl('auto', 500, 300);
    expect(hasHorizontalScrollAncestor(el)).toBe(true);
  });

  it('detects overflow-x:scroll with scroll room', () => {
    const el = makeScrollableEl('scroll', 500, 300);
    expect(hasHorizontalScrollAncestor(el)).toBe(true);
  });

  it('ignores <=1px float rounding noise', () => {
    // 1px or less difference is treated as "not scrollable" to avoid
    // triggering back-swipe on near-exact boxes.
    const snug = makeScrollableEl('auto', 301, 300);
    expect(hasHorizontalScrollAncestor(snug)).toBe(false);
    // 2px or more IS scrollable.
    const el = makeScrollableEl('auto', 302, 300);
    expect(hasHorizontalScrollAncestor(el)).toBe(true);
  });

  it('walks up ancestors to find a scroller', () => {
    const outer = makeScrollableEl('auto', 800, 600);
    const inner = document.createElement('span');
    outer.appendChild(inner);
    expect(hasHorizontalScrollAncestor(inner)).toBe(true);
  });
});

describe('SwipeBack gesture math', () => {
  const edgeWidth = 24;
  const threshold = 80;

  function shouldTriggerBack(startX: number, moveX: number, moveY: number): boolean {
    if (startX > edgeWidth) return false;
    const dx = moveX - startX;
    const dy = moveY;
    if (Math.abs(dy) > Math.abs(dx)) return false;
    return dx >= threshold;
  }

  it('does not trigger when drag starts outside the edge', () => {
    expect(shouldTriggerBack(60, 200, 0)).toBe(false);
  });

  it('triggers for horizontal drag past threshold', () => {
    expect(shouldTriggerBack(5, 100, 0)).toBe(true);
  });

  it('does not trigger when drag is mostly vertical', () => {
    expect(shouldTriggerBack(5, 50, 200)).toBe(false);
  });

  it('does not trigger below threshold', () => {
    expect(shouldTriggerBack(5, 60, 0)).toBe(false);
  });
});

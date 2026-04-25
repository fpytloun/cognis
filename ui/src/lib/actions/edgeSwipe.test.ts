import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { edgeSwipe, type EdgeSwipeParam } from './edgeSwipe';

/**
 * The action listens to native `touchstart`/`touchmove` and falls back
 * to pointer events for mouse/pen. Tests cover the touch path because
 * that is the primary user-facing surface; the pointer path mirrors
 * the same shape.
 */

interface FakeTouchInit {
  identifier: number;
  clientX: number;
  clientY: number;
  target: EventTarget;
}

function fakeTouch(init: FakeTouchInit): Touch {
  return {
    identifier: init.identifier,
    clientX: init.clientX,
    clientY: init.clientY,
    pageX: init.clientX,
    pageY: init.clientY,
    screenX: init.clientX,
    screenY: init.clientY,
    target: init.target,
    radiusX: 1,
    radiusY: 1,
    rotationAngle: 0,
    force: 1,
    altitudeAngle: 0,
    azimuthAngle: 0,
    touchType: 'direct'
  } as unknown as Touch;
}

function dispatchTouch(node: HTMLElement, type: string, touches: Touch[]): { defaultPrevented: boolean } {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'touches', {
    value: touches,
    configurable: true,
    enumerable: true
  });
  Object.defineProperty(event, 'targetTouches', {
    value: touches,
    configurable: true,
    enumerable: true
  });
  Object.defineProperty(event, 'changedTouches', {
    value: touches,
    configurable: true,
    enumerable: true
  });
  node.dispatchEvent(event);
  return { defaultPrevented: event.defaultPrevented };
}

describe('edgeSwipe action', () => {
  let host: HTMLElement;
  const originalInnerWidth = window.innerWidth;

  beforeEach(() => {
    host = document.createElement('div');
    document.body.appendChild(host);
    Object.defineProperty(window, 'innerWidth', {
      value: 400,
      writable: true,
      configurable: true
    });
  });

  afterEach(() => {
    document.body.removeChild(host);
    Object.defineProperty(window, 'innerWidth', {
      value: originalInnerWidth,
      writable: true,
      configurable: true
    });
  });

  function mount(param: EdgeSwipeParam) {
    return edgeSwipe(host, param);
  }

  it('triggers a left-edge swipe once the threshold is crossed', () => {
    const onTrigger = vi.fn();
    const action = mount({ edge: 'left', onTrigger, edgeWidth: 24, threshold: 60 });

    dispatchTouch(host, 'touchstart', [fakeTouch({ identifier: 1, clientX: 10, clientY: 100, target: host })]);
    const move = dispatchTouch(host, 'touchmove', [fakeTouch({ identifier: 1, clientX: 80, clientY: 100, target: host })]);

    expect(onTrigger).toHaveBeenCalledTimes(1);
    // Crossing the threshold counts as a horizontal claim, so the
    // event should have been preventDefault'd.
    expect(move.defaultPrevented).toBe(true);

    action.destroy?.();
  });

  it('triggers a right-edge swipe when dragging leftward from the right edge', () => {
    const onTrigger = vi.fn();
    const action = mount({ edge: 'right', onTrigger, edgeWidth: 24, threshold: 60 });

    // viewportWidth = 400; right edge band is x >= 376.
    dispatchTouch(host, 'touchstart', [fakeTouch({ identifier: 7, clientX: 390, clientY: 200, target: host })]);
    dispatchTouch(host, 'touchmove', [fakeTouch({ identifier: 7, clientX: 320, clientY: 200, target: host })]);

    expect(onTrigger).toHaveBeenCalledTimes(1);
    action.destroy?.();
  });

  it('ignores touchstart outside the edge zone', () => {
    const onTrigger = vi.fn();
    const action = mount({ edge: 'left', onTrigger });

    dispatchTouch(host, 'touchstart', [fakeTouch({ identifier: 1, clientX: 100, clientY: 100, target: host })]);
    dispatchTouch(host, 'touchmove', [fakeTouch({ identifier: 1, clientX: 200, clientY: 100, target: host })]);

    expect(onTrigger).not.toHaveBeenCalled();
    action.destroy?.();
  });

  it('aborts on a vertical-dominant gesture without preventing default', () => {
    const onTrigger = vi.fn();
    const action = mount({ edge: 'left', onTrigger });

    dispatchTouch(host, 'touchstart', [fakeTouch({ identifier: 2, clientX: 8, clientY: 100, target: host })]);
    const move = dispatchTouch(host, 'touchmove', [fakeTouch({ identifier: 2, clientX: 12, clientY: 200, target: host })]);

    expect(onTrigger).not.toHaveBeenCalled();
    expect(move.defaultPrevented).toBe(false);
    action.destroy?.();
  });

  it('does not trigger when disabled', () => {
    const onTrigger = vi.fn();
    const action = mount({ edge: 'left', onTrigger, disabled: true });

    dispatchTouch(host, 'touchstart', [fakeTouch({ identifier: 1, clientX: 8, clientY: 100, target: host })]);
    dispatchTouch(host, 'touchmove', [fakeTouch({ identifier: 1, clientX: 200, clientY: 100, target: host })]);

    expect(onTrigger).not.toHaveBeenCalled();

    // Re-enabling via update() should make subsequent gestures fire.
    action.update?.({ edge: 'left', onTrigger, disabled: false });
    dispatchTouch(host, 'touchstart', [fakeTouch({ identifier: 2, clientX: 8, clientY: 100, target: host })]);
    dispatchTouch(host, 'touchmove', [fakeTouch({ identifier: 2, clientX: 200, clientY: 100, target: host })]);
    expect(onTrigger).toHaveBeenCalledTimes(1);

    action.destroy?.();
  });

  it('aborts when the touch starts inside a horizontally scrollable ancestor', () => {
    const scroller = document.createElement('div');
    Object.defineProperty(scroller, 'scrollWidth', { value: 1000, configurable: true });
    Object.defineProperty(scroller, 'clientWidth', { value: 200, configurable: true });
    host.appendChild(scroller);

    // jsdom's `getComputedStyle` does not always reflect inline
    // `style.overflowX`, so stub it for the scroller element so the
    // ancestor scan reliably sees `auto`.
    const originalGetComputedStyle = window.getComputedStyle;
    window.getComputedStyle = ((el: Element) => {
      if (el === scroller) {
        return { overflowX: 'auto' } as unknown as CSSStyleDeclaration;
      }
      return originalGetComputedStyle(el);
    }) as typeof window.getComputedStyle;

    const onTrigger = vi.fn();
    const action = mount({ edge: 'left', onTrigger });

    try {
      // Dispatch on the scroller so the event bubbles up to the host
      // listener with `event.target === scroller`. Bubbling matters
      // here because the action listens on the host but inspects the
      // initial target's ancestors.
      dispatchTouch(scroller, 'touchstart', [fakeTouch({ identifier: 1, clientX: 8, clientY: 100, target: scroller })]);
      dispatchTouch(scroller, 'touchmove', [fakeTouch({ identifier: 1, clientX: 200, clientY: 100, target: scroller })]);

      expect(onTrigger).not.toHaveBeenCalled();
    } finally {
      window.getComputedStyle = originalGetComputedStyle;
      action.destroy?.();
    }
  });

  it('cleans up listeners on destroy', () => {
    const onTrigger = vi.fn();
    const action = mount({ edge: 'left', onTrigger });
    action.destroy?.();

    dispatchTouch(host, 'touchstart', [fakeTouch({ identifier: 1, clientX: 8, clientY: 100, target: host })]);
    dispatchTouch(host, 'touchmove', [fakeTouch({ identifier: 1, clientX: 200, clientY: 100, target: host })]);

    expect(onTrigger).not.toHaveBeenCalled();
  });
});

/**
 * `use:edgeSwipe` — open / close UI when the user swipes from a screen
 * edge, and suppress the iOS PWA system back/forward gesture along the
 * way.
 *
 * Why touch events instead of pointer events:
 *
 *   The browser issues both pointer events and touch events for a single
 *   finger gesture on iOS. Pointer events are downstream of touch
 *   events; calling `preventDefault()` on a `pointermove` is too late to
 *   stop iOS from claiming the gesture for its native back/forward
 *   swipe. We therefore listen on `touchstart` / `touchmove` directly
 *   with `{ passive: false }` and call `preventDefault()` once we
 *   detect a horizontally-dominant drag inside the edge zone. Combined
 *   with `touch-action: pan-y` on the shell (see `app.css`), iOS yields
 *   the gesture to us in PWA standalone mode. Mouse and pointer / pen
 *   support is handled in parallel via pointer events so the gesture
 *   keeps working in a desktop browser too.
 */

export type EdgeSwipeEdge = 'left' | 'right';

export interface EdgeSwipeParam {
  /** Which edge the swipe must originate from. */
  edge: EdgeSwipeEdge;
  /** Called once when the swipe crosses `threshold`. */
  onTrigger: () => void;
  /**
   * Detection zone width from the edge in CSS px. Default 48.
   * On iOS PWA standalone the system reserves the first ~20-30 px
   * around the bezel for its native back-swipe gesture and only starts
   * delivering JS touchstart events after the finger has moved past
   * that zone, so 48 px gives us a reliable practical detection
   * window of ~20-30 px once iOS yields the gesture.
   */
  edgeWidth?: number;
  /** Horizontal-drag threshold in CSS px. Default 60. */
  threshold?: number;
  /**
   * Abort when the touchstart target is inside an element that scrolls
   * horizontally. Prevents hijacking gestures that belong to a code
   * block, wide table, or carousel. Default `true`.
   */
  respectHScroll?: boolean;
  /**
   * Skip the action entirely. Useful for "disable on desktop" or
   * "disable while drawer is already open". Re-evaluated on every
   * pointer down so toggling this flag mid-flight is safe.
   */
  disabled?: boolean;
}

interface State {
  param: EdgeSwipeParam;
  /** Touch identifier currently being tracked, or `null`. */
  activeTouch: number | null;
  /** Pointer id currently being tracked (mouse / pen), or `null`. */
  activePointer: number | null;
  startX: number;
  startY: number;
  /** True once we've called preventDefault on the active gesture. */
  claimed: boolean;
}

function hasHorizontalScrollAncestor(target: EventTarget | null): boolean {
  let el = target instanceof Element ? target : null;
  while (el) {
    if (el.scrollWidth > el.clientWidth + 1) {
      const style = window.getComputedStyle(el);
      const overflowX = style.overflowX;
      if (overflowX === 'auto' || overflowX === 'scroll') return true;
    }
    el = el.parentElement;
  }
  return false;
}

function isInsideEdge(edge: EdgeSwipeEdge, x: number, edgeWidth: number, viewportWidth: number): boolean {
  if (edge === 'left') return x <= edgeWidth;
  return x >= viewportWidth - edgeWidth;
}

function deltaInDirection(edge: EdgeSwipeEdge, dx: number): number {
  // Left-edge: positive dx = swipe right. Right-edge: positive |dx| in
  // the negative direction = swipe left. Normalise so the trigger
  // condition is always `delta >= threshold`.
  return edge === 'left' ? dx : -dx;
}

function reset(state: State): void {
  state.activeTouch = null;
  state.activePointer = null;
  state.startX = 0;
  state.startY = 0;
  state.claimed = false;
}

export function edgeSwipe(node: HTMLElement, initial: EdgeSwipeParam) {
  const state: State = {
    param: initial,
    activeTouch: null,
    activePointer: null,
    startX: 0,
    startY: 0,
    claimed: false
  };

  function param(): EdgeSwipeParam {
    return state.param;
  }
  function edgeWidth(): number {
    return param().edgeWidth ?? 48;
  }
  function threshold(): number {
    return param().threshold ?? 60;
  }
  function respectHScroll(): boolean {
    return param().respectHScroll !== false;
  }
  function disabled(): boolean {
    return param().disabled === true;
  }

  // -- touch (primary path, lets us preventDefault on iOS) -------------

  function onTouchStart(event: TouchEvent): void {
    // Always start fresh on touchstart so a missed touchend (which can
    // happen if iOS hijacks a gesture mid-flight) does not block all
    // subsequent edge swipes.
    reset(state);
    if (disabled()) return;
    if (event.touches.length !== 1) return;
    const touch = event.touches[0];
    const viewportWidth = window.innerWidth;
    if (!isInsideEdge(param().edge, touch.clientX, edgeWidth(), viewportWidth)) return;
    if (respectHScroll() && hasHorizontalScrollAncestor(event.target)) return;
    state.activeTouch = touch.identifier;
    state.startX = touch.clientX;
    state.startY = touch.clientY;
    state.claimed = false;
  }

  function findActiveTouch(event: TouchEvent): Touch | null {
    if (state.activeTouch === null) return null;
    for (let i = 0; i < event.touches.length; i += 1) {
      const t = event.touches[i];
      if (t.identifier === state.activeTouch) return t;
    }
    return null;
  }

  function onTouchMove(event: TouchEvent): void {
    if (disabled()) {
      reset(state);
      return;
    }
    const touch = findActiveTouch(event);
    if (!touch) return;
    const dx = touch.clientX - state.startX;
    const dy = touch.clientY - state.startY;
    if (!state.claimed && Math.abs(dy) > Math.abs(dx)) {
      // Vertical-dominant gesture → release back to the page so the
      // user can scroll naturally.
      reset(state);
      return;
    }
    // Horizontal-dominant: claim it. Calling preventDefault here stops
    // iOS from taking the gesture for its bezel back/forward swipe.
    if (event.cancelable) event.preventDefault();
    state.claimed = true;
    if (deltaInDirection(param().edge, dx) >= threshold()) {
      const trigger = param().onTrigger;
      reset(state);
      trigger();
    }
  }

  function onTouchEnd(): void {
    reset(state);
  }

  // -- pointer (mouse / pen on desktop) --------------------------------

  function onPointerDown(event: PointerEvent): void {
    if (event.pointerType === 'touch') return; // touch path owns this
    if (disabled()) return;
    if (state.activePointer !== null) return;
    const viewportWidth = window.innerWidth;
    if (!isInsideEdge(param().edge, event.clientX, edgeWidth(), viewportWidth)) return;
    if (respectHScroll() && hasHorizontalScrollAncestor(event.target)) return;
    state.activePointer = event.pointerId;
    state.startX = event.clientX;
    state.startY = event.clientY;
    state.claimed = false;
  }

  function onPointerMove(event: PointerEvent): void {
    if (event.pointerType === 'touch') return;
    if (state.activePointer !== event.pointerId) return;
    const dx = event.clientX - state.startX;
    const dy = event.clientY - state.startY;
    if (!state.claimed && Math.abs(dy) > Math.abs(dx)) {
      reset(state);
      return;
    }
    state.claimed = true;
    try {
      node.setPointerCapture(event.pointerId);
    } catch {
      // capture is best-effort; ignore failures (e.g. detached element)
    }
    if (deltaInDirection(param().edge, dx) >= threshold()) {
      const trigger = param().onTrigger;
      reset(state);
      trigger();
    }
  }

  function onPointerEnd(): void {
    reset(state);
  }

  // Touch listeners must be `{ passive: false }` so we can call
  // preventDefault() to suppress the iOS edge-swipe gesture. Pointer
  // listeners default to non-passive on Element targets.
  node.addEventListener('touchstart', onTouchStart, { passive: false });
  node.addEventListener('touchmove', onTouchMove, { passive: false });
  node.addEventListener('touchend', onTouchEnd, { passive: true });
  node.addEventListener('touchcancel', onTouchEnd, { passive: true });
  node.addEventListener('pointerdown', onPointerDown);
  node.addEventListener('pointermove', onPointerMove);
  node.addEventListener('pointerup', onPointerEnd);
  node.addEventListener('pointercancel', onPointerEnd);

  return {
    update(next: EdgeSwipeParam) {
      state.param = next;
      // If the action becomes disabled mid-gesture, drop tracking.
      if (next.disabled === true) reset(state);
    },
    destroy() {
      reset(state);
      node.removeEventListener('touchstart', onTouchStart);
      node.removeEventListener('touchmove', onTouchMove);
      node.removeEventListener('touchend', onTouchEnd);
      node.removeEventListener('touchcancel', onTouchEnd);
      node.removeEventListener('pointerdown', onPointerDown);
      node.removeEventListener('pointermove', onPointerMove);
      node.removeEventListener('pointerup', onPointerEnd);
      node.removeEventListener('pointercancel', onPointerEnd);
    }
  };
}

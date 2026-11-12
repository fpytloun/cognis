/**
 * Svelte action: keep an anchored popup (dropdown, menu, popover) inside the
 * visible viewport horizontally.
 *
 * Popups are positioned relative to their trigger (for example `right: 0`
 * under a toolbar button). When the trigger is not at the edge of a narrow
 * mobile screen, the popup can extend past the left or right edge. This
 * measures the rendered popup and shifts it back with the CSS `translate`
 * property (which composes with any existing `transform`), and caps its width
 * to the viewport. It re-measures on resize and whenever the popup changes
 * size (for example when a section expands).
 *
 * Pass a changing parameter (such as an inline style string) when the caller
 * repositions the popup itself, so the clamp is recomputed afterwards.
 */
export interface KeepInViewportOptions {
  /** Minimum distance from the viewport edges, in pixels. */
  margin?: number;
  /** Flip an anchored menu above its trigger when there is more room there. */
  vertical?: boolean;
  preferredSide?: 'above' | 'below';
}

export function keepInViewport(
  node: HTMLElement,
  options: KeepInViewportOptions | unknown = {},
): { update(next?: KeepInViewportOptions | unknown): void; destroy(): void } {
  let margin = marginOf(options);
  let vertical = Boolean((options as KeepInViewportOptions)?.vertical);
  let preferredSide = (options as KeepInViewportOptions)?.preferredSide;
  let frame = 0;

  const place = () => {
    frame = 0;
    if (!node.isConnected) return;
    node.style.translate = '';
    if (vertical) node.style.maxHeight = '';
    const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
    const available = Math.max(0, viewportWidth - margin * 2);
    let rect = node.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    if (rect.width > available) {
      node.style.maxWidth = `${available}px`;
      rect = node.getBoundingClientRect();
    }
    let dx = 0;
    if (rect.left < margin) dx = margin - rect.left;
    else if (rect.right > viewportWidth - margin) dx = viewportWidth - margin - rect.right;
    let dy = 0;
    let scale = 1;
    if (vertical) {
      const viewport = window.visualViewport;
      let top = (viewport?.offsetTop ?? 0) + margin;
      let bottom = (viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight) - margin;
      for (let parent = node.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
        if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
        const bounds = parent.getBoundingClientRect();
        if (!bounds.height) continue;
        top = Math.max(top, bounds.top + margin);
        bottom = Math.min(bottom, bounds.bottom - margin);
      }
      const trigger = node.parentElement?.querySelector('summary');
      const anchor = trigger?.getBoundingClientRect();
      if (anchor) {
        if (anchor.bottom <= top || anchor.top >= bottom) {
          const details = trigger?.closest('details');
          if (details) details.open = false;
          return;
        }
        if (trigger?.offsetWidth) scale = anchor.width / trigger.offsetWidth || 1;
        const gap = 6;
        const above = Math.max(0, anchor.top - top - gap);
        const below = Math.max(0, bottom - anchor.bottom - gap);
        const preferAbove = preferredSide ? preferredSide === 'above' : rect.bottom <= anchor.top;
        const openAbove = preferAbove ? above >= rect.height || above >= below : below < rect.height && above > below;
        // Constrain to the selected side before placing. Clamping the whole
        // menu to viewport edges can detach it far from its visible trigger.
        node.style.boxSizing = 'border-box';
        node.style.maxHeight = `${(openAbove ? above : below) / scale}px`;
        node.style.overflowY = 'auto';
        rect = node.getBoundingClientRect();
        dy = openAbove ? anchor.top - gap - rect.bottom : anchor.bottom + gap - rect.top;
      } else {
        node.style.maxHeight = `${Math.max(0, bottom - top)}px`;
        node.style.overflowY = 'auto';
        dy = Math.max(0, top - rect.top) - Math.max(0, rect.bottom - bottom);
      }
    }
    if (dx !== 0 || dy !== 0) node.style.translate = `${dx / scale}px ${dy ? `${dy / scale}px` : '0'}`;
  };

  const schedule = () => {
    if (frame) return;
    frame = requestAnimationFrame(place);
  };

  place();
  window.addEventListener('resize', schedule);
  window.addEventListener('scroll', schedule, true);
  window.visualViewport?.addEventListener('resize', schedule);
  window.visualViewport?.addEventListener('scroll', schedule);
  const details = node.closest('details');
  details?.addEventListener('toggle', schedule);
  const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
  resizeObserver?.observe(node);

  return {
    update(next?: KeepInViewportOptions | unknown) {
      margin = marginOf(next);
      vertical = Boolean((next as KeepInViewportOptions)?.vertical);
      preferredSide = (next as KeepInViewportOptions)?.preferredSide;
      place();
    },
    destroy() {
      if (frame) cancelAnimationFrame(frame);
      window.removeEventListener('resize', schedule);
      window.removeEventListener('scroll', schedule, true);
      window.visualViewport?.removeEventListener('resize', schedule);
      window.visualViewport?.removeEventListener('scroll', schedule);
      details?.removeEventListener('toggle', schedule);
      resizeObserver?.disconnect();
    },
  };
}

function marginOf(options: unknown): number {
  if (options && typeof options === 'object' && 'margin' in options) {
    const value = (options as KeepInViewportOptions).margin;
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return 8;
}

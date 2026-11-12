/**
 * Svelte action for a `<details>` menu panel: while the menu is open, the panel is shown in the
 * browser top layer (`popover`) and placed next to its `<summary>` trigger with fixed
 * coordinates. Scrolling sidebars, overflow clipping and transformed ancestors therefore cannot
 * clip or squeeze it.
 *
 * Choosing a link, a dialog trigger, or an element marked `data-menu-close` closes the menu, so
 * a dialog opened from the menu is never covered by it.
 */
export interface AnchoredMenuOptions {
  preferredSide?: 'above' | 'below';
}

const MARGIN = 8;
const GAP = 6;

export function anchoredMenu(node: HTMLElement, options: AnchoredMenuOptions = {}) {
  const details = node.closest('details');
  const trigger = details?.querySelector('summary');
  if (!details || !trigger) return {};
  let preferredSide = options.preferredSide ?? 'below';
  const popover = typeof node.showPopover === 'function';
  if (popover) node.setAttribute('popover', 'manual');
  let frame = 0;

  function place() {
    frame = 0;
    if (!details!.open) return;
    const viewport = window.visualViewport;
    const top = (viewport?.offsetTop ?? 0) + MARGIN;
    const bottom = (viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight) - MARGIN;
    const width = document.documentElement.clientWidth || window.innerWidth;
    const anchor = trigger!.getBoundingClientRect();
    // The trigger scrolled out of view: close instead of leaving a detached panel.
    if (anchor.bottom < top || anchor.top > bottom) { details!.open = false; return; }
    Object.assign(node.style, {
      position: 'fixed', right: 'auto', bottom: 'auto', margin: '0', translate: '',
      maxWidth: `${width - MARGIN * 2}px`, maxHeight: '', overflowY: 'auto', boxSizing: 'border-box',
    });
    const rect = node.getBoundingClientRect();
    const above = Math.max(0, anchor.top - top - GAP);
    const below = Math.max(0, bottom - anchor.bottom - GAP);
    const openAbove = preferredSide === 'above'
      ? above >= rect.height || above > below
      : below < rect.height && above > below;
    const space = openAbove ? above : below;
    const height = Math.min(rect.height, space);
    node.style.maxHeight = `${space}px`;
    node.style.top = `${openAbove ? anchor.top - GAP - height : anchor.bottom + GAP}px`;
    const left = Math.min(anchor.right - rect.width, width - MARGIN - rect.width);
    node.style.left = `${Math.max(MARGIN, left)}px`;
  }
  const schedule = () => { if (!frame) frame = requestAnimationFrame(place); };
  function toggle() {
    if (details!.open) {
      if (popover && !node.matches(':popover-open')) node.showPopover();
      schedule();
    } else if (popover && node.matches(':popover-open')) {
      node.hidePopover();
    }
  }
  function choose(event: MouseEvent) {
    const target = event.target as Element | null;
    if (target?.closest('a[href], [aria-haspopup="dialog"], [data-menu-close]')) details!.open = false;
  }
  const resize = new ResizeObserver(schedule);
  resize.observe(node);
  details.addEventListener('toggle', toggle);
  node.addEventListener('click', choose);
  window.addEventListener('scroll', schedule, true);
  window.addEventListener('resize', schedule);
  window.visualViewport?.addEventListener('resize', schedule);
  toggle();
  return {
    update(next: AnchoredMenuOptions = {}) { preferredSide = next.preferredSide ?? 'below'; schedule(); },
    destroy() {
      cancelAnimationFrame(frame);
      resize.disconnect();
      details.removeEventListener('toggle', toggle);
      node.removeEventListener('click', choose);
      window.removeEventListener('scroll', schedule, true);
      window.removeEventListener('resize', schedule);
      window.visualViewport?.removeEventListener('resize', schedule);
      if (popover && node.matches(':popover-open')) node.hidePopover();
    },
  };
}

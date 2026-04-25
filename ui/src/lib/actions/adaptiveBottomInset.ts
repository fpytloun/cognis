/**
 * Conditionally reserve the mobile bottom-tab overlay.
 *
 * The bottom tab bar is `position: fixed`; permanently padding the app shell
 * by its height makes short pages look like they have a large useless gap.
 * This action measures the scroller's actual content height and adds bottom
 * padding only when the content would otherwise run underneath the tab bar.
 */

export interface AdaptiveBottomInsetParam {
  /** Disable measurement and remove any padding. */
  disabled?: boolean;
  /** CSS variable containing the overlay height. */
  variable?: string;
}

function readCssPx(name: string): number {
  if (typeof document === 'undefined') return 0;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

function currentPaddingBottom(node: HTMLElement): number {
  const value = Number.parseFloat(getComputedStyle(node).paddingBottom);
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

export function adaptiveBottomInset(node: HTMLElement, initial: AdaptiveBottomInsetParam = {}) {
  let param = initial;
  let rafId = 0;

  const measure = () => {
    rafId = 0;
    if (param.disabled) {
      node.style.paddingBottom = '';
      return;
    }

    const variable = param.variable ?? '--app-shell-bottom-offset';
    const overlayHeight = readCssPx(variable);
    if (overlayHeight <= 0) {
      node.style.paddingBottom = '';
      return;
    }

    // `scrollHeight` includes padding. Remove our current inline padding to
    // recover the real content height, then decide whether any content would
    // sit under the fixed bottom tab bar.
    const existingPadding = currentPaddingBottom(node);
    const contentHeight = Math.max(0, node.scrollHeight - existingPadding);
    const visibleAboveOverlay = Math.max(0, node.clientHeight - overlayHeight);
    const shouldReserve = contentHeight > visibleAboveOverlay + 1;
    const nextPadding = shouldReserve ? `${Math.round(overlayHeight)}px` : '';
    if (node.style.paddingBottom !== nextPadding) {
      node.style.paddingBottom = nextPadding;
    }
  };

  const schedule = () => {
    if (rafId !== 0) return;
    rafId = window.requestAnimationFrame(measure);
  };

  const resizeObserver = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(schedule) : null;
  resizeObserver?.observe(node);

  const mutationObserver = typeof MutationObserver !== 'undefined' ? new MutationObserver(schedule) : null;
  mutationObserver?.observe(node, { childList: true, subtree: true, attributes: true });
  mutationObserver?.observe(document.documentElement, { attributes: true, attributeFilter: ['style'] });

  window.addEventListener('resize', schedule, { passive: true });
  schedule();

  return {
    update(next: AdaptiveBottomInsetParam = {}) {
      param = next;
      schedule();
    },
    destroy() {
      if (rafId !== 0) {
        window.cancelAnimationFrame(rafId);
        rafId = 0;
      }
      resizeObserver?.disconnect();
      mutationObserver?.disconnect();
      window.removeEventListener('resize', schedule);
      node.style.paddingBottom = '';
    }
  };
}

/**
 * Reserve app-owned mobile chrome independently of content layout.
 * scrollHeight is floored by clientHeight, so subtracting self-applied padding
 * cannot recover intrinsic content height and creates a resize feedback cycle.
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

    const nextPadding = `${Math.round(overlayHeight)}px`;
    if (node.style.paddingBottom !== nextPadding) {
      node.style.paddingBottom = nextPadding;
    }
  };

  const schedule = () => {
    if (rafId !== 0) return;
    rafId = window.requestAnimationFrame(measure);
  };

  const mutationObserver = typeof MutationObserver !== 'undefined' ? new MutationObserver(schedule) : null;
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
      mutationObserver?.disconnect();
      window.removeEventListener('resize', schedule);
      node.style.paddingBottom = '';
    }
  };
}

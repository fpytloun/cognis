import { viewportMetrics } from '$lib/stores/viewport';
import type { Readable } from 'svelte/store';

type KeyboardGeometry = {
  visibleBottom: number;
  keyboardOpen: boolean;
};
type ViewportMetricSnapshot = {
  height: number;
  keyboardOpen: boolean;
};

function currentKeyboardGeometry(fallback: KeyboardGeometry): KeyboardGeometry {
  if (
    typeof document === 'undefined'
    || typeof window === 'undefined'
    || document.documentElement.dataset.keyboard !== 'open'
    || !window.visualViewport
  ) return fallback;
  return {
    visibleBottom: window.visualViewport.offsetTop + window.visualViewport.height,
    keyboardOpen: true,
  };
}

export function keyboardOcclusionForRect(
  rect: Pick<DOMRect, 'top' | 'bottom'>,
  geometry: KeyboardGeometry,
): number {
  if (!geometry.keyboardOpen) return 0;
  const height = Math.max(0, rect.bottom - rect.top);
  return Math.min(height, Math.max(0, rect.bottom - geometry.visibleBottom));
}

export function keyboardAvoidance(
  node: HTMLElement,
  metricsStore: Pick<Readable<ViewportMetricSnapshot>, 'subscribe'> = viewportMetrics,
): { destroy(): void } {
  let geometry: KeyboardGeometry = { visibleBottom: 0, keyboardOpen: false };
  let frameId: number | null = null;

  const update = () => {
    frameId = null;
    const currentGeometry = currentKeyboardGeometry(geometry);
    const occlusion = keyboardOcclusionForRect(node.getBoundingClientRect(), currentGeometry);
    node.style.setProperty('--app-local-keyboard-occlusion', `${Math.round(occlusion)}px`);
    if (currentGeometry.keyboardOpen) frameId = requestAnimationFrame(update);
  };
  const scheduleUpdate = () => {
    if (frameId === null) frameId = requestAnimationFrame(update);
  };

  const unsubscribe = metricsStore.subscribe((metrics) => {
    geometry = {
      visibleBottom: metrics.height,
      keyboardOpen: metrics.keyboardOpen,
    };
    scheduleUpdate();
  });
  const resizeObserver = typeof ResizeObserver === 'undefined'
    ? null
    : new ResizeObserver(scheduleUpdate);
  const visualViewport = typeof window === 'undefined' ? null : window.visualViewport;
  resizeObserver?.observe(node);
  visualViewport?.addEventListener('resize', scheduleUpdate);
  visualViewport?.addEventListener('scroll', scheduleUpdate);
  if (typeof window !== 'undefined') {
    window.addEventListener('resize', scheduleUpdate, { passive: true });
  }
  scheduleUpdate();

  return {
    destroy() {
      unsubscribe();
      resizeObserver?.disconnect();
      visualViewport?.removeEventListener('resize', scheduleUpdate);
      visualViewport?.removeEventListener('scroll', scheduleUpdate);
      if (typeof window !== 'undefined') {
        window.removeEventListener('resize', scheduleUpdate);
      }
      if (frameId !== null) cancelAnimationFrame(frameId);
      node.style.removeProperty('--app-local-keyboard-occlusion');
    },
  };
}

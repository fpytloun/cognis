import { get, readable } from 'svelte/store';

type OverlayKind = 'blocking' | 'sheet' | 'fullscreen';

interface OverlayEntry {
  id: string;
  kind: OverlayKind;
  blocksChrome: boolean;
}

let entries: OverlayEntry[] = [];
interface ScrollLockState {
  element: HTMLElement;
  scrollTop: number;
  scrollLeft: number;
  overflow: string;
  overflowX: string;
  overflowY: string;
  overscrollBehavior: string;
}

let scrollLock: ScrollLockState | null = null;
const listeners = new Set<(value: OverlayEntry[]) => void>();

function emit(): void {
  for (const listener of listeners) {
    listener(entries);
  }
}

function scrollContainer(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-app-content="true"]') ?? document.scrollingElement as HTMLElement | null;
}

function lockAppScroll(): void {
  if (typeof document === 'undefined' || scrollLock) return;
  const element = scrollContainer();
  if (!element) return;
  scrollLock = {
    element,
    scrollTop: element.scrollTop,
    scrollLeft: element.scrollLeft,
    overflow: element.style.overflow,
    overflowX: element.style.overflowX,
    overflowY: element.style.overflowY,
    overscrollBehavior: element.style.overscrollBehavior,
  };
  element.style.overflow = 'hidden';
  element.style.overflowX = 'hidden';
  element.style.overflowY = 'hidden';
  element.style.overscrollBehavior = 'none';
  element.dataset.overlayScrollLocked = 'true';
}

function unlockAppScroll(): void {
  if (!scrollLock) return;
  const saved = scrollLock;
  scrollLock = null;
  saved.element.style.overflow = saved.overflow;
  saved.element.style.overflowX = saved.overflowX;
  saved.element.style.overflowY = saved.overflowY;
  saved.element.style.overscrollBehavior = saved.overscrollBehavior;
  delete saved.element.dataset.overlayScrollLocked;
  const restore = (): void => {
    saved.element.scrollTop = saved.scrollTop;
    saved.element.scrollLeft = saved.scrollLeft;
  };
  restore();
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(restore);
}

function syncScrollLock(): void {
  if (entries.length > 0) {
    lockAppScroll();
    return;
  }
  unlockAppScroll();
}

export const overlayStack = readable<OverlayEntry[]>(entries, (set) => {
  listeners.add(set);
  set(entries);
  return () => listeners.delete(set);
});

export const blockingOverlayActive = readable(false, (set) => {
  const listener = (): void => set(entries.some((entry) => entry.blocksChrome));
  listeners.add(listener);
  listener();
  return () => listeners.delete(listener);
});

export function registerOverlay(options: { kind: OverlayKind; blocksChrome: boolean }): {
  id: string;
  unregister: () => void;
} {
  const id = `ov_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  entries = [...entries, { id, ...options }];
  syncScrollLock();
  emit();
  let active = true;
  return {
    id,
    unregister: () => {
      if (!active) return;
      active = false;
      entries = entries.filter((entry) => entry.id !== id);
      syncScrollLock();
      emit();
    }
  };
}

export function isTopOverlay(id: string | null): boolean {
  if (!id) return false;
  return get(overlayStack).at(-1)?.id === id;
}

export function resetOverlayState(): void {
  entries = [];
  syncScrollLock();
  emit();
}

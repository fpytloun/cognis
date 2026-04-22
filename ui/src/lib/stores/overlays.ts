import { get, readable } from 'svelte/store';

type OverlayKind = 'blocking' | 'sheet' | 'fullscreen';

interface OverlayEntry {
  id: string;
  kind: OverlayKind;
  blocksChrome: boolean;
}

let entries: OverlayEntry[] = [];
let savedScrollY = 0;
let bodyLocked = false;
const listeners = new Set<(value: OverlayEntry[]) => void>();

function emit(): void {
  for (const listener of listeners) {
    listener(entries);
  }
}

function lockBodyScroll(): void {
  if (typeof document === 'undefined' || bodyLocked) return;
  savedScrollY = window.scrollY || document.documentElement.scrollTop || 0;
  document.body.style.position = 'fixed';
  document.body.style.top = `-${savedScrollY}px`;
  document.body.style.left = '0';
  document.body.style.right = '0';
  document.body.style.width = '100%';
  document.body.style.overflow = 'hidden';
  bodyLocked = true;
}

function unlockBodyScroll(): void {
  if (typeof document === 'undefined' || !bodyLocked) return;
  document.body.style.position = '';
  document.body.style.top = '';
  document.body.style.left = '';
  document.body.style.right = '';
  document.body.style.width = '';
  document.body.style.overflow = '';
  if (savedScrollY > 0) {
    window.scrollTo(0, savedScrollY);
  }
  savedScrollY = 0;
  bodyLocked = false;
}

function syncBodyLock(): void {
  if (entries.length > 0) {
    lockBodyScroll();
    return;
  }
  unlockBodyScroll();
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
  syncBodyLock();
  emit();
  let active = true;
  return {
    id,
    unregister: () => {
      if (!active) return;
      active = false;
      entries = entries.filter((entry) => entry.id !== id);
      syncBodyLock();
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
  syncBodyLock();
  emit();
}

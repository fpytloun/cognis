import { writable } from 'svelte/store';

export type ToastVariant = 'success' | 'error' | 'warning' | 'info';

export interface ToastItem {
  id: string;
  title: string | null;
  message: string;
  variant: ToastVariant;
  duration: number;
}

const toasts = writable<ToastItem[]>([]);
const timers = new Map<string, number>();

function scheduleRemoval(id: string, duration: number): void {
  if (typeof window === 'undefined' || duration <= 0) {
    return;
  }
  const existing = timers.get(id);
  if (existing !== undefined) {
    window.clearTimeout(existing);
  }
  timers.set(
    id,
    window.setTimeout(() => {
      removeToast(id);
    }, duration)
  );
}

export function addToast(
  message: string,
  variant: ToastVariant = 'info',
  duration = 4_000,
  title: string | null = null
): string {
  const id = `toast-${crypto.randomUUID()}`;
  const item: ToastItem = {
    id,
    title,
    message,
    variant,
    duration
  };
  toasts.update((items) => [item, ...items]);
  scheduleRemoval(id, duration);
  return id;
}

export function removeToast(id: string): void {
  if (typeof window !== 'undefined') {
    const timer = timers.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.delete(id);
    }
  }
  toasts.update((items) => items.filter((item) => item.id !== id));
}

export function clearToasts(): void {
  if (typeof window !== 'undefined') {
    for (const timer of timers.values()) {
      window.clearTimeout(timer);
    }
  }
  timers.clear();
  toasts.set([]);
}

export const toastStore = {
  subscribe: toasts.subscribe
};

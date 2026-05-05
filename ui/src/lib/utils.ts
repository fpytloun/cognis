import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

export function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, milliseconds);
  });
}

export function ensureArray<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function toErrorMessage(error: unknown, fallback = 'Unexpected error'): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }

  if (typeof error === 'string' && error.length > 0) {
    return error;
  }

  return fallback;
}

export function createId(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}_${crypto.randomUUID().slice(0, 12)}`;
  }

  return `${prefix}_${Math.random().toString(36).slice(2, 14)}`;
}

type WakeLockSentinelLike = {
  release: () => Promise<void>;
  released?: boolean;
  addEventListener?: (type: 'release', listener: () => void) => void;
};

type NavigatorWithWakeLock = Navigator & {
  wakeLock?: {
    request: (type: 'screen') => Promise<WakeLockSentinelLike>;
  };
};

/** Keep the screen awake while a user is recording or in voice mode. */
export class ScreenWakeLock {
  private sentinel: WakeLockSentinelLike | null = null;
  private active = false;
  private visibilityHandler: (() => void) | null = null;

  async acquire(): Promise<void> {
    if (typeof navigator === 'undefined' || typeof document === 'undefined') return;
    const wakeLock = (navigator as NavigatorWithWakeLock).wakeLock;
    if (!wakeLock) return;
    this.active = true;
    if (!this.visibilityHandler) {
      this.visibilityHandler = () => {
        if (document.visibilityState === 'visible' && this.active && !this.sentinel) {
          void this.request();
        }
      };
      document.addEventListener('visibilitychange', this.visibilityHandler);
    }
    await this.request();
  }

  async release(): Promise<void> {
    this.active = false;
    if (typeof document !== 'undefined' && this.visibilityHandler) {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
      this.visibilityHandler = null;
    }
    const sentinel = this.sentinel;
    this.sentinel = null;
    if (sentinel && !sentinel.released) {
      await sentinel.release().catch(() => {});
    }
  }

  private async request(): Promise<void> {
    if (typeof navigator === 'undefined' || typeof document === 'undefined') return;
    if (document.visibilityState !== 'visible') return;
    const wakeLock = (navigator as NavigatorWithWakeLock).wakeLock;
    if (!wakeLock || this.sentinel) return;
    try {
      this.sentinel = await wakeLock.request('screen');
      this.sentinel.addEventListener?.('release', () => {
        this.sentinel = null;
      });
    } catch {
      this.sentinel = null;
    }
  }
}

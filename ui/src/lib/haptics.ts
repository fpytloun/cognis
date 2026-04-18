/**
 * Minimal haptic feedback helper.
 *
 * Uses `navigator.vibrate` when available (Android) and is a no-op
 * elsewhere. iOS Safari does not implement `vibrate`; this is accepted —
 * the primary value is Android where it matches native app feel.
 *
 * Exported patterns roughly match iOS UIImpactFeedbackStyle tiers.
 */

function canVibrate(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    typeof (navigator as Navigator & { vibrate?: (pattern: number | number[]) => boolean }).vibrate === 'function'
  );
}

function vibrate(pattern: number | number[]): void {
  if (!canVibrate()) return;
  try {
    (navigator as Navigator & { vibrate: (pattern: number | number[]) => boolean }).vibrate(pattern);
  } catch {
    /* ignore */
  }
}

export const haptic = {
  light(): void {
    vibrate(8);
  },
  medium(): void {
    vibrate(14);
  },
  heavy(): void {
    vibrate(22);
  },
  success(): void {
    vibrate([10, 40, 10]);
  },
  warning(): void {
    vibrate([16, 40, 16]);
  },
  error(): void {
    vibrate([24, 40, 24, 40, 24]);
  }
};

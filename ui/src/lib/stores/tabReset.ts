import { writable } from 'svelte/store';

/**
 * Tab-reset pub/sub.
 *
 * The bottom tab bar emits a signal when the user taps the already-active
 * tab. Pages subscribe, compare the `href` to their own top-level route,
 * and run local reset logic — typically clearing filters, closing
 * expanded UI state, and scrolling their content container back to the
 * top.
 *
 * The payload carries a monotonically-increasing `nonce` so a subscriber
 * that missed the first emission (e.g. not yet mounted) can still react
 * on the next one, and so repeated taps produce distinct store updates
 * even if `href` is unchanged.
 */

export interface TabResetSignal {
  href: string;
  nonce: number;
}

export const tabResetSignal = writable<TabResetSignal | null>(null);

let lastNonce = 0;

export function emitTabReset(href: string): void {
  lastNonce += 1;
  tabResetSignal.set({ href, nonce: lastNonce });
}

/**
 * Convenience subscription: invoke `handler` every time a reset signal
 * arrives whose `href` matches the supplied route. Returns an unsubscribe
 * function suitable for use in `onMount(() => onTabReset(...))`.
 */
export function onTabReset(href: string, handler: () => void): () => void {
  let seen = -1;
  return tabResetSignal.subscribe((signal) => {
    if (!signal || signal.href !== href) return;
    if (signal.nonce === seen) return;
    seen = signal.nonce;
    handler();
  });
}

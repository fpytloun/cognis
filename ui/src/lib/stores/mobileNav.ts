import { writable } from 'svelte/store';

/**
 * Shared signal for opening the global mobile navigation drawer.
 *
 * The drawer itself lives in the `(app)` layout, but some pages hide the
 * global mobile header (chat detail, for example) and need to expose their
 * own hamburger without duplicating the entire layout chrome. This store
 * lets any in-app page request that the drawer opens without importing
 * private layout state.
 */
const mobileNavOpenRequest = writable(0);

export const mobileNavOpenSignal = { subscribe: mobileNavOpenRequest.subscribe };

export function requestOpenMobileNav(): void {
  mobileNavOpenRequest.update((value) => value + 1);
}

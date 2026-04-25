import { writable } from 'svelte/store';

/**
 * Shared signals for the global mobile navigation drawer.
 *
 * The drawer itself lives in the `(app)` layout, but some pages hide
 * the global mobile header (chat detail) and need to (a) request that
 * the drawer opens, and (b) read whether it is currently open so they
 * can stack their own swipe gestures correctly. Both surfaces are
 * exposed here so private layout state never has to leak across the
 * route boundary.
 */

// Edge-triggered open request. Subscribers fire on every increment;
// the layout uses that to call `openMobileNav()` once per emission.
const mobileNavOpenRequest = writable(0);

export const mobileNavOpenSignal = { subscribe: mobileNavOpenRequest.subscribe };

export function requestOpenMobileNav(): void {
  mobileNavOpenRequest.update((value) => value + 1);
}

/**
 * Live boolean reflection of `mobileNavOpen` in the `(app)` layout.
 * The layout is the sole writer; readers should treat it as
 * read-only state. Use `requestOpenMobileNav()` to ask the layout to
 * open the drawer rather than mutating this store directly.
 */
export const mobileNavOpen = writable(false);

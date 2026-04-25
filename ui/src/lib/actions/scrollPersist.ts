/**
 * `use:scrollPersist` — restore and persist an element's `scrollTop` across
 * navigations, keyed by an arbitrary string.
 *
 * Usage:
 *
 *   <div use:scrollPersist={{ key: $page.url.pathname }}>…</div>
 *
 * Behaviour:
 *
 * - On mount, reads `sessionStorage[STORAGE_PREFIX + key]` and applies it as
 *   the element's `scrollTop` on the next frame (so any content rendered
 *   after mount is in place before we scroll).
 * - On every scroll, debounces a write of the current `scrollTop` to the
 *   same key via `requestAnimationFrame`.
 * - When the `key` parameter changes, writes the last value under the old
 *   key, then restores the value for the new key. That makes it safe to
 *   bind to a reactive value like `$page.url.pathname` — switching routes
 *   never loses the previous route's scroll position.
 * - When the element unmounts, a final write happens so the value is
 *   captured even on fast tab-aways.
 *
 * Storage is `sessionStorage` so positions don't survive a full reload;
 * that matches what mobile users expect from a native tab bar and avoids
 * leaking long-lived state between sessions.
 */

const STORAGE_PREFIX = 'cognis-scroll:';

type ScrollPersistParam = {
  /** Storage key. Typically `$page.url.pathname`. */
  key: string;
  /**
   * Optional flag to skip persistence entirely for a key. Useful for pages
   * that want the default "scroll to top on mount" behaviour.
   */
  disabled?: boolean;
};

function readStored(key: string): number | null {
  if (typeof sessionStorage === 'undefined') return null;
  const raw = sessionStorage.getItem(STORAGE_PREFIX + key);
  if (raw === null) return null;
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) ? value : null;
}

function writeStored(key: string, value: number): void {
  if (typeof sessionStorage === 'undefined') return;
  try {
    sessionStorage.setItem(STORAGE_PREFIX + key, String(Math.max(0, Math.round(value))));
  } catch {
    // Quota or privacy-mode errors are non-fatal.
  }
}

export function scrollPersist(node: HTMLElement, param: ScrollPersistParam) {
  let currentKey = param.key;
  let disabled = param.disabled === true;
  let rafId = 0;
  let lastWritten = -1;

  const flush = () => {
    if (disabled) return;
    rafId = 0;
    const next = node.scrollTop;
    if (next === lastWritten) return;
    lastWritten = next;
    writeStored(currentKey, next);
  };

  const onScroll = () => {
    if (disabled) return;
    if (rafId !== 0) return;
    rafId = window.requestAnimationFrame(flush);
  };

  const restore = (key: string) => {
    if (disabled) return;
    const stored = readStored(key);
    if (stored === null) return;
    // Use rAF so the browser has laid out the content before we set
    // scrollTop; otherwise the value can be clamped to 0 on empty
    // containers and lost.
    window.requestAnimationFrame(() => {
      node.scrollTop = stored;
      lastWritten = stored;
    });
  };

  node.addEventListener('scroll', onScroll, { passive: true });
  restore(currentKey);

  return {
    update(next: ScrollPersistParam) {
      const nextKey = next.key;
      const nextDisabled = next.disabled === true;

      if (nextKey !== currentKey) {
        // Capture the current position under the old key before switching.
        if (!disabled) writeStored(currentKey, node.scrollTop);
        currentKey = nextKey;
        lastWritten = -1;
      }

      disabled = nextDisabled;
      if (!disabled) restore(currentKey);
    },
    destroy() {
      if (rafId !== 0) {
        window.cancelAnimationFrame(rafId);
        rafId = 0;
      }
      if (!disabled) writeStored(currentKey, node.scrollTop);
      node.removeEventListener('scroll', onScroll);
    }
  };
}

/**
 * Imperatively clear a stored scroll position. Useful when an active-tab
 * tap resets the view — we want the user to see the top of the page, and
 * we do not want a stale value to snap back on the next mount.
 */
export function clearPersistedScroll(key: string): void {
  if (typeof sessionStorage === 'undefined') return;
  try {
    sessionStorage.removeItem(STORAGE_PREFIX + key);
  } catch {
    // non-fatal
  }
}

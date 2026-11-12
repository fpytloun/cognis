import { writable } from 'svelte/store';
import { identity, type EntityKind, type PaletteItem, type PalettePage } from './model';

export interface SearchState {
  items: PaletteItem[]; loading: boolean; cursor: string | null; error: string; capped: boolean; paused: boolean;
}
export type FetchPage = (query: string, cursor: string | null, signal: AbortSignal, types?: readonly EntityKind[]) => Promise<PalettePage>;
const empty = (): SearchState => ({ items: [], loading: false, cursor: null, error: '', capped: false, paused: false });
const CACHE_ENTRIES = 16;
const MAX_ITEMS = 400;
const TTL = 15_000;
const BUSY_WAIT_MS = 30_000;
const BUSY_RETRY_MS = 250;
// Matches the server's per-user start rate; the debounce only coalesces bursts.
const MIN_START_SPACING_MS = 150;
const DEBOUNCE_MS = 50;

/** One active flight, latest query coalescing, bounded metadata and query caches. */
export class PaletteSearch {
  private store = writable<SearchState>(empty());
  subscribe = this.store.subscribe;
  private state = empty();
  private query = '';
  private types: readonly EntityKind[] | undefined;
  private cacheKey = '';
  private epoch = 0;
  private cacheEpoch = 0;
  private cache = new Map<string, { page: PalettePage; at: number }>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private flight: AbortController | null = null;
  private settled: Promise<void> = Promise.resolve();
  private opened = false;
  private lastStarted = 0;
  private pages = 0;
  private sparsePages = 0;
  private retryCursor: string | null = null;

  constructor(private fetchPage: FetchPage) {}
  private publish(value: Partial<SearchState>) {
    this.state = { ...this.state, ...value }; this.store.set(this.state);
  }
  setQuery(query: string, types: readonly EntityKind[] | undefined = this.types) {
    this.query = query.trim().slice(0, 160); this.opened = true;
    this.types = types ? [...new Set(types)].sort() : undefined;
    this.cacheKey = JSON.stringify([this.query, this.types ?? null]);
    const epoch = ++this.epoch;
    if (this.timer) clearTimeout(this.timer);
    // Let the admitted server read settle; aborting fetch does not stop its SQL.
    // The epoch fences stale results while the newest query waits behind it.
    const cached = this.cache.get(this.cacheKey);
    const fresh = cached && Date.now() - cached.at < TTL;
    const enabled = this.types === undefined || this.types.length > 0;
    this.pages = fresh ? 1 : 0; this.sparsePages = 0; this.retryCursor = null;
    this.publish({
      // Only the same query's ranked page can be reused. Mixing arbitrary
      // cached entities made the initial snapshot jump when SQL settled.
      items: enabled && fresh ? cached.page.items : [],
      loading: enabled && !fresh, cursor: fresh ? cached.page.next_cursor : null, error: '', capped: false, paused: false,
    });
    if (enabled && !fresh) this.timer = setTimeout(() => void this.load(epoch, null), Math.max(DEBOUNCE_MS, MIN_START_SPACING_MS - (Date.now() - this.lastStarted)));
  }
  more(manual = false) {
    if (!manual && !this.query) return;
    if (this.state.loading || !this.state.cursor || this.state.capped || this.state.error || (this.state.paused && !manual)) return;
    if (manual) this.sparsePages = 0;
    this.publish({ loading: true, paused: false });
    void this.load(this.epoch, this.state.cursor);
  }
  retry() {
    if (!this.state.error || this.state.loading || !this.opened) return;
    this.publish({ error: '', loading: true });
    void this.load(this.epoch, this.retryCursor);
  }
  private async load(epoch: number, cursor: string | null, busyDeadline = Date.now() + BUSY_WAIT_MS) {
    if (!this.opened || epoch !== this.epoch) return;
    const delay = MIN_START_SPACING_MS - (Date.now() - this.lastStarted);
    if (delay > 0) {
      this.timer = setTimeout(() => void this.load(epoch, cursor, busyDeadline), delay); return;
    }
    // Aborting fetch does not guarantee cancellation of SQL. Wait until the
    // transport settles; the server separately admits only one query per user.
    // Start the latest query as soon as the previous transport settles.
    if (this.flight) {
      void this.settled.then(() => this.load(epoch, cursor, busyDeadline)); return;
    }
    const controller = new AbortController();
    let settle!: () => void;
    this.settled = new Promise((resolve) => { settle = resolve; });
    this.flight = controller; this.lastStarted = Date.now();
    const query = this.query;
    const types = this.types;
    const cacheKey = this.cacheKey;
    const cacheEpoch = this.cacheEpoch;
    try {
      const page = await this.fetchPage(query, cursor, controller.signal, types);
      if (epoch !== this.epoch || !this.opened) return;
      this.pages++;
      this.sparsePages = page.items.length ? 0 : this.sparsePages + 1;
      if (cacheEpoch === this.cacheEpoch) {
        if (!cursor) {
          this.cache.delete(cacheKey); this.cache.set(cacheKey, { page, at: Date.now() });
          while (this.cache.size > CACHE_ENTRIES) this.cache.delete(this.cache.keys().next().value!);
        }
      }
      const merged = new Map<string, PaletteItem>();
      for (const item of [...cursor ? this.state.items : [], ...page.items]) merged.set(identity(item), item);
      const items = [...merged.values()].slice(0, MAX_ITEMS);
      this.publish({
        items, loading: false, cursor: page.next_cursor,
        capped: merged.size >= MAX_ITEMS || this.pages >= 40, paused: this.sparsePages >= 2,
      });
    } catch (error) {
      if (epoch !== this.epoch || !this.opened || controller.signal.aborted) return;
      // Other tabs and a server read surviving a transport timeout can still
      // occupy admission. Keep the latest query loading instead of requiring a click.
      if (error instanceof Error && 'code' in error && error.code === 'search_busy' && Date.now() < busyDeadline) {
        this.timer = setTimeout(() => void this.load(epoch, cursor, busyDeadline), BUSY_RETRY_MS);
        return;
      }
      this.retryCursor = cursor;
      this.publish({ loading: false, error: error instanceof Error ? error.message : 'Search unavailable. Try again.' });
    } finally {
      if (this.flight === controller) this.flight = null;
      settle();
    }
  }
  close() {
    this.opened = false; ++this.epoch;
    if (this.timer) clearTimeout(this.timer);
    this.publish({ loading: false });
  }
  clear() {
    this.close(); this.flight?.abort(); this.cache.clear(); this.publish(empty());
  }
  invalidate(accessChanged = false) {
    this.cache.clear(); ++this.cacheEpoch;
    // Routine background activity must not disrupt the selected snapshot or
    // trigger extra reads. Scope/revocation events retire it immediately.
    if (!accessChanged) return;
    ++this.epoch;
    this.pages = 0; this.sparsePages = 0;
    if (this.timer) clearTimeout(this.timer);
    this.retryCursor = null;
    this.publish(empty());
    // Retire revoked metadata immediately, then recover the current query.
    // Old responses remain fenced by the new epoch.
    if (this.opened) this.setQuery(this.query);
  }
}

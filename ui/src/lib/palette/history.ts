const LIMIT = 20;
const MAX_QUERY = 160;
const MAX_STORAGE = 32768;
type HistoryStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function clean(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const query = value.trim();
  return query && query.length <= MAX_QUERY ? query : null;
}

/** Browser-local query strings only; never stores results, entity IDs or URLs. */
export class PaletteHistory {
  #owner: string | null = null;
  #entries: string[] = [];
  #position = -1;
  #draft = '';
  #persisted = true;
  #storage: () => HistoryStorage;

  constructor(storage: () => HistoryStorage = () => localStorage) { this.#storage = storage; }
  get #key(): string { return `cognis.palette.history:${encodeURIComponent(this.#owner ?? '')}`; }
  get size(): number { return this.#entries.length; }
  get browsing(): boolean { return this.#position >= 0; }

  open(owner: string | null): void {
    if (owner !== this.#owner) { this.#entries = []; this.#persisted = true; }
    this.#owner = owner;
    this.reset();
    // A failed write must not let stale storage replace this account's session history.
    if (!owner || !this.#persisted) return;
    try {
      const raw = this.#storage().getItem(this.#key);
      this.#entries = [];
      const saved: unknown = raw && raw.length <= MAX_STORAGE ? JSON.parse(raw) : null;
      if (Array.isArray(saved)) {
        for (const value of saved.slice(0, LIMIT)) {
          const query = clean(value);
          if (query && !this.#entries.some(entry => entry.toLowerCase() === query.toLowerCase())) {
            this.#entries.push(query);
          }
        }
      }
    } catch { /* History still works in memory when browser storage is disabled. */ }
  }

  remember(value: string): void {
    const query = clean(value);
    if (!this.#owner || !query) return;
    this.#entries = [query, ...this.#entries.filter(entry => entry.toLowerCase() !== query.toLowerCase())].slice(0, LIMIT);
    this.reset();
    try { this.#storage().setItem(this.#key, JSON.stringify(this.#entries)); this.#persisted = true; }
    catch { this.#persisted = false; }
  }

  previous(current: string): string | null {
    if (!this.#entries.length) return null;
    if (!this.browsing) this.#draft = current;
    this.#position = Math.min(this.#position + 1, this.#entries.length - 1);
    return this.#entries[this.#position];
  }

  next(): string | null {
    if (!this.browsing) return null;
    if (--this.#position < 0) return this.#draft;
    return this.#entries[this.#position];
  }

  reset(): void { this.#position = -1; this.#draft = ''; }
  clear(): void {
    this.#entries = [];
    this.reset();
    if (this.#owner) {
      try { this.#storage().removeItem(this.#key); this.#persisted = true; }
      catch { this.#persisted = false; }
    }
  }
}

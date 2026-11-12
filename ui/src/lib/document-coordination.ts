import { apiUrl } from '$lib/config';
import { CoordinationUnavailableError, ProfileIntentStore, validIntent, type ProfileIntent } from './profile-intent';
export { CoordinationUnavailableError } from './profile-intent';

export class StaleDocumentError extends Error {
  constructor() {
    super('The account or server changed while this operation was pending.');
    this.name = 'StaleDocumentError';
  }
}

export interface DocumentLease {
  readonly owner: string | null;
  readonly signal: AbortSignal;
  readonly epoch: string;
  /** Projection-only check, for synchronous UI use. */
  assertCurrent(): void;
  validate(): Promise<void>;
}

export type DocumentInvalidationReason = 'changed' | 'unavailable';

export interface CoordinationEnvironment {
  storage?: Storage;
  indexedDB?: IDBFactory;
  locks: Pick<LockManager, 'request'> | undefined;
  profile(): string;
  generation(): string;
  uuid(): string;
  subscribe(listener: () => void): () => void;
  publish(): void;
}

/** Profile intent/epochs, never a credential cache or a second auth store. */
export class DocumentCoordination {
  private readonly profile: string;
  private readonly generation: string;
  private readonly metadata: ProfileIntentStore;
  private current: ProfileIntent;
  private initialized = false;
  private disposed = false;
  private metadataQueue: Promise<unknown> = Promise.resolve();
  private controller = new AbortController();
  private verifiedAccount: string | null = null;
  private readonly listeners = new Set<(reason: DocumentInvalidationReason) => void>();
  private readonly unsubscribe: () => void;

  constructor(private readonly environment: CoordinationEnvironment) {
    this.profile = environment.profile();
    this.generation = environment.generation();
    this.metadata = new ProfileIntentStore(environment.indexedDB ?? globalThis.indexedDB);
    this.current = { epoch: environment.uuid(), account: null, blocked: false, refresh: '', revision: 0 };
    this.unsubscribe = environment.subscribe(() => {
      void this.synchronize().catch(() => undefined);
    });
  }

  /** An unverified document observes intent; it must not publish a fresh epoch
   * from its constructor. Only one cold initializer creates shared intent. */
  async initialize(): Promise<void> {
    if (!this.supported) throw new CoordinationUnavailableError();
    try {
      await this.transaction((raw) => {
        if (validIntent(raw)) return raw;
        if (this.initialized || raw !== undefined) throw new StaleDocumentError();
        return { ...this.current, revision: 1 };
      });
    } catch (error) {
      this.revoke(error instanceof CoordinationUnavailableError ? 'unavailable' : 'changed');
      throw error;
    }
  }

  private checkEnvironment(): void {
    if (this.disposed || this.environment.profile() !== this.profile
      || this.environment.generation() !== this.generation) throw new StaleDocumentError();
  }

  private revoke(reason: DocumentInvalidationReason = 'changed'): void {
    this.controller.abort(new StaleDocumentError());
    this.controller = new AbortController();
    this.verifiedAccount = null;
    if (typeof navigator !== 'undefined') {
      navigator.serviceWorker?.controller?.postMessage({ type: 'REVOKE_MEDIA_SCOPE' });
    }
    for (const listener of this.listeners) listener(reason);
  }

  private transaction(change: (raw: unknown) => ProfileIntent): Promise<ProfileIntent> {
    const result = this.metadataQueue.then(() => this.performTransaction(change));
    this.metadataQueue = result.catch(() => undefined);
    return result;
  }

  private async performTransaction(change: (raw: unknown) => ProfileIntent): Promise<ProfileIntent> {
    try {
      const state = await this.metadata.transact(this.profile, (raw) => {
        this.checkEnvironment();
        return change(raw);
      });
      this.checkEnvironment();
      if (state.revision < this.current.revision) {
        this.revoke();
        throw new StaleDocumentError();
      }
      if (this.initialized && state.epoch !== this.current.epoch) this.revoke();
      this.current = state;
      this.initialized = true;
      return state;
    } catch (error) {
      if (error instanceof StaleDocumentError) throw error;
      this.revoke('unavailable');
      throw new CoordinationUnavailableError();
    }
  }

  async synchronize(): Promise<void> {
    try {
      await this.transaction((raw) => {
        if (!validIntent(raw)) throw new StaleDocumentError();
        return raw;
      });
    } catch (error) {
      this.revoke(error instanceof CoordinationUnavailableError ? 'unavailable' : 'changed');
      throw error;
    }
  }

  private async update(lease: DocumentLease, change: (state: ProfileIntent) => ProfileIntent): Promise<void> {
    try {
      await this.transaction((raw) => {
        lease.assertCurrent();
        if (!validIntent(raw) || raw.epoch !== lease.epoch) throw new StaleDocumentError();
        const next = change(raw);
        return { ...next, revision: raw.revision + 1 };
      });
    } catch (error) {
      // A rejected old callback must not revoke a newer local login. Re-read
      // authoritative state instead; missing/corrupt state still revokes leases.
      if (error instanceof StaleDocumentError) await this.synchronize().catch(() => undefined);
      throw error;
    }
    try { this.environment.publish(); } catch { /* Invalidation is only a hint. */ }
  }

  get blocked(): boolean { return this.current.blocked; }
  get supported(): boolean { return Boolean(this.environment.locks && (this.environment.indexedDB ?? globalThis.indexedDB)); }
  async resumeCachedAccount(email: string): Promise<boolean> {
    await this.synchronize();
    if (this.current.blocked || this.current.account !== email.trim().toLowerCase()) return false;
    this.verifiedAccount = this.current.account;
    return true;
  }
  get refreshVersion(): string { return this.current.refresh; }
  get owner(): string | null {
    return this.verifiedAccount && !this.current.blocked && this.current.account === this.verifiedAccount
      ? `${this.profile}\n${this.verifiedAccount}` : null;
  }

  /** Only call after a server response verifies this document's account. */
  async verified(account: string, lease = this.capture(false)): Promise<void> {
    account = account.trim().toLowerCase();
    // A late /me must not overwrite a different verified login.
    // Explicit login revokes the previous epoch before replacing its account.
    await this.update(lease, (state) => {
      if (!account || (state.account !== null && state.account !== account)) throw new StaleDocumentError();
      return { ...state, account, blocked: false };
    });
    lease.assertCurrent();
    this.verifiedAccount = account;
    if (typeof navigator !== 'undefined') {
      navigator.serviceWorker?.controller?.postMessage({
        type: 'BIND_MEDIA_SCOPE', owner: `${this.profile}\n${account}`, epoch: this.current.epoch,
      });
    }
  }

  async invalidate(blocked = true, lease = this.capture(false)): Promise<void> {
    await this.update(lease, (state) => {
      if (state.account && lease.owner !== `${this.profile}\n${state.account}`) throw new StaleDocumentError();
      return { ...state, epoch: this.environment.uuid(), account: null, blocked, refresh: '' };
    });
  }

  /** Explicit user login, not an unverified bootstrap or a network callback. */
  async beginLogin(): Promise<void> {
    await this.initialize();
    await this.update(this.capture(false), (state) => ({
      ...state, epoch: this.environment.uuid(), account: null, blocked: true, refresh: '',
    }));
  }

  async refreshed(lease = this.capture()): Promise<void> {
    await this.update(lease, (state) => {
      if (state.blocked || lease.owner !== `${this.profile}\n${state.account}`) throw new StaleDocumentError();
      return { ...state, refresh: this.environment.uuid() };
    });
  }

  onInvalidated(listener: (reason: DocumentInvalidationReason) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  capture(requireAccount = true): DocumentLease {
    if (requireAccount && !this.supported) throw new CoordinationUnavailableError();
    this.checkEnvironment();
    if (!this.initialized) throw new StaleDocumentError();
    const epoch = this.current.epoch;
    const account = this.current.account;
    const controller = this.controller;
    const assertCurrent = () => {
      if (controller.signal.aborted || epoch !== this.current.epoch
        || this.environment.profile() !== this.profile || this.environment.generation() !== this.generation
        || (requireAccount && (!account || this.verifiedAccount !== account || this.current.blocked))) {
        throw new StaleDocumentError();
      }
    };
    assertCurrent();
    return {
      owner: account && this.verifiedAccount === account ? `${this.profile}\n${account}` : null,
      signal: controller.signal,
      epoch,
      assertCurrent,
      validate: async () => {
        assertCurrent();
        await this.synchronize();
        assertCurrent();
      },
    };
  }

  /** Stable namespace, independent of transient login epochs. Never opens legacy stores. */
  storageName(base: string, lease = this.capture()): string {
    lease.assertCurrent();
    if (!lease.owner) throw new StaleDocumentError();
    return `${base}:account-v2:${encodeURIComponent(lease.owner)}`;
  }

  /**
   * Web Locks arbitrate documents, not just promises in this JS instance.
   * Queueing is bounded/cancellable. Once acquired, owners must use the lease's
   * signal and check it after awaits; releasing a lock early around live I/O
   * would permit a second sender while the first request is still running.
   */
  async run<T>(name: string, lease: DocumentLease, action: () => Promise<T>): Promise<T> {
    const locks = this.environment.locks;
    if (!locks) throw new CoordinationUnavailableError();
    const queue = new AbortController();
    const abort = () => queue.abort(lease.signal.reason);
    lease.signal.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => queue.abort(new Error('Another window is busy. Try again.')), 15_000);
    try {
      await lease.validate();
      return await locks.request(`cognis:${this.profile}:${name}`, { signal: queue.signal }, async () => {
        clearTimeout(timer);
        await lease.validate();
        const result = await action();
        await lease.validate();
        return result;
      });
    } finally {
      clearTimeout(timer);
      lease.signal.removeEventListener('abort', abort);
    }
  }

  dispose(): void {
    this.disposed = true;
    this.unsubscribe();
    this.controller.abort(new StaleDocumentError());
    this.listeners.clear();
  }
}

let instance: DocumentCoordination | undefined;

export function documentCoordination(): DocumentCoordination {
  if (!instance) {
    const channel = typeof window.BroadcastChannel === 'undefined'
      ? null : new window.BroadcastChannel('cognis-scope-invalidation-v1');
    instance = new DocumentCoordination({
      locks: navigator.locks,
      profile: () => apiUrl('') || window.location.origin,
      generation: () => window.__cognisNative?.generation ?? '',
      uuid: () => crypto.randomUUID(),
      subscribe(listener) {
        // Payloads never carry authority; always re-read durable intent.
        const changed = () => listener();
        window.addEventListener('storage', changed);
        window.addEventListener('pageshow', changed);
        channel?.addEventListener('message', changed);
        return () => {
          window.removeEventListener('storage', changed);
          window.removeEventListener('pageshow', changed);
          channel?.close();
        };
      },
      publish: () => channel?.postMessage({ changed: true }),
    });
  }
  return instance;
}

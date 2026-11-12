import type { UsageAttemptTarget, UsageDataSource } from './api';
import { usageErrorMessage } from './adapter';
import type { UsageAttempt, UsageAvailability, UsageLoadState, UsageQuery } from './model';

export interface UsageSnapshot {
  result: UsageLoadState;
  attempts: UsageAttempt[];
  attemptsPending: boolean;
  attemptError: string | null;
  nextCursor: string | null;
  availability?: UsageAvailability;
}
/**
 * Active-view request owner. No timers, hidden work, speculative scans or retries.
 * Authority changes replace the query and clear prior data before any new read.
 */
export class UsageController {
  private queryValue: UsageQuery | null = null;
  private active = false;
  private disposed = false;
  private overview: AbortController | null = null;
  private detail: AbortController | null = null;
  private queued = false;
  private dirty = false;
  private target: UsageAttemptTarget | null = null;
  private snapshot: UsageSnapshot = { result: { state: 'loading' }, attempts: [], attemptsPending: false, attemptError: null, nextCursor: null };
  constructor(private readonly source: UsageDataSource, private readonly publish: (snapshot: UsageSnapshot) => void) {}
  private emit(patch: Partial<UsageSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.publish(this.snapshot);
  }
  replace(query: UsageQuery): void {
    this.queryValue = structuredClone(query);
    this.overview?.abort();
    this.overview = null;
    this.closeAttempts();
    this.emit({ result: { state: 'loading' } });
    this.invalidate();
  }
  setActive(active: boolean): void {
    if (this.disposed || active === this.active) return;
    this.active = active;
    if (active) this.invalidate();
    else {
      this.overview?.abort(); this.overview = null;
      this.closeAttempts();
      this.emit({ result: { state: 'loading' } });
    }
  }
  /** Called by explicit refresh, reconnect, visibility restore or authorized push. */
  invalidate(): void {
    if (!this.active || this.disposed || !this.queryValue) return;
    this.dirty = true;
    if (this.queued || this.overview) return;
    this.queued = true;
    queueMicrotask(() => {
      this.queued = false;
      if (this.active && !this.disposed && this.dirty && !this.overview) void this.load();
    });
  }
  private async load(): Promise<void> {
    if (!this.queryValue) return;
    const controller = new AbortController();
    this.overview = controller;
    this.dirty = false;
    try {
      const result = await this.source.query(structuredClone(this.queryValue), controller.signal);
      if (this.overview === controller && !controller.signal.aborted && this.active && !this.disposed) this.emit({ result });
    } catch (error) {
      if (this.overview === controller && !controller.signal.aborted && this.active && !this.disposed) {
        this.emit({ result: { state: 'error', message: usageErrorMessage(error) } });
      }
    } finally {
      if (this.overview === controller) {
        this.overview = null;
        if (this.dirty) this.invalidate();
      }
    }
  }
  closeAttempts(): void {
    this.detail?.abort(); this.detail = null; this.target = null;
    this.emit({ attempts: [], attemptsPending: false, attemptError: null, nextCursor: null, availability: undefined });
  }
  async inspect(target: UsageAttemptTarget): Promise<void> {
    this.closeAttempts();
    this.target = { ...target };
    await this.loadAttempts(null);
  }
  async nextAttempts(): Promise<void> {
    if (!this.snapshot.attemptsPending && this.snapshot.nextCursor) await this.loadAttempts(this.snapshot.nextCursor);
  }
  private async loadAttempts(cursor: string | null): Promise<void> {
    if (!this.active || this.disposed || !this.queryValue || !this.target) return;
    const controller = new AbortController();
    this.detail = controller;
    this.emit({ attemptsPending: true, attemptError: null });
    try {
      const page = await this.source.attempts(structuredClone(this.queryValue), { ...this.target }, cursor, controller.signal);
      if (this.detail !== controller || controller.signal.aborted || this.disposed || !this.active) return;
      // One visible bounded page, never accumulate an unbounded history.
      this.emit({ attempts: page.rows, nextCursor: page.nextCursor, availability: page.availability });
    } catch (error) {
      if (this.detail === controller && !controller.signal.aborted) this.emit({ attemptError: usageErrorMessage(error) });
    } finally {
      if (this.detail === controller) { this.detail = null; this.emit({ attemptsPending: false }); }
    }
  }
  dispose(): void {
    this.disposed = true; this.active = false;
    this.overview?.abort(); this.overview = null; this.closeAttempts();
  }
}

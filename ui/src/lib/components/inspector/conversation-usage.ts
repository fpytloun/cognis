import type { TimelineScope } from '$lib/chat-v2/types';

/** Presentation boundary, separate from the backend-owned raw API DTO. */
export type UsageTarget =
  | { kind: 'conversation'; id: string }
  | { kind: 'execution'; id: string };
export type UsageInclusion = 'direct' | 'inclusive';

export function usageTarget(scope: TimelineScope): UsageTarget | null {
  if (scope.missing_stream) return null;
  if (scope.kind === 'conversation' && scope.conversation_id) {
    return { kind: 'conversation', id: scope.conversation_id };
  }
  if (scope.kind === 'session' && scope.session_id) {
    return { kind: 'execution', id: scope.session_id };
  }
  // A task/activity identity cannot be replaced with its containing conversation.
  return null;
}

export interface UsageSummary {
  coverage: 'complete' | 'partial' | 'unknown';
  costs: {
    currency: string;
    basis: string;
    amount: string | null;
    complete: boolean;
  }[];
  unpricedAttempts: number | null;
  tokens: {
    input: number | null;
    output: number | null;
    cacheRead: number | null;
    cacheWrite: number | null;
    reasoning: number | null;
  };
  attempts: { total: number | null; succeeded: number | null; failed: number | null };
  notes: string[];
  representedSessions?: number | null;
  observedThrough?: string | null;
}

export type UsageResult =
  | { status: 'ready'; summary: UsageSummary }
  | { status: 'unavailable'; reason: string };
export type UsageSource = (
  target: UsageTarget,
  inclusion: UsageInclusion,
  signal: AbortSignal,
) => Promise<UsageResult>;
export type UsageState =
  | { status: 'idle' | 'loading' }
  | { status: 'error'; message: string }
  | UsageResult;

export interface UsageInput {
  target: UsageTarget | null;
  inclusion: UsageInclusion;
  active: boolean;
  visible: boolean;
  source: UsageSource | null;
  actor?: string | null;
}

/** Live updates arrive in bursts (one per turn/step); coalesce them into one read. */
export const USAGE_REFRESH_DEBOUNCE_MS = 600;

function sameSelection(a: UsageInput | null, b: UsageInput | null): boolean {
  return Boolean(a && b && a.target?.kind === b.target?.kind && a.target?.id === b.target?.id
    && a.inclusion === b.inclusion && a.actor === b.actor && a.source === b.source);
}

/** One panel owns cancellation and generation fencing; no shared or durable state. */
export class ConversationUsageController {
  private input: UsageInput | null = null;
  private generation = 0;
  private abort: AbortController | null = null;
  /** Last published result and the selection it belongs to; kept across refreshes. */
  private shown: { input: UsageInput; result: UsageResult } | null = null;
  private pending: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly publish: (state: UsageState) => void) {}

  /** Live invalidation: refresh in place, coalescing bursts, without a loader. */
  invalidate(): void {
    if (this.pending) clearTimeout(this.pending);
    this.pending = setTimeout(() => { this.pending = null; void this.refresh(); }, USAGE_REFRESH_DEBOUNCE_MS);
  }

  update(input: UsageInput): void {
    const previous = this.input;
    this.input = input;
    if (previous
      && previous.target?.kind === input.target?.kind
      && previous.target?.id === input.target?.id
      && previous.inclusion === input.inclusion
      && previous.active === input.active
      && previous.visible === input.visible
      && previous.actor === input.actor
      && previous.source === input.source) return;
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.cancel();
    const input = this.input;
    if (!input?.active || !input.visible) {
      this.publish({ status: 'idle' });
      return;
    }
    if (!input.target || !input.source) {
      this.publish({
        status: 'unavailable',
        reason: !input.target
          ? 'Usage is unavailable for this selected scope.'
          : 'Accounting data is not connected yet.',
      });
      return;
    }
    const generation = this.generation;
    const abort = new AbortController();
    this.abort = abort;
    // Stale-while-revalidate: a refresh of the same selection keeps the current
    // numbers on screen; only a new selection shows the loader.
    const current = this.shown && sameSelection(this.shown.input, input) ? this.shown : null;
    if (!current) this.publish({ status: 'loading' });
    try {
      const result = await input.source(input.target, input.inclusion, abort.signal);
      if (generation === this.generation) {
        this.shown = { input, result };
        this.publish(result);
      }
    } catch {
      // A transient failure behind a shown result keeps that result; the next
      // invalidation retries. Without one, report the failure.
      if (generation === this.generation && !current) {
        this.shown = null;
        this.publish({ status: 'error', message: 'Could not load usage. Try again.' });
      }
    } finally {
      if (this.abort === abort) this.abort = null;
    }
  }

  private cancel(): void {
    this.generation += 1;
    this.abort?.abort();
    this.abort = null;
  }

  destroy(): void {
    if (this.pending) clearTimeout(this.pending);
    this.pending = null;
    this.cancel();
    this.input = null;
    this.shown = null;
  }
}

import { get, writable } from 'svelte/store';
import type { FeedbackController } from './state';
import { loadFeedbackController } from './controller-loader';
import { documentCoordination } from '$lib/document-coordination';
import type { FeedbackContext, FeedbackSourceRef, FeedbackSurfaceBinding } from './types';

/** Owns opened per-event sources; renderers only consume resolved bindings. */
export class FeedbackDiffRegistry {
  readonly active = writable<FeedbackController | null>(null);
  readonly error = writable('');
  readonly activationStatus = writable('');
  private controllers = new Map<string, FeedbackController>();
  private unsubscribers: (() => void)[] = [];
  private pending = new Map<string, Promise<FeedbackController | null>>();
  private loads = new Map<string, Promise<void>>();
  private disposed = false;
  private activationGeneration = 0;
  constructor(
    private readonly context: FeedbackContext,
    private readonly changed: () => void,
    private readonly currentContext: () => FeedbackContext = () => this.context,
    private readonly onOwner?: (owner: FeedbackController) => (() => void) | undefined,
  ) {}
  private captureLease() {
    try { return documentCoordination().capture(); }
    catch { return null; }
  }
  private ensure(source: Extract<FeedbackSourceRef, { kind: 'work_diff' }>): Promise<FeedbackController | null> {
    if (this.disposed) return Promise.resolve(null);
    const key = JSON.stringify(source);
    const existing = this.controllers.get(key);
    if (existing) return Promise.resolve(existing);
    const pending = this.pending.get(key);
    if (pending) return pending;
    const lease = this.captureLease();
    if (!lease) return Promise.resolve(null);
    const promise = Promise.resolve().then(async () => {
      try {
        const { FeedbackController } = await loadFeedbackController();
        if (this.disposed) return null;
        lease.assertCurrent();
        if (JSON.stringify(this.context) !== JSON.stringify(this.currentContext())) return null;
        const opened = new FeedbackController(source, this.context);
        this.controllers.set(key, opened);
        const release = this.onOwner?.(opened);
        if (release) this.unsubscribers.push(release);
        this.unsubscribers.push(opened.state.subscribe(() => this.changed()));
        if (!get(this.active)) this.active.set(opened);
        this.loads.set(key, opened.load());
        return opened;
      } catch (caught) {
        if (!this.disposed && JSON.stringify(this.context) === JSON.stringify(this.currentContext())) {
          try { lease.assertCurrent(); } catch { return null; }
          this.error.set(caught instanceof Error ? caught.message : 'Feedback could not be loaded.');
        }
        return null;
      }
    });
    this.pending.set(key, promise);
    return promise;
  }
  /** Activate one exact event without treating source resolution as comment authority. */
  async activate(source: Extract<FeedbackSourceRef, { kind: 'work_diff' }>, onCommenting?: (owner: FeedbackController) => void): Promise<void> {
    if (this.disposed) return;
    const generation = ++this.activationGeneration;
    const lease = this.captureLease();
    if (!lease) {
      this.error.set('Feedback is unavailable until the current account is verified.');
      return;
    }
    const current = () => !this.disposed && generation === this.activationGeneration
      && JSON.stringify(this.context) === JSON.stringify(this.currentContext());
    this.activationStatus.set('Loading feedback for this file change…');
    try {
      const controller = await this.ensure(source);
      lease.assertCurrent();
      if (!current()) return;
      if (!controller) { this.activationStatus.set(''); return; }
      const previous = get(this.active);
      if (previous && previous !== controller) { previous.presenter.invalidate(); previous.patch({ mode: 'view', open: false }); }
      this.active.set(controller);
      controller.patch({ open: !!get(controller.state).error, view: 'threads' });
      const key = JSON.stringify(source);
      if (!get(controller.state).loading && !get(controller.state).source) {
        this.loads.set(key, controller.load());
      }
      await this.loads.get(key);
      lease.assertCurrent();
      if (!current()) return;
      const state = get(controller.state);
      if (!state.source) { this.activationStatus.set(''); return; }
      const permitted = state.source.capabilities.comment;
      controller.patch({ mode: permitted ? 'comment' : 'view' });
      this.activationStatus.set(permitted ? '' : 'Commenting is not permitted for this file change.');
      if (permitted) onCommenting?.(controller);
    } catch (caught) {
      if (current()) {
        try { lease.assertCurrent(); } catch { return; }
        this.activationStatus.set('');
        this.error.set(caught instanceof Error ? caught.message : 'Feedback could not be loaded.');
      }
    }
  }
  binding(source: Extract<FeedbackSourceRef, { kind: 'work_diff' }> | null | undefined): FeedbackSurfaceBinding | null {
    if (!source) return null;
    const controller = this.controllers.get(JSON.stringify(source));
    if (!controller) {
      if (!get(this.error)) void this.ensure(source);
      return null;
    }
    const binding = controller.binding(get(controller.state));
    if (!binding) return null;
    const selected = controller;
    return { ...binding,
      onAnchorCandidate: candidate => { this.activationGeneration++; this.activationStatus.set(''); this.active.set(selected); binding.onAnchorCandidate(candidate); },
      onSelectThread: thread => { this.activationGeneration++; this.activationStatus.set(''); this.active.set(selected); binding.onSelectThread(thread); },
    };
  }
  retry(): void {
    if (this.disposed) return;
    this.pending.clear(); this.error.set(''); this.changed();
  }
  dispose(): void {
    this.disposed = true;
    this.activationGeneration++;
    for (const stop of this.unsubscribers) stop();
    for (const controller of this.controllers.values()) controller.dispose();
    this.controllers.clear();
  }
}

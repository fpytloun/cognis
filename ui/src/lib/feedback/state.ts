import { writable, get } from 'svelte/store';
import { FeedbackPresenterSession, registerPresenterAdapter } from './presenter-session';
import type { FeedbackPresentationPort } from './presentation';
import { surfaceIdentity } from './annotations/surface-identity';
import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
import { feedbackApi, type FeedbackHistoryItem } from './api';
import { FeedbackDrafts, type FeedbackDraftRecord } from './persistence';
import { createScopedTextResolver } from './scoped-text';
import { feedbackSourceState } from './target-summary';
import type {
  FeedbackContext, FeedbackSourceRef, ResolvedFeedbackSource, FeedbackThread, FeedbackAnchorCandidate,
  FeedbackMode, FeedbackAnnotationAdapter, FeedbackSurfaceBinding, FeedbackEntryVersion, FeedbackPreflight, FeedbackLocator,
} from './types';

/** Text and diff-line selections are always commentable; comment mode is only for click/drag targeting. */
function selectionLocator(locator: FeedbackLocator): boolean {
  if (locator.kind === 'text') return true;
  if (locator.kind === 'diff') return locator.scope === 'lines';
  // Rich targets outside comment mode come only from native selection, including its block fallback.
  if (locator.kind === 'rich') return true;
  if (locator.kind === 'pdf') return !!locator.range;
  return false;
}

export interface FeedbackState {
  source: ResolvedFeedbackSource | null; threads: FeedbackThread[]; history: FeedbackHistoryItem[];
  loading: boolean; error: string; open: boolean; mode: FeedbackMode; selected: string | null;
  candidate: FeedbackAnchorCandidate | null; draft: string; saving: boolean; interacting: boolean;
  next_cursor: string | null; review: FeedbackPreflight | null;
  view: 'threads' | 'review' | 'history' | 'access';
  pending_body: string | null;
}
export class FeedbackController {
  readonly presenter = new FeedbackPresenterSession();
  private presentationPort: FeedbackPresentationPort | null = null;
  private readonly presentationIdentity = crypto.randomUUID();
  private readonly stopPresentation = documentCoordination().onInvalidated(() => this.presenter.invalidate());
  readonly state = writable<FeedbackState>({ source: null, threads: [], history: [], loading: false, error: '', open: false,
    mode: 'view', selected: null, candidate: null, draft: '', saving: false, interacting: false, next_cursor: null, review: null, view: 'threads', pending_body: null });
  private lease: Pick<DocumentLease, 'assertCurrent'>;
  private drafts: FeedbackDrafts | null;
  private active = true;
  private remoteKey = '';
  private remoteVersion = 0;
  private saveTimer?: ReturnType<typeof setTimeout>;
  private draftQueue: Promise<void> = Promise.resolve();
  private readonly adapters = new Map<string, FeedbackAnnotationAdapter>();
  private readonly draftKey: string;
  private generation = 0;
  private creationKey = crypto.randomUUID();
  private textResolver: FeedbackSurfaceBinding['resolveTextScope'] | null = null;
  private pending: FeedbackDraftRecord['pending'] = null;
  private standaloneReady = false;
  constructor(readonly sourceRef: FeedbackSourceRef, readonly context: FeedbackContext, private standalone = false) {
    this.lease = standalone ? { assertCurrent: () => { if (!this.active) throw new Error('Feedback view changed.'); } } : documentCoordination().capture();
    this.drafts = standalone ? null : new FeedbackDrafts();
    this.draftKey = JSON.stringify({ source: sourceRef, context });
    this.restoreLocal();
  }
  private restoreLocal(): void {
    // Comments are saved or cancelled; there are no resumable drafts. Only an unconfirmed send
    // is recovered so it can be retried idempotently instead of being lost or duplicated.
    const record = this.drafts?.read(this.draftKey);
    if (!record?.pending) return;
    this.pending = record.pending;
    this.patch({ draft: record.body, candidate: record.locator ? { locator: record.locator } : null, pending_body: record.pending.request.body });
  }
  private localRecord(): FeedbackDraftRecord {
    const state = get(this.state);
    return { body: state.draft, locator: state.candidate?.locator ?? null, pending: this.pending };
  }
  private async persistLocal(): Promise<void> { await this.drafts?.write(this.draftKey, this.localRecord()); }
  patch(patch: Partial<FeedbackState>): void {
    this.lease.assertCurrent();
    const state = get(this.state);
    if (Object.entries(patch).some(([key, value]) => state[key as keyof FeedbackState] !== value)) {
      this.state.set({ ...state, ...patch });
    }
  }
  async load(): Promise<void> {
    const generation = ++this.generation;
    // A refresh keeps the current source so annotation layers and in-progress marks stay mounted;
    // only the first load starts without one.
    this.patch(get(this.state).source ? { error: '' } : { loading: true, error: '', source: null });
    try {
      if (this.standalone && !this.standaloneReady) {
        const manager = documentCoordination();
        await manager.initialize();
        const proofLease = manager.capture(false);
        const owner = await feedbackApi.owner();
        if (!this.active) return;
        await manager.verified(owner.email, proofLease);
        this.lease = manager.capture();
        this.drafts = new FeedbackDrafts();
        this.standaloneReady = true;
        this.restoreLocal();
      }
      const source = await feedbackApi.resolve(this.sourceRef, this.context);
      this.lease.assertCurrent();
      this.textResolver = createScopedTextResolver(
        { source: source.source, context: this.context, content_digest: source.content_digest },
        () => this.active && get(this.state).source ? {
          source: get(this.state).source!.source, context: this.context, content_digest: get(this.state).source!.content_digest,
        } : null,
        feedbackApi.text, () => this.lease.assertCurrent(),
      );
      const page = source.capabilities.read ? await feedbackApi.threads(source.subject_id) : { items: [], next_cursor: null };
      if (generation !== this.generation) return;
      this.patch({ source, threads: page.items, next_cursor: page.next_cursor, loading: false });
      if (!this.remoteKey) {
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(this.draftKey));
        this.remoteKey = `annotation-${Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')}`;
        try {
          // Read only the version so a stale server-side draft can be cleared on the next write.
          const saved = await feedbackApi.draft(this.remoteKey);
          this.lease.assertCurrent(); this.remoteVersion = saved.version;
        } catch (caught) {
          if (!(caught && typeof caught === 'object' && 'status' in caught && caught.status === 404)) this.fail(caught);
        }
      }
    } catch (caught) {
      // A failed revalidation withdraws the earlier source and its capabilities.
      if (generation === this.generation) { this.patch({ source: null }); this.fail(caught); }
    }
  }
  dispose(): void { this.stopPresentation(); this.presenter.invalidate(); this.active = false; this.generation++; this.adapters.clear(); clearTimeout(this.saveTimer); }
  fail(caught: unknown): void {
    try { this.lease.assertCurrent(); } catch { return; }
    this.patch({ loading: false, saving: false, error: caught instanceof Error ? caught.message : 'Feedback could not be loaded.' });
  }
  binding(state: FeedbackState): FeedbackSurfaceBinding | null {
    if (!state.source) return null;
    return {
      source: state.source, context: this.context, mode: state.mode,
      completeness: state.source.availability, supported_locator_kinds: state.source.permitted_locator_kinds,
      highlights: state.threads.map(thread => ({ thread_id: thread.thread_id, anchor_id: thread.anchor.anchor_id,
        locator: thread.anchor.locator, category: thread.anchor.content_digest === state.source?.content_digest ? 'exact' : 'not_located', label: thread.entries[0]?.body ?? 'Comment' })),
      selected_thread_id: state.selected, accepted_candidate: state.candidate, tool: get(this.presenter.state).tool,
      onTargetChange: target => {
        this.lease.assertCurrent();
        const live = get(this.state);
        const passive = !!target && selectionLocator(target.candidate.locator);
        if (!this.active || target && live.mode === 'view' && !passive || !live.source?.capabilities.comment
          || target && target.surface_id !== surfaceIdentity({ source: live.source, context: this.context })) return;
        this.presenter.publish(target);
      },
      resolveTextScope: (scope, page) => {
        if (!this.textResolver) return Promise.reject(new Error('Canonical text is not available yet.'));
        return this.textResolver(scope, page);
      },
      onAnchorCandidate: candidate => {
        this.lease.assertCurrent();
        const live = get(this.state);
        const passive = live.source?.source.kind === 'transcript' || live.source?.source.kind === 'work_diff'
          || selectionLocator(candidate.locator);
        if (!this.active || live.mode === 'view' && !passive || !live.source?.capabilities.comment || live.source.availability === 'unavailable'
          || state.source?.source_token !== live.source.source_token || !live.source.permitted_locator_kinds.includes(candidate.locator.kind)) return;
        if (get(this.state).draft || this.pending) { this.presenter.compose(); return; }
        this.patch({ candidate, interacting: true });
        this.presenter.compose();
        void this.persistLocal().catch(caught => this.fail(caught));
      },
      onSelectThread: selected => { this.patch({ selected }); if (selected) this.presenter.open('thread'); },
      onInteractionChange: interacting => { if (this.active) this.patch({ interacting }); },
      registerAdapter: (id, adapter) => {
        return registerPresenterAdapter(this.adapters, this.presenter, id, adapter);
      },
    };
  }
  presentation(): FeedbackPresentationPort {
    return this.presentationPort ??= {
      state: this.state, session: this.presenter, context: this.context,
      binding: () => this.binding(get(this.state)),
      authority: () => { this.lease.assertCurrent(); return this.presentationIdentity; },
      accept: candidate => this.binding(get(this.state))?.onAnchorCandidate(candidate),
      setMode: mode => { this.presenter.close(); this.patch({ mode, interacting: false }); },
      setDraft: body => this.setDraft(body), discard: () => this.discardDraft(), save: () => this.save(),
      // Comment mode belongs to the open comment list; closing the list returns to reading.
      closeRail: () => { this.presenter.close(); this.patch({ open: false, ...(get(this.state).mode === 'comment' ? { mode: 'view' as const } : {}) }); },
      commentWhole: () => {
        const live = get(this.state);
        if (!live.source?.permitted_locator_kinds.includes('whole')) return;
        if (live.mode === 'view') this.patch({ mode: 'comment' });
        this.binding(get(this.state))?.onAnchorCandidate({ locator: { kind: 'whole' } });
      },
      openRail: (view = 'threads') => { this.patch({ open: true, view }); this.presenter.open(view === 'threads' ? 'comments' : view); },
      select: selected => { this.patch({ selected }); if (selected) this.presenter.open('thread'); },
      editorHost: locator => {
        if (!this.active) return null;
        for (const adapter of Array.from(this.adapters.values()).reverse()) {
          const host = adapter.editorHost?.(locator);
          if (host?.isConnected && !host.closest('[inert], [hidden], [aria-hidden="true"]')) return host;
        }
        return null;
      },
      geometry: candidate => {
        for (const adapter of this.adapters.values()) {
          const geometry = adapter.targetGeometry?.(candidate.locator);
          if (geometry?.rects.length) return geometry;
        }
        return null;
      },
      reveal: async candidate => {
        for (const adapter of this.adapters.values()) {
          const result = await adapter.locate(candidate.locator);
          this.lease.assertCurrent();
          if (result.status === 'exact' || result.status === 'remapped') return;
        }
      },
      locate: async thread => { await this.locate(thread); }, more: () => this.more(),
      expose: async (candidate, visible) => {
        this.lease.assertCurrent();
        for (const adapter of this.adapters.values()) {
          if (!adapter.targetGeometry?.(candidate.locator)?.rects.length) continue;
          await adapter.exposeTarget?.(candidate.locator, visible);
          this.lease.assertCurrent(); return;
        }
      },
      reload: () => this.load(),
      reply: (thread, body) => this.reply(thread, body), edit: (entry, body) => this.edit(entry, body),
      resolve: thread => this.resolve(thread), moderate: (thread, reason) => this.moderate(thread, reason),
      reattach: (thread, reason) => this.reattach(thread, reason), capturePixels: id => this.capturePixels(id),
      loadHistory: () => this.loadHistory(),
    };
  }
  async discardDraft(): Promise<void> {
    if (this.pending || get(this.state).saving) return;
    this.patch({ draft: '', candidate: null, interacting: false });
    this.presenter.close();
    await this.persistLocal();
    clearTimeout(this.saveTimer);
    await this.persistRemoteDraft();
  }
  async more(): Promise<void> {
    const state = get(this.state);
    if (!state.source || !state.next_cursor) return;
    try {
      const page = await feedbackApi.threads(state.source.subject_id, state.next_cursor);
      this.patch({ threads: [...state.threads, ...page.items], next_cursor: page.next_cursor });
    } catch (caught) { this.fail(caught); }
  }
  setDraft(draft: string): void {
    this.patch({ draft });
    void this.persistLocal().catch(caught => this.fail(caught));
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => { void this.persistRemoteDraft().catch(caught => this.fail(caught)); }, 600);
  }
  private persistRemoteDraft(): Promise<void> {
    const state = get(this.state);
    if (!this.remoteKey || !state.source) return Promise.resolve();
    const source = state.source;
    const operation = this.draftQueue.then(async () => {
      this.lease.assertCurrent();
      const locator = state.candidate?.locator;
      const anchor = locator && !(locator.kind === 'html' && locator.shapes.length && !locator.capture_id)
        ? { schema_version: 1 as const, source_token: source.source_token, content_digest: source.content_digest, locator } : null;
      const result = await feedbackApi.saveDraft(this.remoteKey, { expected_version: this.remoteVersion,
        context: this.context, source: this.sourceRef, anchor, body: state.draft, selection: [], overall_message: null });
      this.lease.assertCurrent(); this.remoteVersion = result.version;
    });
    this.draftQueue = operation.catch(() => undefined);
    return operation;
  }
  async save(): Promise<void> {
    const state = get(this.state);
    if (!state.source || (!state.draft.trim() && !this.pending) || state.saving) return;
    this.patch({ saving: true, error: '' });
    try {
      if (!this.pending) {
        let locator = state.candidate?.locator ?? { kind: 'whole' as const };
        if (locator.kind === 'html' && locator.shapes.length && !locator.capture_id) {
          const capture = state.candidate?.capture;
          if (!capture) throw new Error('Confirm a screenshot of the observed HTML view before saving visual feedback.');
          locator = { ...locator, capture_id: await feedbackApi.adoptCapture(state.source, capture) };
        }
        this.lease.assertCurrent();
        this.pending = { original_locator: state.candidate?.locator ?? null, request: {
          creation_key: this.creationKey, context: this.context,
          anchor: { schema_version: 1, source_token: state.source.source_token, content_digest: state.source.content_digest, locator },
          body: state.draft, audience: 'internal' } };
        this.patch({ pending_body: state.draft });
      }
      await this.drafts?.pin(this.draftKey, this.localRecord());
      this.lease.assertCurrent();
      const pending = this.pending;
      const saved = await feedbackApi.createThread(pending.request);
      this.lease.assertCurrent();
      const current = get(this.state);
      if (current.draft === pending.request.body
        && JSON.stringify(current.candidate?.locator ?? null) === JSON.stringify(pending.original_locator)) {
        await this.drafts?.consume(this.draftKey, this.localRecord());
        this.patch({ draft: '', candidate: null, interacting: false });
      }
      this.pending = null; this.patch({ pending_body: null });
      if (saved.thread_id) this.patch({ selected: saved.thread_id });
      this.creationKey = crypto.randomUUID();
      await this.persistLocal();
      clearTimeout(this.saveTimer);
      await this.persistRemoteDraft();
      await this.load();
      if (typeof window !== 'undefined') window.dispatchEvent(new Event('feedback-changed'));
    } catch (caught) { this.fail(caught); }
    finally { if (this.active) { try { this.patch({ saving: false }); } catch { /* Account changed. */ } } }
  }
  async locate(thread: FeedbackThread): Promise<boolean> {
    this.lease.assertCurrent();
    const { source } = get(this.state);
    if (source?.capabilities.read && source.availability !== 'unavailable' && feedbackSourceState(thread.anchor, source) === 'current') {
      for (const adapter of this.adapters.values()) {
        const result = await adapter.locate(thread.anchor.locator);
        this.lease.assertCurrent();
        if (result.status === 'exact' || result.status === 'remapped') {
          const threads = get(this.state).threads;
          this.patch({ selected: thread.thread_id, error: '',
            ...(!threads.some(value => value.thread_id === thread.thread_id) ? { threads: [...threads, thread] } : {}) });
          this.presenter.open('thread');
          return true;
        }
      }
    }
    this.patch({ error: 'This anchor is not located in this view. Open the original revision to inspect it.' });
    return false;
  }
  async resolve(thread: FeedbackThread): Promise<void> {
    try { await feedbackApi.changeThread(thread, { state: thread.state === 'open' ? 'resolved' : 'open' }); await this.load(); }
    catch (caught) { this.fail(caught); }
  }
  async capturePixels(id: string): Promise<Blob> {
    this.lease.assertCurrent();
    const blob = await feedbackApi.capturePixels(id);
    this.lease.assertCurrent();
    return blob;
  }
  async moderate(thread: FeedbackThread, reason: string): Promise<void> {
    void reason;
    try { await feedbackApi.changeThread(thread, { hidden: !thread.hidden }); await this.load(); }
    catch (caught) { this.fail(caught); }
  }
  async reattach(thread: FeedbackThread, reason: string): Promise<void> {
    const state = get(this.state);
    if (!state.source || !state.candidate || !reason.trim()) return;
    try {
      let locator = state.candidate.locator;
      if (locator.kind === 'html' && locator.shapes.length && !locator.capture_id) {
        if (!state.candidate.capture) throw new Error('Confirm an observed screenshot before reattaching HTML geometry.');
        locator = { ...locator, capture_id: await feedbackApi.adoptCapture(state.source, state.candidate.capture) };
      }
      this.lease.assertCurrent();
      await feedbackApi.reattach(thread.thread_id, thread.version, { schema_version: 1, source_token: state.source.source_token,
        content_digest: state.source.content_digest, locator }, reason);
      await this.load();
    } catch (caught) { this.fail(caught); }
  }
  async reply(thread: FeedbackThread, body: string): Promise<void> {
    try { await feedbackApi.replyTo(thread, body, this.context); await this.load();
      if (typeof window !== 'undefined') window.dispatchEvent(new Event('feedback-changed')); }
    catch (caught) { this.fail(caught); throw caught; }
  }
  async edit(entry: FeedbackEntryVersion, body: string): Promise<void> {
    try { await feedbackApi.edit(entry.entry_id, entry.version, body, this.context); await this.load();
      if (typeof window !== 'undefined') window.dispatchEvent(new Event('feedback-changed')); }
    catch (caught) { this.fail(caught); throw caught; }
  }
  async loadHistory(): Promise<void> {
    const source = get(this.state).source;
    if (!source) return;
    try { this.patch({ history: (await feedbackApi.history(source.subject_id)).items }); }
    catch (caught) { this.fail(caught); }
  }
}

import { getContext, setContext, tick } from 'svelte';
import { get, writable } from 'svelte/store';
import type { FeedbackController } from './state';
import type { FeedbackSourceRef, FeedbackThread } from './types';
import { sameFeedbackSource } from './target-summary';
import { revealComment } from './locate-target';

const key = Symbol('comment-workspace');

/** Owns conversation-wide comment chrome, not source authority or draft lifetime. */
export class CommentWorkspace {
  constructor(readonly canComment: () => boolean = () => true) {}
  readonly state = writable<{ enabled: boolean; active: FeedbackController | null; list: boolean; revision: number }>({
    enabled: false, active: null, list: false, revision: 0,
  });
  private owners = new Map<FeedbackController, () => void>();
  private documentLocators = new Map<(thread: FeedbackThread) => Promise<boolean>, FeedbackSourceRef>();
  private scope = '';
  reset(scope: string): void {
    if (scope === this.scope) return;
    this.scope = scope;
    this.dispose();
  }
  dispose(): void {
    for (const stop of this.owners.values()) stop();
    this.owners.clear();
    this.documentLocators.clear();
    this.state.set({ enabled: false, active: null, list: false, revision: 0 });
  }
  controllers(): FeedbackController[] { return [...this.owners.keys()]; }
  register(owner: FeedbackController): () => void {
    if (this.owners.has(owner)) return () => {};
    const changed = () => this.state.update(state => ({ ...state, revision: state.revision + 1 }));
    const stopState = owner.state.subscribe(changed);
    const stopPresenter = owner.presenter.state.subscribe(session => {
      if (session.target || session.surface) this.claim(owner);
    });
    let stopped = false;
    const stop = () => { if (!stopped) { stopped = true; stopState(); stopPresenter(); } };
    this.owners.set(owner, stop);
    if (get(this.state).enabled) owner.patch({ mode: 'comment' });
    changed();
    return () => {
      stop();
      if (this.owners.get(owner) !== stop) return;
      this.owners.delete(owner);
      this.state.update(state => ({ ...state, active: state.active === owner ? null : state.active, revision: state.revision + 1 }));
    };
  }
  claim(owner: FeedbackController): void {
    const previous = get(this.state).active;
    if (previous === owner) return;
    this.state.update(state => ({ ...state, active: owner, list: false }));
    previous?.presenter.close();
  }
  setEnabled(enabled: boolean): void {
    if (enabled && !this.canComment()) return;
    this.state.update(state => ({ ...state, enabled }));
    for (const owner of this.owners.keys()) {
      owner.presentation().setMode(enabled ? 'comment' : 'view');
    }
  }
  /**
   * Showing the comments is comment mode, as on documents: rich blocks, tables and images in the
   * chat get the crosshair picker. Closing the list ends it again.
   */
  showComments(): void {
    get(this.state).active?.presenter.close();
    this.state.update(state => ({ ...state, list: true }));
    this.setEnabled(true);
  }
  closeComments(): void {
    this.hideList();
    if (get(this.state).enabled) this.setEnabled(false);
  }
  private hideList(): void { this.state.update(state => ({ ...state, list: false })); }
  private revealer: ((source: FeedbackSourceRef) => Promise<boolean>) | null = null;
  /** The conversation view loads and renders a commented source that is outside its window. */
  setRevealer(reveal: ((source: FeedbackSourceRef) => Promise<boolean>) | null): () => void {
    this.revealer = reveal;
    return () => { if (this.revealer === reveal) this.revealer = null; };
  }
  reveal(source: FeedbackSourceRef): Promise<boolean> { return this.revealer ? this.revealer(source) : Promise.resolve(false); }
  /** Document hosts stay lazy and keep their own comment mode, but expose exact navigation. */
  registerDocumentLocator(source: FeedbackSourceRef, locate: (thread: FeedbackThread) => Promise<boolean>): () => void {
    this.documentLocators.set(locate, source);
    return () => { this.documentLocators.delete(locate); };
  }
  /** Use the source's annotation adapter, including structural table targets, when mounted. */
  async locate(thread: FeedbackThread): Promise<boolean> {
    const source = thread.anchor.source;
    const documentLocators = () => [...this.documentLocators].filter(([, ref]) => sameFeedbackSource(ref, source));
    if (source.kind === 'document') {
      let locators = documentLocators();
      if (!locators.length && await this.reveal(source)) { await tick(); locators = documentLocators(); }
      for (const [locate] of locators) if (await locate(thread)) return true;
      // A document outline is not proof that its annotation was located.
      return false;
    }
    const ownerForSource = () => this.controllers().find(owner => sameFeedbackSource(owner.sourceRef, source));
    let owner = ownerForSource();
    if (!owner && await this.reveal(source)) {
      await tick();
      owner = ownerForSource();
    }
    if (owner) {
      if (!get(owner.state).source) { await owner.load(); await tick(); }
      return owner.locate(thread);
    }
    return revealComment({ source, locator: thread.anchor.locator });
  }
  resumeDraft(): void {
    const owner = get(this.state).active;
    if (!owner) return;
    const snapshot = get(owner.state);
    if (!snapshot.draft && !snapshot.pending_body && !snapshot.candidate) return;
    // The draft keeps its comment mode; only the list steps aside for the composer.
    this.hideList();
    owner.presenter.compose();
  }
}

export function provideCommentWorkspace(workspace: CommentWorkspace): void { setContext(key, workspace); }
export function getCommentWorkspace(): CommentWorkspace | undefined { return getContext(key); }

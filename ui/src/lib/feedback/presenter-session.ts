import { get, writable } from 'svelte/store';
import type { FeedbackAnnotationAdapter, FeedbackEntryVersion, FeedbackTargetView, FeedbackThread, FeedbackTool, FeedbackVisibleArea } from './types';
import type { FeedbackReviewSource } from './review-selection';

/** Client geometry only; the workspace owns the corresponding source layout. */
export interface FeedbackSourceLayout { visibleBottom: number; viewportHeight: number }
export type FeedbackFloatingView = 'compose' | 'thread' | 'comments' | 'review' | 'history' | 'access';

export class FeedbackPresenterSession {
  readonly state = writable<{ tool: FeedbackTool; target: FeedbackTargetView | null; composer: boolean; layout: FeedbackSourceLayout | null; bounds: FeedbackVisibleArea | null;
    surface: FeedbackFloatingView | null; preview: string | null; checked: FeedbackEntryVersion[]; reviewed: FeedbackEntryVersion[]; reviewedSources: FeedbackReviewSource[]; choices: string[] }>({
    tool: 'text', target: null, composer: false, layout: null, bounds: null,
    surface: null, preview: null, checked: [], reviewed: [], reviewedSources: [], choices: [],
  });
  private authority = '';
  private readonly buffers = new Map<string, string>();
  private readonly checkedSources = new Map<string, FeedbackReviewSource>();
  private returnFocus: HTMLElement | null = null;
  private restoringFocus = false;
  private focusRequest: (() => void) | null = null;
  setBounds(bounds: FeedbackVisibleArea | null): void {
    const current = get(this.state).bounds;
    if (current?.left !== bounds?.left || current?.top !== bounds?.top || current?.width !== bounds?.width || current?.height !== bounds?.height) {
      this.state.update(state => ({ ...state, bounds }));
    }
    if (bounds && this.focusRequest) {
      const request = this.focusRequest; this.focusRequest = null;
      if (bounds.width < 720) request();
    }
  }
  /** One request per explicit entry intent; exiting focus never re-arms it. */
  requestSourceFocus(request?: () => void): void {
    if (!request) return;
    this.focusRequest = request;
    const bounds = get(this.state).bounds;
    if (bounds) this.setBounds(bounds);
  }
  publish(target: FeedbackTargetView | null): void {
    if (!get(this.state).composer) this.state.update(state => ({ ...state, target, ...(target ? { surface: null, preview: null } : {}) }));
  }
  setTool(tool: FeedbackTool): void {
    this.state.update(state => ({ ...state, tool, target: null, preview: null }));
  }
  compose(): void {
    this.returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.state.update(state => ({ ...state, surface: 'compose', composer: true, target: null, preview: null }));
  }
  open(surface: Exclude<FeedbackFloatingView, 'compose'>): void {
    if (!get(this.state).surface) this.returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.state.update(state => ({ ...state, surface, composer: false, target: null, preview: null, layout: null, choices: [] }));
  }
  chooseThreads(ids: string[]): void {
    this.open('comments');
    this.state.update(state => ({ ...state, choices: [...ids] }));
  }
  preview(threadId: string | null, selecting = false): void {
    const state = get(this.state);
    if (state.surface || state.target || selecting || this.restoringFocus) return;
    if (state.preview !== threadId) this.state.set({ ...state, preview: threadId });
  }
  authorize(identity: string): void {
    if (identity === this.authority) return;
    if (this.authority) this.invalidate();
    this.authority = identity;
  }
  buffer(key: string): string { return this.buffers.get(JSON.stringify([this.authority, key])) ?? ''; }
  setBuffer(key: string, value: string): void {
    if (!this.authority) return;
    const id = JSON.stringify([this.authority, key]);
    if (value) this.buffers.set(id, value); else this.buffers.delete(id);
  }
  check(entry: FeedbackEntryVersion, checked: boolean, thread?: Pick<FeedbackThread, 'thread_id' | 'anchor'>): void {
    this.state.update(state => {
      if (!checked) {
        this.checkedSources.delete(entry.entry_version_id);
        return { ...state, checked: state.checked.filter(value => value.entry_version_id !== entry.entry_version_id) };
      }
      if (state.checked.some(value => value.entry_version_id === entry.entry_version_id)) return state;
      if (thread?.thread_id === entry.thread_id && thread.anchor?.schema_version === 1) {
        this.checkedSources.set(entry.entry_version_id, JSON.parse(JSON.stringify({
          entry_version_id: entry.entry_version_id, version: entry.version, thread_id: thread.thread_id,
          anchor_id: thread.anchor.anchor_id, source: thread.anchor.source,
        })) as FeedbackReviewSource);
      }
      return { ...state, checked: [...state.checked, JSON.parse(JSON.stringify(entry)) as FeedbackEntryVersion] };
    });
  }
  review(): void {
    this.open('review');
    this.state.update(state => ({ ...state, reviewed: [...state.checked],
      reviewedSources: state.checked.flatMap(entry => {
        const proof = this.checkedSources.get(entry.entry_version_id);
        return proof ? [JSON.parse(JSON.stringify(proof)) as FeedbackReviewSource] : [];
      }) }));
  }
  setLayout(layout: FeedbackSourceLayout | null): void {
    const current = get(this.state);
    if (!current.composer) layout = null;
    if (current.layout?.visibleBottom === layout?.visibleBottom && current.layout?.viewportHeight === layout?.viewportHeight) return;
    this.state.set({ ...current, layout });
  }
  close(restoreFocus = true): void {
    this.focusRequest = null;
    this.state.update(state => ({ ...state, surface: null, preview: null, composer: false, target: null, layout: null }));
    this.restoringFocus = true;
    try { if (restoreFocus && this.returnFocus?.isConnected) this.returnFocus.focus({ preventScroll: true }); }
    finally { this.restoringFocus = false; }
    this.returnFocus = null;
  }
  invalidate(): void {
    this.close(false); this.setBounds(null); this.buffers.clear(); this.authority = '';
    this.checkedSources.clear();
    this.state.update(state => ({ ...state, checked: [], reviewed: [], reviewedSources: [] }));
  }
}

/** Prop replacement re-registers synchronously; actual teardown revokes its target. */
export function registerPresenterAdapter(adapters: Map<string, FeedbackAnnotationAdapter>, session: FeedbackPresenterSession,
  id: string, adapter: FeedbackAnnotationAdapter): () => void {
  adapters.set(id, adapter);
  return () => {
    if (adapters.get(id) !== adapter) return;
    adapters.delete(id);
    queueMicrotask(() => {
      if (!adapters.has(id) && get(session.state).target?.instance_id === id) session.publish(null);
    });
  };
}

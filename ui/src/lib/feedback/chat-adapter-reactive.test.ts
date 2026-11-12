import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import ChatReactiveFeedbackHarness from '$lib/components/feedback/ChatReactiveFeedbackHarness.svelte';
import { renderMarkdown } from '$lib/markdown';
import type { MessageTimelineItem, TimelineScope } from '$lib/chat-v2/types';
import { FeedbackController } from './state';
import { chatTranscriptSource } from './chat-transcript-source';
import { mountChatTextAdapter } from './chat-text-adapter';
import { textDigest } from './annotations/digest';
import type { FeedbackSurfaceBinding, ResolvedFeedbackSource } from './types';

const deferred = vi.hoisted(() => ({
  gate: Promise.resolve() as Promise<void>, entered: vi.fn(), epoch: 0,
  invalidationListeners: new Set<() => void>(),
}));
vi.mock('$lib/markdown', async importOriginal => {
  const actual = await importOriginal<typeof import('$lib/markdown')>();
  return { ...actual, mapRenderedMarkdown: async (source: string) => {
    deferred.entered();
    await deferred.gate;
    return actual.mapRenderedMarkdown(source);
  } };
});
vi.mock('$lib/document-coordination', () => ({
  documentCoordination: () => ({ capture: () => {
    const epoch = deferred.epoch;
    return { assertCurrent: () => { if (epoch !== deferred.epoch) throw new Error('Authority changed'); } };
  }, onInvalidated: (listener: () => void) => {
    deferred.invalidationListeners.add(listener);
    return () => { deferred.invalidationListeners.delete(listener); };
  } }),
}));
const sourceText = '# Staging review\n\n🧭 Prepare a **staging-only** rollout plan. Protect production data and name the rollback owner.\n\nKeep approval separate from execution.';
let release = () => {};
let mounted: ReturnType<typeof mountChatTextAdapter> | undefined;
let controller: FeedbackController | undefined;
const rects = Object.getOwnPropertyDescriptor(Range.prototype, 'getClientRects');
afterEach(() => {
  release(); cleanup(); mounted?.destroy(); mounted = undefined; controller?.dispose(); controller = undefined;
  expect(deferred.invalidationListeners.size).toBe(0);
  document.getSelection()?.removeAllRanges(); document.body.replaceChildren();
  if (rects) Object.defineProperty(Range.prototype, 'getClientRects', rects);
  else Reflect.deleteProperty(Range.prototype, 'getClientRects');
  deferred.entered.mockClear(); deferred.gate = Promise.resolve();
});
function pause() {
  deferred.gate = new Promise<void>(resolve => { release = resolve; });
}
function invalidateCoordination() {
  deferred.epoch++;
  for (const listener of deferred.invalidationListeners) listener();
}
async function fixture() {
  const scope: TimelineScope = { key: crypto.randomUUID(), kind: 'conversation', conversation_id: crypto.randomUUID() };
  const item: MessageTimelineItem = { id: crypto.randomUUID(), message_id: crypto.randomUUID(), kind: 'message', role: 'assistant',
    status: null, text_streaming: null, stable: true, partial: false, content: sourceText, attachments: [], created_at: null,
    sort_key: crypto.randomUUID(), source_refs: [{ store: 'intaris', session_id: crypto.randomUUID(), seq: 9,
      event_type: 'assistant_message', event_id: null }] };
  const source = chatTranscriptSource(item, scope)!;
  const digest = await textDigest(sourceText);
  const resolved: ResolvedFeedbackSource = { source, source_token: crypto.randomUUID(), content_digest: digest, subject_id: crypto.randomUUID(),
    context: { kind: 'chat', conversation_id: scope.conversation_id! }, availability: 'available',
    capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false }, permitted_locator_kinds: ['text'],
    canonical_text: { representation: 'feedback-text-v1', text_digest: digest, length: sourceText.length, available_ranges: [[0, sourceText.length]] },
    lineage_id: null, revision: null, latest_content_ref: null, title: null, format: 'markdown' };
  controller = new FeedbackController(source, resolved.context, true);
  controller.patch({ source: resolved });
  return { item, scope, resolved, owner: controller };
}
function nativeSelection(root: HTMLElement) {
  const strong = root.querySelector('strong')!, after = strong.nextSibling!;
  const range = document.createRange(); range.setStart(strong.firstChild!, 0); range.setEnd(after, 8);
  document.getSelection()!.removeAllRanges(); document.getSelection()!.addRange(range);
  document.dispatchEvent(new Event('selectionchange'));
  root.dispatchEvent(new Event('pointerup', { bubbles: true }));
  expect(range.toString()).toBe('staging-only rollout');
}

describe('chat adapter reactive owner lifetime', () => {
  it.each(['button', 'copy-exclude'] as const)(
    'maps paragraph element boundaries when an unrelated %s exists', async control => {
      const { owner } = await fixture();
      const root = document.createElement('div');
      root.innerHTML = renderMarkdown(sourceText);
      const chrome = document.createElement(control === 'button' ? 'button' : 'span');
      if (control === 'copy-exclude') chrome.dataset.copyExclude = '';
      chrome.textContent = 'Copy renderer control';
      root.prepend(chrome);
      document.body.append(root);
      const binding = owner.binding(get(owner.state))!, target = vi.fn(), error = vi.fn();
      mounted = mountChatTextAdapter(root, sourceText, { ...binding, onTargetChange: target }, error);
      const paragraph = root.querySelector('p')!;
      const range = document.createRange();
      range.selectNodeContents(paragraph);
      document.getSelection()!.addRange(range);
      document.dispatchEvent(new Event('selectionchange'));
      root.dispatchEvent(new Event('pointerup', { bubbles: true }));
      await waitFor(() => expect(target.mock.calls.some(([value]) => value !== null)).toBe(true));
      const selected = target.mock.calls.findLast(([value]) => value !== null)![0];
      expect(selected.candidate.locator.range.exact).toBe(
        '🧭 Prepare a **staging-only** rollout plan. Protect production data and name the rollback owner.',
      );
      expect(error.mock.calls.filter(([message]) => message)).toEqual([]);
    },
  );

  it('never turns a selection of a renderer control into message text', async () => {
    const { owner } = await fixture();
    const root = document.createElement('div');
    root.innerHTML = renderMarkdown(sourceText);
    const button = document.createElement('button');
    button.textContent = 'Copy';
    root.prepend(button);
    document.body.append(root);
    const binding = owner.binding(get(owner.state))!, target = vi.fn(), error = vi.fn();
    mounted = mountChatTextAdapter(root, sourceText, { ...binding, onTargetChange: target }, error);
    const range = document.createRange();
    range.selectNodeContents(button);
    document.getSelection()!.addRange(range);
    document.dispatchEvent(new Event('selectionchange'));
    root.dispatchEvent(new Event('pointerup', { bubbles: true }));
    await waitFor(() => expect(error).toHaveBeenCalledWith('Select only message text, not controls.'));
    expect(target.mock.calls.some(([value]) => value !== null)).toBe(false);
  });

  it('delivers Add comment through actual controller snapshots during async mapping, without registration churn', async () => {
    const { item, scope, owner } = await fixture();
    const bindings: FeedbackSurfaceBinding[] = [], actual = owner.binding.bind(owner);
    let registrations = 0, removals = 0;
    vi.spyOn(owner, 'binding').mockImplementation(state => {
      const binding = actual(state);
      if (!binding) return null;
      bindings.push(binding);
      const register = binding.registerAdapter;
      return { ...binding, registerAdapter: (id, adapter) => {
        registrations++; const unregister = register(id, adapter); return () => { removals++; unregister(); };
      } };
    });
    Object.defineProperty(Range.prototype, 'getClientRects', { configurable: true,
      value: () => [{ left: 20, top: 60, width: 160, height: 20 }] });
    pause();
    const view = render(ChatReactiveFeedbackHarness, { controller: owner, item, scope });
    const root = view.container.querySelector<HTMLElement>('.chat-markdown')!;
    expect(root.textContent).toHaveLength(147);
    await waitFor(() => expect(deferred.entered).toHaveBeenCalled());
    nativeSelection(root);
    await waitFor(() => expect(get(owner.state).interacting).toBe(true));
    expect(bindings.length).toBeGreaterThan(1);
    expect(registrations).toBe(0);
    release();
    await waitFor(() => expect(get(owner.presenter.state).target?.label).toBe('staging-only rollout'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add comment' })).toBeVisible());
    expect(document.getSelection()!.toString()).toBe('staging-only rollout');
    expect(registrations).toBe(1); expect(removals).toBe(0);
    owner.patch({ interacting: true });
    await waitFor(() => expect(bindings.length).toBeGreaterThan(2));
    expect(registrations).toBe(1); expect(removals).toBe(0);
    const invalidatePresentation = vi.spyOn(owner.presenter, 'invalidate');
    invalidateCoordination();
    expect(invalidatePresentation).toHaveBeenCalledOnce();
    expect(get(owner.presenter.state).target).toBeNull();
    view.unmount();
    expect(removals).toBe(1);
    owner.dispose();
    expect(deferred.invalidationListeners.size).toBe(0);
    const callsAfterDisposal = invalidatePresentation.mock.calls.length;
    invalidateCoordination();
    expect(invalidatePresentation).toHaveBeenCalledTimes(callsAfterDisposal);
  });

  it.each(['source', 'context', 'capability', 'descriptor', 'available-ranges', 'account', 'mode', 'tool',
    'null-binding', 'undefined-binding', 'same-text-dom', 'changed-text', 'dispose'] as const)(
    'cancels pending mapping and publication after %s replacement', async change => {
      const { owner } = await fixture();
      owner.patch({ mode: 'comment' });
      const root = document.createElement('div'); root.innerHTML = renderMarkdown(sourceText); document.body.append(root);
      const binding = owner.binding(get(owner.state))!;
      const targets = vi.fn(), registered = vi.fn(() => () => {}), error = vi.fn();
      const initial = { ...binding, onTargetChange: targets, onInteractionChange: vi.fn(), registerAdapter: registered };
      const next = { ...initial, registerAdapter: vi.fn(() => () => {}) };
      pause();
      mounted = mountChatTextAdapter(root, sourceText, initial, error);
      nativeSelection(root);
      await waitFor(() => expect(deferred.entered).toHaveBeenCalled());
      if (change === 'source') mounted.update({ ...next, source: { ...initial.source, source_token: crypto.randomUUID() } });
      if (change === 'context') mounted.update({ ...next, context: { kind: 'chat', conversation_id: crypto.randomUUID() } });
      if (change === 'capability') mounted.update({ ...next, source: { ...initial.source, capabilities: { ...initial.source.capabilities, comment: false } } });
      if (change === 'descriptor') mounted.update({ ...next, source: { ...initial.source, canonical_text: { ...initial.source.canonical_text!, text_digest: crypto.randomUUID() } } });
      if (change === 'available-ranges') mounted.update({ ...next, source: { ...initial.source, canonical_text: { ...initial.source.canonical_text!, available_ranges: [] } } });
      if (change === 'account') invalidateCoordination();
      if (change === 'mode') mounted.update({ ...initial, mode: 'view' });
      if (change === 'tool') mounted.update({ ...initial, tool: 'area' });
      if (change === 'null-binding') mounted.update(null);
      if (change === 'undefined-binding') mounted.update(undefined);
      if (change === 'same-text-dom') root.innerHTML = renderMarkdown(sourceText);
      if (change === 'changed-text') root.textContent = 'Changed source';
      if (change === 'dispose') { mounted.destroy(); root.remove(); }
      release(); await vi.dynamicImportSettled();
      await waitFor(() => {
        expect(targets.mock.calls.some(([target]) => target !== null)).toBe(false);
        if (change !== 'mode' && change !== 'tool') expect(registered).not.toHaveBeenCalled();
      });
    },
  );
});

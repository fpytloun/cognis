import { afterEach, describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/svelte';
import { renderMarkdown } from '$lib/markdown';
import { mountChatTextAdapter } from './chat-text-adapter';
import { chatSelectionIntent } from './chat-selection-intent';
import { lazyChatFeedback } from './lazy-chat-feedback';
import { textDigest } from './annotations/digest';
import { selectText } from './annotations/text';
import { chatTranscriptSource } from './chat-transcript-source';
import type { MessageTimelineItem, SourceRef } from '$lib/chat-v2/types';
import type { FeedbackAnnotationAdapter, FeedbackSurfaceBinding, FeedbackTargetView } from './types';
import type { FeedbackSurfaceProvider } from './context';
const authority = vi.hoisted(() => ({ current: 0 }));
vi.mock('$lib/document-coordination', () => ({
  documentCoordination: () => ({ capture: () => {
    const version = authority.current;
    return { assertCurrent: () => { if (version !== authority.current) throw new Error('Account changed'); } };
  } }),
}));

async function fixture(text: string) {
  const source = text.replace(/\r\n?/g, '\n');
  const binding: FeedbackSurfaceBinding = {
    source: { subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(),
      source: { kind: 'transcript', conversation_id: crypto.randomUUID(), source_item_id: crypto.randomUUID(),
        source_event: { store: 'intaris', session_id: crypto.randomUUID(), seq: 1, event_type: 'message' }, field_path: ['content'] },
      content_digest: await textDigest(source), canonical_text: { representation: 'feedback-text-v1',
        text_digest: await textDigest(source), length: source.length, available_ranges: [[0, source.length]] },
      context: { kind: 'standalone' }, lineage_id: null, revision: null, latest_content_ref: null, availability: 'available',
      permitted_locator_kinds: ['text'], title: null, format: 'transcript',
      capabilities: { read: true, comment: true, dispatch: false, moderate: false, disclose: false } },
    context: { kind: 'standalone' }, mode: 'comment', completeness: 'available', supported_locator_kinds: ['text'],
    highlights: [], selected_thread_id: null, onTargetChange: vi.fn(), onAnchorCandidate: vi.fn(),
    onSelectThread: vi.fn(), onInteractionChange: vi.fn(), resolveTextScope: vi.fn(),
    registerAdapter: vi.fn(() => () => {}),
  };
  const root = document.createElement('div'); root.innerHTML = renderMarkdown(text); document.body.append(root);
  return { root, binding };
}
function select(start: Node, from: number, end: Node = start, to = from + 1) {
  const range = document.createRange(); range.setStart(start, from); range.setEnd(end, to);
  const selection = document.getSelection()!; selection.removeAllRanges(); selection.addRange(range);
  document.dispatchEvent(new Event('selectionchange'));
  return range;
}
function published(binding: FeedbackSurfaceBinding) {
  return vi.mocked(binding.onTargetChange!).mock.calls.map(([value]) => value).filter(Boolean) as FeedbackTargetView[];
}
afterEach(() => { document.getSelection()?.removeAllRanges(); document.body.replaceChildren(); vi.restoreAllMocks(); });

describe('chat transcript native adapter', () => {
  it('registers immediately usable saved-target geometry when opened without a native selection', async () => {
    const text = 'Before **saved target** and [link](https://host.test)';
    const { root, binding } = await fixture(text);
    const start = text.indexOf('saved'), end = start + 'saved target'.length;
    const locator = { kind: 'text' as const, range: { representation: 'feedback-text-v1' as const,
      text_digest: binding.source.canonical_text!.text_digest, ...selectText(text, start, end) } };
    const rects = vi.fn(() => [{ left: 12, top: 24, width: 80, height: 18 }]);
    const original = Object.getOwnPropertyDescriptor(Range.prototype, 'getClientRects');
    Object.defineProperty(Range.prototype, 'getClientRects', { configurable: true, value: rects });
    const unregister = vi.fn();
    let registered: FeedbackAnnotationAdapter | undefined;
    vi.mocked(binding.registerAdapter).mockImplementation((_instance, adapter) => {
      registered = adapter;
      expect(adapter.targetGeometry?.(locator)).toEqual({ space: 'client', rects: [{ left: 12, top: 24, width: 80, height: 18 }] });
      return unregister;
    });
    const surface: FeedbackSurfaceProvider = { binding, available: true, open: vi.fn(), commenting: vi.fn() };
    const action = lazyChatFeedback(root, { surface, binding, key: crypto.randomUUID(), enabled: true, text });
    try {
      await waitFor(() => expect(registered).toBeDefined());
      expect(document.getSelection()?.toString()).toBe('');
      expect(published(binding)).toHaveLength(0);
      root.querySelector('strong')!.scrollIntoView = vi.fn();
      expect(await registered!.locate(locator)).toEqual({ status: 'exact' });
      expect(root.querySelector('strong')!.scrollIntoView).toHaveBeenCalledOnce();
      authority.current++;
      expect(registered!.targetGeometry?.(locator)).toBeNull();
    } finally {
      action.destroy();
      expect(unregister).toHaveBeenCalledOnce();
      if (original) Object.defineProperty(Range.prototype, 'getClientRects', original);
      else Reflect.deleteProperty(Range.prototype, 'getClientRects');
    }
  });
  it.each(['source', 'context', 'account', 'unmount'] as const)('does not register pending saved geometry after %s replacement', async change => {
    const text = 'Saved **target**', { root, binding } = await fixture(text);
    const error = vi.fn(), action = mountChatTextAdapter(root, text, binding, error);
    const next = { ...binding, registerAdapter: vi.fn(() => () => {}), source: { ...binding.source, source_token: crypto.randomUUID() } };
    if (change === 'source') action.update(next);
    if (change === 'context') action.update({ ...next, context: { kind: 'chat', conversation_id: crypto.randomUUID() } });
    if (change === 'account') authority.current++;
    if (change === 'unmount') { action.destroy(); root.remove(); }
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(binding.registerAdapter).not.toHaveBeenCalled();
    if (change === 'source' || change === 'context') expect(next.registerAdapter).toHaveBeenCalledOnce();
    action.destroy();
  });
  it('rejects a span crossing an image even when it contributes no rendered text', async () => {
    const text = 'Before ![hidden description](https://host.test/image.png) after';
    const { root, binding } = await fixture(text);
    const error = vi.fn(), action = mountChatTextAdapter(root, text, binding, error);
    const paragraph = root.querySelector('p')!;
    select(paragraph.firstChild!, 0, paragraph.lastChild!, 6);
    await waitFor(() => expect(error).toHaveBeenCalledWith(expect.stringContaining('unmapped')));
    expect(published(binding)).toHaveLength(0);
    action.destroy();
  });
  it('trims rendered-only edges such as list spacing instead of rejecting the selection', async () => {
    const text = '- First item text\n- Second item text';
    const { root, binding } = await fixture(text);
    const errors = vi.fn(), adapter = mountChatTextAdapter(root, text, binding, errors);
    const items = root.querySelectorAll('li');
    // From inside the first item to the very start of the second: the trailing edge is the
    // rendered gap between items, which has no source characters of its own.
    select(items[0].firstChild!, 6, items[1].firstChild!, 0);
    await waitFor(() => expect(published(binding)).toHaveLength(1));
    const locator = published(binding)[0].candidate.locator;
    if (locator.kind !== 'text') throw new Error('Expected text');
    expect(locator.range.exact).toBe('item text');
    expect(errors).not.toHaveBeenCalledWith(expect.stringMatching(/precise|Select message text/));
    adapter.destroy();
  });
  it('maps a mixed-format selection to the minimal raw span and reveals the same DOM target', async () => {
    const text = '- First **bold** and [label](https://host.test) with `code` 😀\r\n- Next';
    const { root, binding } = await fixture(text);
    const errors = vi.fn(), adapter = mountChatTextAdapter(root, text, binding, errors);
    const start = root.querySelector('strong')!.firstChild!, end = root.querySelector('code')!.firstChild!;
    select(start, 1, end, 3);
    await waitFor(() => expect(published(binding)).toHaveLength(1));
    const target = published(binding)[0], locator = target.candidate.locator;
    expect(locator.kind).toBe('text');
    if (locator.kind !== 'text') throw new Error('Expected text');
    expect(locator.range.exact).toBe('old** and [label](https://host.test) with `cod');
    expect(text.slice(locator.range.start, locator.range.end)).toBe(locator.range.exact);
    const registered = vi.mocked(binding.registerAdapter).mock.calls.at(-1)![1] as FeedbackAnnotationAdapter;
    start.parentElement!.scrollIntoView = vi.fn();
    expect(await registered.locate(locator)).toEqual({ status: 'exact' });
    expect(start.parentElement!.scrollIntoView).toHaveBeenCalled();
    expect(errors).not.toHaveBeenCalledWith(expect.stringMatching(/unmapped|ambiguous/));
    adapter.destroy();
  });
  it('uses exact repeated phrase positions and refuses user-interface controls', async () => {
    const text = 'same **same** `same`';
    const { root, binding } = await fixture(text);
    const action = mountChatTextAdapter(root, text, binding);
    select(root.querySelector('strong')!.firstChild!, 0, root.querySelector('strong')!.firstChild!, 4);
    await waitFor(() => expect(published(binding)).toHaveLength(1));
    const locator = published(binding)[0].candidate.locator;
    expect(locator.kind === 'text' && locator.range.start).toBe(7);
    const button = document.createElement('button'); button.textContent = 'Copy'; root.append(button);
    select(button.firstChild!, 0, button.firstChild!, 4);
    await new Promise<void>(resolve => queueMicrotask(resolve));
    await waitFor(() => expect(vi.mocked(binding.onTargetChange!).mock.calls.at(-1)?.[0]).toBeNull());
    expect(published(binding)).toHaveLength(1);
    action.destroy();
  });
  it.each(['authority', 'unmount', 'content'] as const)('cancels pending mapping after %s replacement', async change => {
    const { root, binding } = await fixture('Selected **text**');
    const action = mountChatTextAdapter(root, 'Selected **text**', binding);
    select(root.querySelector('strong')!.firstChild!, 0, root.querySelector('strong')!.firstChild!, 4);
    await new Promise<void>(resolve => queueMicrotask(resolve));
    if (change === 'authority') action.update({ ...binding, source: { ...binding.source,
      source_token: crypto.randomUUID(), capabilities: { ...binding.source.capabilities, comment: false } } });
    if (change === 'unmount') { action.destroy(); root.remove(); }
    if (change === 'content') root.textContent = 'replacement';
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(published(binding)).toHaveLength(0);
    action.destroy();
  });
});

describe('passive chat intent', () => {
  it.each(['multi-append', 'replacement then delta', 'null-status persisted message'])('preserves selection while the full %s descriptor resolves', async variant => {
    const text = 'First **second** third', { root, binding: initialBinding } = await fixture(text);
    const persisted = variant === 'null-status persisted message';
    const refs: SourceRef[] = [7, 8, 9].map(seq => ({ store: 'intaris', session_id: crypto.randomUUID(),
      seq, event_id: persisted ? null : crypto.randomUUID(), event_type: 'assistant_message' }));
    const item: MessageTimelineItem = { kind: 'message', role: 'assistant', id: crypto.randomUUID(), content: text,
      status: persisted ? null : 'complete', stable: true, partial: false, text_streaming: false, source_refs: refs, sort_key: crypto.randomUUID(),
      created_at: null, message_id: crypto.randomUUID(), attachments: [] };
    const source = chatTranscriptSource(item, { key: crypto.randomUUID(), kind: 'conversation', conversation_id: crypto.randomUUID() })!;
    const binding = { ...initialBinding, source: { ...initialBinding.source, source } };
    const surface: FeedbackSurfaceProvider = { binding: null, available: true, open: vi.fn(), commenting: vi.fn() };
    const options = { surface, binding: null, key: JSON.stringify(source), enabled: true, text };
    const action = lazyChatFeedback(root, options), node = root.querySelector('strong')!.firstChild!;
    const range = select(node, 0, node, 6);
    await waitFor(() => expect(surface.commenting).toHaveBeenCalledOnce());
    expect(document.getSelection()!.toString()).toBe(range.toString());
    expect(binding.source.source).toMatchObject({ representation: 'chat-item-as-of-event-v1',
      source_event: { ...refs[2], event_id: refs[2].event_id ?? undefined } });
    action.update({ ...options, binding });
    await waitFor(() => expect(published(binding)).toHaveLength(1));
    const locator = published(binding)[0].candidate.locator;
    expect(locator.kind === 'text' && locator.range.exact).toBe('second');
    expect(document.getSelection()!.toString()).toBe('second');
    action.destroy();
  });
  it('loads the real lazy adapter on selection and preserves the range through delayed binding readiness', async () => {
    const text = 'Before **bold** after', { root, binding } = await fixture(text);
    const surface: FeedbackSurfaceProvider = { binding: null, available: true, open: vi.fn(), commenting: vi.fn() };
    const options = { surface, binding: null, key: crypto.randomUUID(), enabled: true, text };
    const action = lazyChatFeedback(root, options);
    const node = root.querySelector('strong')!.firstChild!;
    select(node, 0, node, 4);
    await waitFor(() => expect(surface.commenting).toHaveBeenCalledTimes(1));
    expect(binding.registerAdapter).not.toHaveBeenCalled();
    action.update({ ...options, binding });
    await waitFor(() => expect(published(binding)).toHaveLength(1));
    expect(document.getSelection()!.toString()).toBe('bold');
    action.destroy();
  });
  it('cancels the initial lazy import across account invalidation', async () => {
    const { root } = await fixture('Native selection');
    const surface: FeedbackSurfaceProvider = { binding: null, available: true, open: vi.fn(), commenting: vi.fn() };
    const action = lazyChatFeedback(root, { surface, binding: null, key: crypto.randomUUID(), enabled: true, text: 'Native selection' });
    const node = root.querySelector('p')!.firstChild!;
    select(node, 0, node, 6);
    authority.current++;
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(surface.commenting).not.toHaveBeenCalled();
    action.destroy();
  });
  it('waits for binding readiness without changing selection, then hands off once', async () => {
    const { root, binding } = await fixture('Native selection');
    const surface: FeedbackSurfaceProvider = { binding: null, available: true, open: vi.fn(), commenting: vi.fn() };
    const options = { surface, binding: null, key: crypto.randomUUID(), enabled: true };
    const action = chatSelectionIntent(root, options);
    const range = select(root.querySelector('p')!.firstChild!, 0, root.querySelector('p')!.firstChild!, 6);
    expect(surface.commenting).toHaveBeenCalledTimes(1);
    const observe = vi.fn(); root.addEventListener('pointerup', observe);
    action.update({ ...options, binding });
    await new Promise<void>(resolve => queueMicrotask(resolve));
    expect(observe).toHaveBeenCalledTimes(1);
    expect(document.getSelection()!.toString()).toBe(range.toString());
    action.destroy();
  });
  it('does not replay stale selection, context or unmounted roots', async () => {
    const { root, binding } = await fixture('Native selection');
    const surface: FeedbackSurfaceProvider = { binding: null, available: true, open: vi.fn(), commenting: vi.fn() };
    const options = { surface, binding: null, key: crypto.randomUUID(), enabled: true };
    const action = chatSelectionIntent(root, options), observe = vi.fn(); root.addEventListener('pointerup', observe);
    const node = root.querySelector('p')!.firstChild!;
    select(node, 0, node, 6);
    action.update({ ...options, binding, key: crypto.randomUUID() });
    await new Promise<void>(resolve => queueMicrotask(resolve));
    expect(observe).not.toHaveBeenCalled();
    action.destroy();
  });
  it('does not hand off an intent captured before an account fence changed', async () => {
    const { root, binding } = await fixture('Native selection');
    const surface: FeedbackSurfaceProvider = { binding: null, available: true, open: vi.fn(), commenting: vi.fn() };
    const options = { surface, binding: null, key: crypto.randomUUID(), enabled: true };
    const action = chatSelectionIntent(root, options), observe = vi.fn(); root.addEventListener('pointerup', observe);
    const node = root.querySelector('p')!.firstChild!;
    select(node, 0, node, 6);
    authority.current++;
    action.update({ ...options, binding });
    await new Promise<void>(resolve => queueMicrotask(resolve));
    expect(observe).not.toHaveBeenCalled();
    action.destroy();
  });
  it('assigns nested native selection and table intent only to the nearest source, without preventing gestures', async () => {
    const outer = document.createElement('div'), inner = document.createElement('div');
    inner.innerHTML = '<div data-feedback-rich-block="table"><table><tbody><tr><td>Native text</td></tr></tbody></table></div>';
    outer.append(inner); document.body.append(outer);
    const surface = (): FeedbackSurfaceProvider => ({ binding: null, available: true, open: vi.fn(), commenting: vi.fn() });
    const a = surface(), b = surface();
    const one = chatSelectionIntent(outer, { surface: a, binding: null, key: crypto.randomUUID(), enabled: true });
    const two = chatSelectionIntent(inner, { surface: b, binding: null, key: crypto.randomUUID(), enabled: true });
    const cell = inner.querySelector('td')!;
    const event = new Event('pointerover', { bubbles: true, cancelable: true }); cell.dispatchEvent(event);
    select(cell.firstChild!, 0, cell.firstChild!, 6);
    expect(b.commenting).toHaveBeenCalledTimes(1); expect(a.commenting).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
    const outside = document.createTextNode('Outside'); outer.append(outside);
    select(cell.firstChild!, 0, outside, 3);
    expect(a.commenting).not.toHaveBeenCalled();
    one.destroy(); two.destroy();
  });
});

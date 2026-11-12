import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FeedbackAnnotationAdapter, FeedbackSurfaceBinding, ScopedCanonicalFeedbackText } from '$lib/feedback/types';
import FileDiffViewer from '../../FileDiffViewer.svelte';
import WorkFileTree from '../../work/WorkFileTree.svelte';
import type { WorkFileDiff } from '$lib/work/fileTree';
import AnnotationHarness from './AnnotationHarness.svelte';
import { textDigest } from '$lib/feedback/annotations/digest';
vi.mock('$lib/feedback/annotations/digest', () => ({ textDigest: vi.fn(async () => 'hunk-hash') }));
function binding(): FeedbackSurfaceBinding {
  return { source: { subject_id: 'review-document', source_token: 'same-source-token', source: { kind: 'document', content_ref: 'art_review' }, content_digest: 'source-hash',
    availability: 'available', permitted_locator_kinds: ['rich', 'diff', 'image'], context: { kind: 'chat', conversation_id: 'origin-chat' }, lineage_id: null, revision: 1, latest_content_ref: null,
    capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false }, title: 'Review', format: 'text', canonical_text: null },
    context: { kind: 'chat', conversation_id: 'origin-chat' }, mode: 'comment', tool: 'element', supported_locator_kinds: ['rich', 'diff', 'image'], completeness: 'available', highlights: [], selected_thread_id: null,
    resolveTextScope: vi.fn(), onTargetChange: vi.fn(), onAnchorCandidate: vi.fn(), onSelectThread: vi.fn(), onInteractionChange: vi.fn(), registerAdapter: vi.fn(() => () => {}) };
}
function adapter(value: FeedbackSurfaceBinding): FeedbackAnnotationAdapter {
  return vi.mocked(value.registerAdapter).mock.calls.at(-1)![1];
}
const diffs = [{ path: 'review.ts', diff: '@@ -1 +1 @@\n-old\n+new\n' }];
async function selectedDiff(value: FeedbackSurfaceBinding) {
  const view = render(FileDiffViewer, { diffs, annotationBindings: [value] });
  await fireEvent.click(screen.getByLabelText('Select new line 1'));
  return view;
}
function selectRich() {
  const node = screen.getByTestId('rich-rendered-text');
  const range = document.createRange(); range.setStart(node.firstChild!, 0); range.setEnd(node.firstChild!, 8);
  window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
  return fireEvent.pointerUp(node);
}
beforeEach(() => { vi.mocked(textDigest).mockReset().mockResolvedValue('hunk-hash'); });
afterEach(() => { vi.restoreAllMocks(); });
describe('independent review regression coverage', () => {
  it.each(['cancel', 'context', 'source', 'evidence'] as const)('drops deferred diff hashing after %s', async action => {
    const first = binding();
    let finish!: (digest: string) => void;
    vi.mocked(textDigest).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = await selectedDiff(first);
    const next: FeedbackSurfaceBinding = action === 'source'
      ? { ...binding(), source: { ...first.source, source_token: 'replacement-source' } }
      : { ...binding(), context: { kind: 'chat', conversation_id: 'replacement-chat' } };
    if (action === 'cancel') adapter(first).clearSelection();
    else if (action === 'evidence') await view.rerender({ diffs: [{ ...diffs[0], diff: '@@ -1 +1 @@\n-old\n+replacement\n' }], annotationBindings: [first] });
    else await view.rerender({ diffs, annotationBindings: [next] });
    finish('old-hunk-hash'); await Promise.resolve(); await Promise.resolve();
    expect(first.onAnchorCandidate).not.toHaveBeenCalled(); expect(next.onAnchorCandidate).not.toHaveBeenCalled();
    expect(first.onTargetChange).not.toHaveBeenCalledWith(expect.objectContaining({ candidate: expect.anything() }));
  });
  it('invalidates pending line hashing when another line replaces the selection', async () => {
    const value = binding();
    let finish!: (digest: string) => void;
    vi.mocked(textDigest).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(FileDiffViewer, { diffs: [{ path: 'review.ts', diff: '@@ -1 +1 @@\n-old\n+new\n@@ -10 +10 @@\n-earlier\n+later\n' }], annotationBindings: [value] });
    await fireEvent.click(screen.getByLabelText('Select new line 1'));
    await fireEvent.click(screen.getByLabelText('Select new line 10'));
    finish('discarded-line'); await Promise.resolve(); await Promise.resolve();
    expect(value.onAnchorCandidate).not.toHaveBeenCalled();
    await waitFor(() => expect(value.onTargetChange).toHaveBeenCalledWith(expect.objectContaining({ candidate: { locator: expect.objectContaining({ side: 'new', start_line: 10, end_line: 10, quote: 'later' }) } })));
    expect(vi.mocked(value.onTargetChange!).mock.calls.filter(([target]) => target?.candidate.locator.kind === 'diff' && target.candidate.locator.start_line === 1)).toHaveLength(0);
  });
  it('clears rendered rich selection and a pending scoped read after shell cancellation', async () => {
    const value = { ...binding(), tool: 'text' as const };
    let finish!: (page: ScopedCanonicalFeedbackText) => void;
    value.resolveTextScope = vi.fn(() => new Promise<ScopedCanonicalFeedbackText>(resolve => { finish = resolve; }));
    render(AnnotationHarness, { binding: value, kind: 'rich' });
    await selectRich();
    await waitFor(() => expect(value.resolveTextScope).toHaveBeenCalled());
    adapter(value).clearSelection();
    finish({ scope: { kind: 'document' }, content_digest: 'source-hash', descriptor: { representation: 'feedback-text-v1', text_digest: 'text-hash', length: 18, available_ranges: [[0, 18]] },
      text_page: { representation: 'feedback-text-v1', text_digest: 'text-hash', text: 'Selected rich text', start: 0, end: 18, total_length: 18 } });
    await waitFor(() => expect(value.onTargetChange).toHaveBeenLastCalledWith(null));
    expect(window.getSelection()?.toString()).toBe('');
    expect(value.onInteractionChange).toHaveBeenLastCalledWith(false);
    expect(value.onAnchorCandidate).not.toHaveBeenCalled();
  });
  it('invalidates a rendered rich selection on revision replacement', async () => {
    const value = { ...binding(), tool: 'text' as const };
    value.resolveTextScope = vi.fn().mockResolvedValue({ descriptor: { text_digest: 'text-hash' }, text_page: { text: 'Selected rich text', start: 0 } });
    const view = render(AnnotationHarness, { binding: value, kind: 'rich' });
    await selectRich();
    await waitFor(() => expect(value.onTargetChange).toHaveBeenCalledWith(expect.objectContaining({ label: 'Selected' })));
    await view.rerender({ binding: { ...binding(), source: { ...value.source, source_token: 'next-revision-token' } }, kind: 'rich' });
    expect(value.onTargetChange).toHaveBeenLastCalledWith(null);
    expect(value.onAnchorCandidate).not.toHaveBeenCalled();
  });
  it('passes each original Work event to binding resolution instead of concatenating evidence', async () => {
    const first = binding(), second = binding();
    const events = [{ ...diffs[0], source_item_id: 'first-event' }, { path: 'review.ts', diff: '@@ -1 +1 @@\n-new\n+latest\n', source_item_id: 'second-event' }];
    const resolve = vi.fn((event: WorkFileDiff) => event.source_item_id === 'first-event' ? first : event.source_item_id === 'second-event' ? second : null);
    render(WorkFileTree, { diffs: events, feedbackBindingForDiff: resolve });
    await fireEvent.click(await screen.findByTestId('work-file-review.ts'));
    await waitFor(() => expect(resolve).toHaveBeenCalled());
    expect(resolve.mock.calls.some(([event]) => event.source_item_id === 'first-event' && event.diff === events[0].diff)).toBe(true);
    expect(resolve.mock.calls.some(([event]) => event.source_item_id === 'second-event' && event.diff === events[1].diff)).toBe(true);
    expect(resolve.mock.calls.every(([event]) => events.some(original => original.diff === event.diff))).toBe(true);
  });
  it('keeps bound lightbox View-mode zoom and pan on the image-plus-overlay transform', async () => {
    const value: FeedbackSurfaceBinding = { ...binding(), mode: 'view' };
    const view = render(AnnotationHarness, { binding: value, kind: 'lightbox' });
    const transformed = view.container.ownerDocument.querySelector<HTMLElement>('[data-feedback-image-transform]')!;
    const stage = transformed.closest<HTMLElement>('[data-image-stage]')!;
    stage.setPointerCapture = vi.fn(); stage.releasePointerCapture = vi.fn();
    await fireEvent.dblClick(stage);
    expect(transformed.style.transform).toContain('scale(2)');
    function pointer(type: string, x: number, y: number) {
      const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button: 0 });
      Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'touch' } }); return event;
    }
    await fireEvent(stage, pointer('pointerdown', 100, 100));
    await fireEvent(stage, pointer('pointermove', 150, 120));
    await fireEvent(stage, pointer('pointerup', 150, 120));
    expect(transformed.style.transform).toContain('translate3d(50px, 20px, 0)');
  });
  it('keeps two-pointer pinch zoom working in a bound View-mode lightbox', async () => {
    const value: FeedbackSurfaceBinding = { ...binding(), mode: 'view' };
    const view = render(AnnotationHarness, { binding: value, kind: 'lightbox' });
    const transformed = view.container.ownerDocument.querySelector<HTMLElement>('[data-feedback-image-transform]')!;
    const stage = transformed.closest<HTMLElement>('[data-image-stage]')!;
    stage.setPointerCapture = vi.fn(); stage.releasePointerCapture = vi.fn();
    function pointer(type: string, id: number, x: number) {
      const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: 100, button: 0 });
      Object.defineProperties(event, { pointerId: { value: id }, pointerType: { value: 'touch' } }); return event;
    }
    await fireEvent(stage, pointer('pointerdown', 1, 100));
    await fireEvent(stage, pointer('pointerdown', 2, 200));
    await fireEvent(stage, pointer('pointermove', 2, 300));
    expect(transformed.style.transform).toContain('scale(2)');
    await fireEvent(stage, pointer('pointerup', 2, 300));
    await fireEvent(stage, pointer('pointerup', 1, 100));
  });
});

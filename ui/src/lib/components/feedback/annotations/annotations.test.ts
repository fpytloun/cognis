import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import AnnotationHarness from './AnnotationHarness.svelte';
import type { FeedbackSurfaceBinding, ScopedCanonicalFeedbackText } from '$lib/feedback/types';
function page(text: string, start = 0): ScopedCanonicalFeedbackText {
  return { scope: { kind: 'document' }, content_digest: 'source-digest', descriptor: { representation: 'feedback-text-v1', text_digest: 'text-digest', length: start + text.length, available_ranges: [[0, start + text.length]] },
    text_page: { representation: 'feedback-text-v1', text_digest: 'text-digest', start, end: start + text.length, total_length: start + text.length, text } };
}
function binding(): FeedbackSurfaceBinding {
  return { source: { subject_id: 'subject-document', source_token: 'resolved-document-token', source: { kind: 'document', content_ref: 'art_document' }, content_digest: 'source-digest',
    availability: 'available', permitted_locator_kinds: ['text', 'image'], context: { kind: 'chat', conversation_id: 'origin-conversation' }, lineage_id: null, revision: 1, latest_content_ref: null,
    capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false }, title: 'Document', format: 'text', canonical_text: null },
    context: { kind: 'chat', conversation_id: 'origin-conversation' }, mode: 'comment', supported_locator_kinds: ['text', 'image'], completeness: 'available', highlights: [], selected_thread_id: null,
    resolveTextScope: vi.fn(async () => page('First 😀 last', 100)), onAnchorCandidate: vi.fn(), onSelectThread: vi.fn(), onInteractionChange: vi.fn(), registerAdapter: vi.fn(() => () => {}) };
}
describe('real annotation controls using canonical shell binding', () => {
  it('builds UTF-16 canonical offsets relative to a paged source, not browser text', async () => {
    const value = binding(); render(AnnotationHarness, { binding: value });
    await fireEvent.click(screen.getByText('Load canonical text for selection'));
    const input = await screen.findByLabelText('Canonical source text') as HTMLTextAreaElement;
    input.setSelectionRange(6, 8); await fireEvent.select(input);
    await fireEvent.click(screen.getByText('Comment on selected text'));
    expect(value.onAnchorCandidate).toHaveBeenCalledWith({ locator: { kind: 'text', range: expect.objectContaining({ start: 106, end: 108, exact: '😀', text_digest: 'text-digest' }) } });
  });
  it('fences a canonical read completed after originating chat replacement', async () => {
    let resolve!: (value: ScopedCanonicalFeedbackText) => void;
    const first = binding(); first.resolveTextScope = vi.fn(() => new Promise<ScopedCanonicalFeedbackText>(done => { resolve = done; }));
    const { rerender } = render(AnnotationHarness, { binding: first });
    await fireEvent.click(screen.getByText('Load canonical text for selection'));
    const next: FeedbackSurfaceBinding = { ...binding(), context: { kind: 'chat', conversation_id: 'other-conversation' } };
    await rerender({ binding: next }); resolve(page('Old private text'));
    await waitFor(() => expect(screen.queryByDisplayValue('Old private text')).toBeNull());
    expect(next.onAnchorCandidate).not.toHaveBeenCalled();
  });
  it('supports keyboard pins and emits only typed geometry candidates', async () => {
    const value = { ...binding(), mode: 'area' as const, onTargetChange: vi.fn() }; render(AnnotationHarness, { binding: value, kind: 'visual' });
    await fireEvent.click(screen.getByText('Add pin'));
    expect(value.onAnchorCandidate).not.toHaveBeenCalled();
    expect(value.onTargetChange).toHaveBeenCalledWith(expect.objectContaining({ candidate: { locator: expect.objectContaining({ kind: 'image', shapes: [{ kind: 'pin', point: { x: 0.5, y: 0.5 } }] }) } }));
  });
});

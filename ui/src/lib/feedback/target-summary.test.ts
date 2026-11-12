import { expect, it } from 'vitest';
import { feedbackSourceState, sameFeedbackSource, targetSummary } from './target-summary';
import type { FeedbackLocator, FeedbackSourceRef, FeedbackTextRange, ResolvedFeedbackSource } from './types';

const range: FeedbackTextRange = { representation: 'feedback-text-v1', text_digest: 'a'.repeat(64), start: 0, end: 5, exact: 'Hello', prefix: '', suffix: '' };
const event = { store: 'intaris', session_id: crypto.randomUUID(), seq: 1, event_type: 'tool_result' };
const work: Extract<FeedbackSourceRef, { kind: 'work_diff' }> = { kind: 'work_diff', activity_scope_id: crypto.randomUUID(), source_event: event, source_item_id: crypto.randomUUID(), file_ordinal: 0, root_id: null, path: 'src/main.ts', old_path: 'src/old.ts' };
const viewport = { width_css_px: 800, height_css_px: 600, scroll_x_css_px: 0, scroll_y_css_px: 0, device_pixel_ratio: 1 };
const summary = (locator: FeedbackLocator) => targetSummary({ locator, source: work, sourceTitle: 'Release notes' });

it('uses evidence-backed quotes across all text families', () => {
  expect(summary({ kind: 'text', range })).toEqual({ label: 'Selected text', quote: 'Hello', quoteKind: 'canonical' });
  expect(summary({ kind: 'rich', block_id: crypto.randomUUID(), identity: 'semantic', item_key: null, field_path: ['body'], range }).quote).toBe('Hello');
  expect(summary({ kind: 'pdf', page: 3, page_width_pt: 600, page_height_pt: 800, rotation: 0, shapes: [], range }).label).toBe('Page 3');
  expect(summary({ kind: 'html', range, element_key: null, selector_path: null, viewport, render_state_digest: 'b'.repeat(64), shapes: [], capture_id: null }).quoteKind).toBe('observed');
});

it('shows transcript quotes as read text while documents keep their exact text', () => {
  const transcript: FeedbackSourceRef = { kind: 'transcript', conversation_id: crypto.randomUUID(), source_event: event, source_item_id: crypto.randomUUID(), field_path: ['content'] };
  const quoted = (exact: string, source: FeedbackSourceRef) => targetSummary({ locator: { kind: 'text', range: { ...range, exact } }, source, sourceTitle: null }).quote;
  expect(quoted('staging-only** rollout', transcript)).toBe('staging-only rollout');
  expect(quoted('a **staging-only** `rollout` [plan](https://x.test)', transcript)).toBe('a staging-only rollout plan');
  expect(quoted('# Staging review\n\n- *Keep* approval_flag separate', transcript)).toBe('Staging review Keep approval_flag separate');
  expect(quoted('**', transcript)).toBeNull();
  expect(quoted('keep **this** exact', { kind: 'document', content_ref: crypto.randomUUID() })).toBe('keep **this** exact');
});

it('never invents excerpts or exposes opaque element identifiers', () => {
  expect(summary({ kind: 'rich', block_id: crypto.randomUUID(), identity: 'semantic', item_key: null, field_path: ['rows', '0', 'checks'], range: null })).toEqual({ label: 'Selected field', quote: null, quoteKind: null });
  expect(summary({ kind: 'html', range: null, element_key: crypto.randomUUID(), selector_path: 'body > div', viewport, render_state_digest: 'b'.repeat(64), shapes: [], capture_id: null }).label).toBe('Selected element');
  expect(summary({ kind: 'image', intrinsic_width: 800, intrinsic_height: 600, orientation: 1, shapes: [] }).label).toBe('Marked area');
  expect(summary({ kind: 'pdf', page: 2, page_width_pt: 600, page_height_pt: 800, rotation: 0, shapes: [], range: null }).label).toBe('Page 2 · Marked area');
  expect(summary({ kind: 'whole' }).label).toBe('Whole document');
  expect(targetSummary({ locator: { kind: 'whole' }, source: null, sourceTitle: null }).label).toBe('Whole source');
});

it('describes Work file and old/new ranges', () => {
  const locator = { kind: 'diff' as const, scope: 'lines' as const, side: 'new' as const, start_line: 4, end_line: 7, hunk_digest: null, quote: null };
  expect(summary(locator).label).toBe('src/main.ts · New lines 4–7');
  expect(summary({ ...locator, side: 'old', end_line: 4 }).label).toBe('src/old.ts · Old lines 4');
  expect(summary({ ...locator, scope: 'file', side: null, start_line: null, end_line: null }).label).toBe('Entire file · src/main.ts');
});

it('compares immutable document, transcript and Work identities explicitly', () => {
  const document: FeedbackSourceRef = { kind: 'document', content_ref: crypto.randomUUID() };
  expect(sameFeedbackSource(document, { ...document })).toBe(true);
  expect(sameFeedbackSource(document, { ...document, content_ref: crypto.randomUUID() })).toBe(false);
  const transcript: Extract<FeedbackSourceRef, { kind: 'transcript' }> = { kind: 'transcript', conversation_id: crypto.randomUUID(), source_event: event, source_item_id: crypto.randomUUID(), field_path: ['body'] };
  expect(sameFeedbackSource(transcript, structuredClone(transcript))).toBe(true);
  expect(sameFeedbackSource(transcript, { ...transcript, source_event: { ...event, seq: 2 } })).toBe(false);
  expect(sameFeedbackSource(transcript, { ...transcript, field_path: ['title'] })).toBe(false);
  expect(sameFeedbackSource(work, structuredClone(work))).toBe(true);
  for (const changed of [{ file_ordinal: 1 }, { source_item_id: crypto.randomUUID() }, { path: 'src/next.ts' }, { root_id: crypto.randomUUID() }]) {
    expect(sameFeedbackSource(work, { ...work, ...changed })).toBe(false);
  }
});

it('does not equate matching content with matching revision or missing source with historical', () => {
  const source: ResolvedFeedbackSource = { source: { kind: 'document', content_ref: crypto.randomUUID() }, content_digest: 'a'.repeat(64),
    subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), availability: 'available', permitted_locator_kinds: ['whole'],
    context: { kind: 'standalone' }, lineage_id: null, revision: 1, latest_content_ref: null, capabilities: { read: true, comment: true, dispatch: false, moderate: false, disclose: false }, title: 'Current title', format: 'text', canonical_text: null };
  expect(feedbackSourceState(source, source)).toBe('current');
  expect(feedbackSourceState({ ...source, source: { kind: 'document', content_ref: crypto.randomUUID() } }, source)).toBe('historical');
  expect(feedbackSourceState(source, null)).toBe('unavailable');
});

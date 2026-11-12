import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { feedbackApi } from '$lib/feedback/api';
import { FeedbackDrafts } from '$lib/feedback/persistence';
import type { ResolvedFeedbackSource } from '$lib/feedback/types';
import FeedbackHostLoaderHarness from './FeedbackHostLoaderHarness.svelte';
import { inlineChrome, panelChrome } from './panel-chrome';

const source: ResolvedFeedbackSource = {
  source: { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
  subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
  context: { kind: 'standalone' }, availability: 'available', permitted_locator_kinds: ['whole'],
  lineage_id: null, revision: 1, latest_content_ref: null, title: 'Library report', format: 'text',
  canonical_text: null, capabilities: { read: true, comment: true, dispatch: false, moderate: false, disclose: false },
};
beforeAll(async () => { await Promise.all([inlineChrome.load(), panelChrome.load()]); });
beforeEach(async () => {
  await loginFixtureAccount();
  vi.spyOn(feedbackApi, 'resolve').mockResolvedValue(source);
  vi.spyOn(feedbackApi, 'threads').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(feedbackApi, 'draft').mockRejectedValue(Object.assign(new Error('No draft'), { status: 404 }));
  vi.spyOn(feedbackApi, 'saveDraft').mockResolvedValue({ version: 1 });
  vi.spyOn(FeedbackDrafts.prototype, 'read').mockReturnValue(null);
  vi.spyOn(FeedbackDrafts.prototype, 'write').mockResolvedValue();
});
afterEach(async () => { cleanup(); vi.restoreAllMocks(); await auth.clear(); });

for (const warmed of [false, true]) {
  it(`opens a whole-document composer from the comment list ${warmed ? 'after reopening it' : 'on first open'}`, async () => {
    render(FeedbackHostLoaderHarness, { props: { context: { kind: 'standalone' } } });
    if (warmed) {
      await fireEvent.click(screen.getByRole('button', { name: /^Comments/ }));
      await fireEvent.click(await screen.findByRole('button', { name: 'Close comments' }));
      await waitFor(() => expect(screen.queryByRole('region', { name: 'Feedback panel' })).toBeNull());
    }
    await fireEvent.click(screen.getByRole('button', { name: /^Comments/ }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Comment on document' }));
    const composer = await screen.findByRole('region', { name: 'Add comment' });
    expect(composer).toHaveTextContent('Whole document');
    await fireEvent.input(screen.getByRole('textbox', { name: 'Comment' }), { target: { value: 'Review the whole report.' } });
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Add comment' })).toBeNull());
  });
}

it('does not compose when the resolved source denies comments', async () => {
  vi.mocked(feedbackApi.resolve).mockResolvedValue({ ...source, capabilities: { ...source.capabilities, comment: false } });
  render(FeedbackHostLoaderHarness, { props: { context: { kind: 'standalone' } } });
  await fireEvent.click(screen.getByRole('button', { name: /^Comments/ }));
  await screen.findByRole('region', { name: 'Feedback panel' });
  await waitFor(() => expect(feedbackApi.threads).toHaveBeenCalled());
  expect(screen.queryByRole('button', { name: 'Comment on document' })).toBeNull();
  expect(screen.queryByRole('region', { name: 'Add comment' })).toBeNull();
});

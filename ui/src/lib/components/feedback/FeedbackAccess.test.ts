import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { documentCoordination } from '$lib/document-coordination';
import { testDocumentLease } from '$lib/testing/document-lease';
import type { FeedbackShare, ResolvedFeedbackSource } from '$lib/feedback/types';
import FeedbackAccess from './FeedbackAccess.svelte';
const calls = vi.hoisted(() => ({ shares: vi.fn(), copyShare: vi.fn(), revokeShare: vi.fn(), updateShare: vi.fn() }));
vi.mock('$lib/feedback/api', () => ({ feedbackApi: calls }));
const source: ResolvedFeedbackSource = {
  subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
  source: { kind: 'document', content_ref: `art_${crypto.randomUUID().replaceAll('-', '')}` },
  context: { kind: 'standalone' }, availability: 'available', format: 'plain',
  permitted_locator_kinds: ['whole'], lineage_id: null, revision: 1, latest_content_ref: null,
  canonical_text: null, title: 'Shared document',
  capabilities: { read: true, comment: true, moderate: true, dispatch: false, disclose: true },
};
const share: FeedbackShare = {
  share_id: crypto.randomUUID(), share_token: null, version: 1, allow_comments: false, discussion: 'own_only',
  expires_at: '2100-01-01T00:00:00Z', revoked: false, can_copy: true, link_mode: 'view',
};
const copy = vi.fn();
beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(documentCoordination(), 'capture').mockReturnValue(testDocumentLease().lease);
  calls.shares.mockResolvedValue({ items: [share] });
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
  copy.mockResolvedValue(undefined);
});
afterEach(() => vi.restoreAllMocks());
it('updates comment permission after asynchronous authority validation and refreshes the confirmed state', async () => {
  calls.updateShare.mockImplementation(async (current: FeedbackShare, allow_comments: boolean) => {
    const updated = { ...current, allow_comments, version: current.version + 1 };
    calls.shares.mockResolvedValue({ items: [updated] });
    return updated;
  });
  render(FeedbackAccess, { source });
  const checkbox = await screen.findByRole('checkbox', { name: 'Allow comments' });
  await waitFor(() => expect(checkbox).toBeEnabled());
  await fireEvent.click(checkbox);
  await waitFor(() => expect(checkbox).toBeEnabled());
  expect(checkbox).toBeChecked();
  expect(calls.updateShare).toHaveBeenCalledWith(share, true);
  expect(screen.queryByRole('alert')).toBeNull();
  await fireEvent.click(checkbox);
  await waitFor(() => expect(checkbox).toBeEnabled());
  expect(checkbox).not.toBeChecked();
  expect(calls.updateShare).toHaveBeenLastCalledWith({ ...share, allow_comments: true, version: 2 }, false);
});
it('copies the existing token with a copy icon without creating or changing a share', async () => {
  const token = crypto.randomUUID();
  calls.copyShare.mockResolvedValue({ ...share, share_token: token });
  render(FeedbackAccess, { source });
  const button = await screen.findByRole('button', { name: 'Copy public link' });
  expect(button.querySelector('svg')).not.toBeNull();
  await fireEvent.click(button);
  await waitFor(() => expect(copy).toHaveBeenCalledWith(`${location.origin}/feedback#${token}`));
  expect(calls.copyShare).toHaveBeenCalledWith(share.share_id);
  expect(calls.updateShare).not.toHaveBeenCalled();
  expect(calls.revokeShare).not.toHaveBeenCalled();
});
it('does not offer recovery for legacy tokens but still allows explicit revocation', async () => {
  calls.shares.mockResolvedValue({ items: [{ ...share, can_copy: false }] });
  render(FeedbackAccess, { source });
  expect(await screen.findByRole('button', { name: 'Copy public link' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Revoke link' })).toBeEnabled();
  expect(screen.getByText(/cannot be recovered/)).toBeVisible();
});
it('reconciles an uncertain revoke before allowing another mutation', async () => {
  calls.revokeShare.mockRejectedValueOnce(new TypeError('Connection interrupted'));
  render(FeedbackAccess, { source });
  await fireEvent.click(await screen.findByRole('button', { name: 'Revoke link' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('not confirmed');
  expect(screen.getByRole('button', { name: 'Revoke link' })).toBeDisabled();
  calls.shares.mockResolvedValue({ items: [{ ...share, version: 2, revoked: true, can_copy: false }] });
  await fireEvent.click(screen.getByRole('button', { name: 'Reload links' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect(calls.revokeShare).toHaveBeenCalledTimes(1);
  // A revoked link is treated as deleted: it is not listed, so it cannot be revoked again.
  expect(screen.queryByRole('button', { name: 'Revoke link' })).toBeNull();
  expect(screen.getByText('No managed public links yet.')).toBeVisible();
});
it('never exposes a late recovered token after changing sources', async () => {
  let complete!: (value: FeedbackShare) => void;
  calls.copyShare.mockImplementationOnce(() => new Promise<FeedbackShare>(resolve => { complete = resolve; }));
  const view = render(FeedbackAccess, { source });
  await fireEvent.click(await screen.findByRole('button', { name: 'Copy public link' }));
  await view.rerender({ source: { ...source, subject_id: crypto.randomUUID() } });
  complete({ ...share, share_token: crypto.randomUUID() });
  await waitFor(() => expect(calls.shares).toHaveBeenCalledTimes(2));
  expect(copy).not.toHaveBeenCalled();
  expect(screen.queryByRole('textbox', { name: 'Public link URL' })).toBeNull();
});

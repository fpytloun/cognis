import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { documentCoordination } from '$lib/document-coordination';
import { testDocumentLease } from '$lib/testing/document-lease';
import type { ResolvedFeedbackSource } from '$lib/feedback/types';
import PublicLinkDialog from './PublicLinkDialog.svelte';
import type { ComponentProps } from 'svelte';
import { DOCUMENT_SHARING_CONTEXT, type DocumentSharingContext } from '$lib/document-sharing-context';
const calls = vi.hoisted(() => ({ people: vi.fn(), setPerson: vi.fn() }));
vi.mock('$lib/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('$lib/api/client')>(),
  api: { artifactDocuments: calls },
}));
beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(documentCoordination(), 'capture').mockReturnValue(testDocumentLease().lease);
  calls.people.mockResolvedValue({ kind: 'artifact', lineage_id: crypto.randomUUID(), items: [] });
});
afterEach(() => vi.restoreAllMocks());
it('does not load private people UI without an app-provided loader', async () => {
  render(PublicLinkDialog, { feedbackSource: source(), createLink: vi.fn(), onClose: vi.fn() });
  await vi.dynamicImportSettled();
  expect(screen.getByRole('tab', { name: 'Share with people' })).toBeDisabled();
  expect(screen.queryByRole('textbox', { name: 'Cognis user email' })).toBeNull();
  expect(calls.people).not.toHaveBeenCalled();
});
it('closes sharing when its verified actor lease is invalidated', async () => {
  const { lease, controller } = testDocumentLease();
  vi.spyOn(documentCoordination(), 'capture').mockReturnValue(lease);
  const onClose = vi.fn();
  renderOwned({ feedbackSource: source(), createLink: vi.fn(), onClose });
  await vi.dynamicImportSettled();
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Cognis user email' })).toBeEnabled());
  controller.abort();
  expect(onClose).toHaveBeenCalledOnce();
});
function renderOwned(props: ComponentProps<typeof PublicLinkDialog>) {
  const resolved = props.feedbackSource;
  const sharing: DocumentSharingContext = {
    contentRef: resolved?.source.kind === 'document' ? resolved.source.content_ref : null,
    canManage: !!resolved?.capabilities.disclose,
    capture: () => documentCoordination().capture(),
    loadPeople: () => import('./DocumentPeopleAccess.svelte'),
  };
  return render(PublicLinkDialog, { props, context: new Map([[DOCUMENT_SHARING_CONTEXT, sharing]]) });
}
it('shares from owned document metadata even when annotations are unsupported', async () => {
  const contentRef = `art_${crypto.randomUUID().replaceAll('-', '')}`;
  const validate = vi.fn().mockResolvedValue(undefined);
  const sharing: DocumentSharingContext = {
    contentRef, canManage: true,
    loadPeople: () => import('./DocumentPeopleAccess.svelte'),
    capture: () => ({ owner: 'owner@cognis.test', signal: new AbortController().signal,
      epoch: crypto.randomUUID(), assertCurrent: vi.fn(), validate }),
  };
  render(PublicLinkDialog, { props: { internalUrl: `/library/artifact/${contentRef}`, onClose: vi.fn() },
    context: new Map([[DOCUMENT_SHARING_CONTEXT, sharing]]) });
  await vi.dynamicImportSettled();
  expect(await screen.findByRole('textbox', { name: 'Cognis user email' })).toBeEnabled();
  expect(calls.people).toHaveBeenCalledWith('artifact', contentRef);
  expect(validate).toHaveBeenCalledTimes(2);
});
async function publicLinks() {
  const tab = screen.getByRole('tab', { name: 'Public links' });
  await waitFor(() => expect(tab).toBeEnabled());
  await fireEvent.click(tab);
}

function source(disclose = true): ResolvedFeedbackSource {
  return {
    subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
    source: { kind: 'document', content_ref: `art_${crypto.randomUUID().replaceAll('-', '')}` },
    context: { kind: 'standalone' }, availability: 'available', format: 'plain',
    permitted_locator_kinds: ['whole'], lineage_id: null, revision: 1, latest_content_ref: null,
    canonical_text: null, title: 'Review document',
    capabilities: { read: true, comment: true, moderate: true, dispatch: false, disclose },
  };
}

it('separates whole-document people sharing from revision-pinned public links', async () => {
  const createLink = vi.fn().mockResolvedValue('https://feedback.test/review');
  renderOwned({ createLink, internalUrl: '/library', feedbackSource: source(), onClose: vi.fn() });
  expect(screen.getAllByRole('dialog')).toHaveLength(1);
  expect(screen.getByRole('tab', { name: 'Share with people' })).toHaveAttribute('aria-selected', 'true');
  await vi.dynamicImportSettled();
  expect(await screen.findByText(/including future revisions/)).toBeVisible();
  expect(screen.queryByRole('checkbox', { name: 'Allow comments' })).toBeNull();
  await publicLinks();
  expect(screen.getByRole('checkbox', { name: 'Allow comments' })).toBeEnabled();
  expect(screen.getByRole('checkbox', { name: 'Allow comments' })).not.toBeChecked();
  expect(screen.queryByText('Revocable link and commenting permissions')).toBeNull();
  expect(createLink).not.toHaveBeenCalled();
});

it('selects a public viewer and sends explicit comment permission only on creation', async () => {
  const createLink = vi.fn().mockResolvedValue('https://feedback.test/review');
  renderOwned({ createLink, defaultMode: 'download', allowModeChoice: false,
    feedbackSource: source(), onClose: vi.fn() });
  await publicLinks();
  await fireEvent.click(screen.getByRole('checkbox', { name: 'Allow comments' }));
  expect(createLink).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Create public link' }));
  await waitFor(() => expect(createLink).toHaveBeenCalledWith({ ttl_seconds: 604800, mode: 'view', allow_comments: true }));
});

it('removes comment permission for downloads without changing internal recipient permissions', async () => {
  const createLink = vi.fn().mockResolvedValue('https://feedback.test/review');
  renderOwned({ createLink, internalUrl: '/library', feedbackSource: source(), onClose: vi.fn() });
  await publicLinks();
  await fireEvent.click(screen.getByRole('checkbox', { name: 'Allow comments' }));
  await fireEvent.change(screen.getByRole('combobox', { name: 'Link behavior' }), { target: { value: 'download' } });
  expect(screen.getByRole('checkbox', { name: 'Allow comments' })).not.toBeChecked();
  await fireEvent.click(screen.getByRole('tab', { name: 'Share with people' }));
  expect(screen.queryByRole('checkbox', { name: 'Allow comments' })).toBeNull();
  expect(await screen.findByRole('combobox', { name: 'New recipient permission' })).toHaveValue('viewer');
  expect(createLink).not.toHaveBeenCalled();
});

it('does not offer comment sharing without disclosure permission', () => {
  renderOwned({ createLink: vi.fn(), feedbackSource: source(false), onClose: vi.fn() });
  expect(screen.queryByRole('checkbox', { name: 'Allow comments' })).toBeNull();
  expect(screen.queryByText('Manage links and access')).toBeNull();
});

import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { documentCoordination } from '$lib/document-coordination';
import { testDocumentLease } from '$lib/testing/document-lease';
import type { DocumentPeople } from '$lib/document-people';
import DocumentPeopleAccess from './DocumentPeopleAccess.svelte';
import { tick } from 'svelte';
const calls = vi.hoisted(() => ({ people: vi.fn(), setPerson: vi.fn() }));
vi.mock('$lib/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('$lib/api/client')>(),
  api: { artifactDocuments: calls },
}));
const contentRef = `art_${crypto.randomUUID().replaceAll('-', '')}`;
const email = 'reviewer@cognis.test';
function page(version = 3, revoked = true): DocumentPeople {
  return { kind: 'artifact', lineage_id: contentRef,
    items: [{ principal_email: email, allow_comments: false, revoked, version }] };
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(documentCoordination(), 'capture').mockReturnValue(testDocumentLease().lease);
  calls.people.mockResolvedValue(page());
  calls.setPerson.mockResolvedValue(page(4, false));
});
afterEach(() => vi.restoreAllMocks());
async function fillAndSubmit() {
  const input = screen.getByRole('textbox', { name: 'Cognis user email' });
  await waitFor(() => expect(input).toBeEnabled());
  await fireEvent.input(input, { target: { value: email } });
  await fireEvent.submit(input.closest('form')!);
  return input;
}
it('uses a view-only default and preserves the version of a previously revoked grant', async () => {
  render(DocumentPeopleAccess, { contentRef });
  await fillAndSubmit();
  await waitFor(() => expect(calls.setPerson).toHaveBeenCalledWith('artifact', contentRef, {
    principal_email: email, expected_version: 3, allow_comments: false, revoked: false,
  }));
  expect(await screen.findByText('Document access saved.')).toBeVisible();
  expect(calls.people).toHaveBeenCalledTimes(1);
});
it('preserves input after an uncertain save and never retries it automatically', async () => {
  calls.setPerson.mockRejectedValueOnce(new TypeError('Connection interrupted'));
  render(DocumentPeopleAccess, { contentRef });
  const input = await fillAndSubmit();
  expect(await screen.findByRole('alert')).toHaveTextContent('Access was not confirmed');
  expect(input).toHaveValue(email);
  expect(screen.getByRole('button', { name: 'Share with person' })).toBeDisabled();
  calls.people.mockResolvedValue(page(4, false));
  await fireEvent.click(screen.getByRole('button', { name: 'Reload access' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Share with person' })).toBeEnabled());
  expect(calls.setPerson).toHaveBeenCalledTimes(1);
  await fireEvent.change(screen.getByRole('combobox', { name: `Permission for ${email}` }), { target: { value: 'commenter' } });
  await waitFor(() => expect(calls.setPerson).toHaveBeenLastCalledWith('artifact', contentRef, {
    principal_email: email, expected_version: 4, allow_comments: true, revoked: false,
  }));
});
it('discards late access responses after changing documents', async () => {
  let complete!: (value: DocumentPeople) => void;
  calls.people.mockImplementationOnce(() => new Promise<DocumentPeople>(resolve => { complete = resolve; }));
  const view = render(DocumentPeopleAccess, { contentRef });
  await waitFor(() => expect(calls.people).toHaveBeenCalledTimes(1));
  const nextRef = `art_${crypto.randomUUID().replaceAll('-', '')}`;
  calls.people.mockResolvedValue({ kind: 'artifact', lineage_id: nextRef, items: [] });
  await view.rerender({ contentRef: nextRef });
  await waitFor(() => expect(calls.people).toHaveBeenCalledWith('artifact', nextRef));
  complete(page(4, false));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Cognis user email' })).toBeEnabled());
  expect(screen.queryByText(email)).toBeNull();
});
it('does not write a different document while actor validation is pending', async () => {
  const view = render(DocumentPeopleAccess, { contentRef });
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Cognis user email' })).toBeEnabled());
  let complete!: () => void;
  const validate = vi.fn(() => new Promise<void>(resolve => { complete = resolve; }));
  vi.spyOn(documentCoordination(), 'capture').mockReturnValueOnce({ ...testDocumentLease().lease, validate });
  await fillAndSubmit();
  expect(validate).toHaveBeenCalledOnce();
  const nextRef = `art_${crypto.randomUUID().replaceAll('-', '')}`;
  calls.people.mockResolvedValue({ kind: 'artifact', lineage_id: nextRef, items: [] });
  await view.rerender({ contentRef: nextRef });
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Cognis user email' })).toBeEnabled());
  complete();
  await tick();
  expect(calls.setPerson).not.toHaveBeenCalled();
});

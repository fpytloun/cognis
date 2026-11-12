import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import DocumentActions from './DocumentActions.svelte';
import './PublicLinkDialog.svelte';

describe('DocumentActions sharing', () => {
  it('keeps document search in the extended menu', async () => {
    let searching = false;
    const view = render(DocumentActions, { onSearch: () => { searching = true; } });
    await fireEvent.click(screen.getByLabelText('More document actions'));
    const menu = within(view.container.querySelector('.chat-document-menu')! as HTMLElement);
    const search = await menu.findByRole('button', { name: 'Search in document' });
    expect(screen.getAllByRole('button', { name: 'Search in document' })).toEqual([search]);
    await fireEvent.click(search);
    expect(searching).toBe(true);
    expect(view.container.querySelector('details')?.open).toBe(false);
  });
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  });
  it('offers a desktop popout in the more menu for authenticated deliverables only', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const view = render(DocumentActions, { internalUrl: '/library/deliverable/dlv_revision' });
    await fireEvent.click(screen.getByLabelText('More document actions'));
    await fireEvent.click(screen.getByRole('button', { name: 'Open in new window' }));
    expect(open).toHaveBeenCalledWith(expect.stringContaining('/library/deliverable/dlv_revision?window=1'), expect.any(String), expect.stringContaining('noopener'));
    expect(view.container.querySelector('details')?.open).toBe(false);
    await view.rerender({ internalUrl: '/library/artifact/art_image' });
    await fireEvent.click(screen.getByLabelText('More document actions'));
    expect(screen.queryByRole('button', { name: 'Open in new window' })).toBeNull();
    open.mockRestore();
  });

  it('defaults to a private authenticated link without creating a public token', async () => {
    const createLink = vi.fn().mockResolvedValue('https://cognis.test/shared/report');
    render(DocumentActions, { internalUrl: '/library/deliverable/report', createLink });
    await vi.dynamicImportSettled();
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await vi.dynamicImportSettled();
    const dialog = within(await screen.findByRole('dialog', { name: 'Share document' }));
    expect(dialog.getByLabelText('Link access and expiration')).toHaveValue('private');
    expect(dialog.getByRole('option', { name: 'Private · internal access only' })).toBeTruthy();
    expect(dialog.queryByRole('button', { name: 'Create public link' })).toBeNull();
    await fireEvent.click(dialog.getByRole('button', { name: 'Copy private link' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/library/deliverable/report`));
    expect(createLink).not.toHaveBeenCalled();
  });

  it('requires an explicit public expiry and creation action before requesting a token', async () => {
    const createLink = vi.fn().mockResolvedValue('https://cognis.test/shared/report');
    render(DocumentActions, { internalUrl: '/library/deliverable/report', createLink });
    await vi.dynamicImportSettled();
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await vi.dynamicImportSettled();
    const dialog = within(await screen.findByRole('dialog', { name: 'Share document' }));
    await fireEvent.change(dialog.getByLabelText('Link access and expiration'), { target: { value: '1' } });
    expect(createLink).not.toHaveBeenCalled();
    await fireEvent.click(dialog.getByRole('button', { name: 'Create public link' }));
    await waitFor(() => expect(createLink).toHaveBeenCalledWith({ ttl_seconds: 86400 }));
    await fireEvent.change(dialog.getByLabelText('Link access and expiration'), { target: { value: 'private' } });
    expect(dialog.getByRole('button', { name: 'Copy private link' })).toBeEnabled();
    expect(dialog.queryByRole('textbox', { name: 'Public link URL' })).toBeNull();
  });

  it('keeps private sharing available when public-link creation is not permitted', async () => {
    render(DocumentActions, { internalUrl: '/library/deliverable/read-only' });
    await vi.dynamicImportSettled();
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await vi.dynamicImportSettled();
    const dialog = within(await screen.findByRole('dialog', { name: 'Share document' }));
    await fireEvent.click(dialog.getByRole('button', { name: 'Copy private link' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/library/deliverable/read-only`));
    expect(dialog.getAllByRole('option')).toHaveLength(1);
    expect(dialog.queryByRole('button', { name: 'Create public link' })).toBeNull();
  });
});

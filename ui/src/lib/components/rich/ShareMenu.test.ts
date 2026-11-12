import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import ShareMenu from './ShareMenu.svelte';
import { DOCUMENT_SHARING_CONTEXT } from '$lib/document-sharing-context';
import { testDocumentLease } from '$lib/testing/document-lease';

describe('share expiration controls', () => {
  it('does not publish after the selected document changes during preparation', async () => {
    let contentRef = `art_${crypto.randomUUID().replaceAll('-', '')}`;
    const createLink = vi.fn().mockResolvedValue('https://cognis.test/shared/download');
    const sharing = { get contentRef() { return contentRef; }, canManage: false,
      capture: () => testDocumentLease().lease };
    render(ShareMenu, { props: { createLink }, context: new Map([[DOCUMENT_SHARING_CONTEXT, sharing]]) });
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    screen.getByRole('button', { name: 'Create public link' }).click();
    contentRef = `art_${crypto.randomUUID().replaceAll('-', '')}`;
    expect(await screen.findByRole('alert')).toHaveTextContent('The document changed');
    expect(createLink).not.toHaveBeenCalled();
  });
  it('does not expose or copy a late link after switching documents', async () => {
    let contentRef = `art_${crypto.randomUUID().replaceAll('-', '')}`;
    let complete!: (url: string) => void;
    const createLink = vi.fn(() => new Promise<string>(resolve => { complete = resolve; }));
    const writeText = vi.fn();
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const sharing = { get contentRef() { return contentRef; }, canManage: false,
      capture: () => testDocumentLease().lease };
    render(ShareMenu, { props: { createLink }, context: new Map([[DOCUMENT_SHARING_CONTEXT, sharing]]) });
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Create public link' }));
    await waitFor(() => expect(createLink).toHaveBeenCalledOnce());
    contentRef = `art_${crypto.randomUUID().replaceAll('-', '')}`;
    complete('https://cognis.test/shared/download');
    expect(await screen.findByRole('alert')).toHaveTextContent('The document changed');
    expect(screen.queryByRole('textbox', { name: 'Public link URL' })).toBeNull();
    expect(writeText).not.toHaveBeenCalled();
  });
  it.each([1, 7, 30, 90])('creates and copies a %s-day link only after selection', async days => {
    const createLink = vi.fn().mockResolvedValue('https://cognis.fpy.cz/share-result');
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(ShareMenu, { createLink });
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    expect(screen.getByRole('dialog', { name: 'Share document' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Share document' })).toHaveClass('text-strong');
    expect(screen.getByRole('combobox', { name: 'Link access and expiration' }))
      .toHaveClass('bg-surface', 'text-foreground', 'border-control-border');
    expect(createLink).not.toHaveBeenCalled();
    await fireEvent.change(screen.getByRole('combobox', { name: 'Link access and expiration' }), { target: { value: String(days) } });
    await fireEvent.click(screen.getByRole('button', { name: 'Create public link' }));
    await waitFor(() => expect(createLink).toHaveBeenCalledWith({ ttl_seconds: days * 86400 }));
    expect(writeText).toHaveBeenCalledWith('https://cognis.fpy.cz/share-result');
    expect(screen.getByRole('textbox', { name: 'Public link URL' })).toHaveValue('https://cognis.fpy.cz/share-result');
    expect(screen.getByRole('textbox', { name: 'Public link URL' })).toHaveClass('bg-surface', 'text-foreground');
  });

  it('uses a timezone-qualified custom date and restores focus on Escape', async () => {
    const createLink = vi.fn().mockResolvedValue('https://cognis.fpy.cz/share-result');
    render(ShareMenu, { createLink });
    const trigger = screen.getByRole('button', { name: 'Share link' });
    trigger.focus();
    await fireEvent.click(trigger);
    await fireEvent.change(screen.getByRole('combobox', { name: 'Link access and expiration' }), { target: { value: 'custom' } });
    const date = new Date(Date.now() + 2 * 86400000);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    await fireEvent.input(screen.getByLabelText('Expiration date and time', { exact: false }), { target: { value: local } });
    await fireEvent.click(screen.getByRole('button', { name: 'Create public link' }));
    await waitFor(() => expect(createLink).toHaveBeenCalledWith({ expires_at: new Date(local).toISOString() }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Done' })).toBeEnabled());
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it('rejects expired custom dates and exposes failures without copying', async () => {
    const createLink = vi.fn().mockRejectedValue(new Error('Share denied'));
    render(ShareMenu, { createLink });
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await fireEvent.change(screen.getByRole('combobox', { name: 'Link access and expiration' }), { target: { value: 'custom' } });
    await fireEvent.input(screen.getByLabelText('Expiration date and time', { exact: false }), { target: { value: '2020-01-01T00:00' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Create public link' }));
    expect(screen.getByRole('alert')).toHaveTextContent('between one minute and 90 days');
    expect(createLink).not.toHaveBeenCalled();
    await fireEvent.change(screen.getByRole('combobox', { name: 'Link access and expiration' }), { target: { value: '1' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Create public link' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Share denied'));
    expect(screen.getByRole('alert')).toHaveClass('text-danger');
  });
  it('passes explicit download mode for mode-aware document previews', async () => {
    const createLink = vi.fn().mockResolvedValue('https://cognis.test/shared/download');
    render(ShareMenu, { createLink, allowModeChoice: true, defaultMode: 'download' });
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    expect(screen.getByRole('combobox', { name: 'Link behavior' })).toHaveValue('download');
    await fireEvent.click(screen.getByRole('button', { name: 'Create public link' }));
    await waitFor(() => expect(createLink).toHaveBeenCalledWith({ ttl_seconds: 604800, mode: 'download' }));
  });

  it('defaults to Private and copies only an authenticated internal URL', async () => {
    const createLink = vi.fn().mockResolvedValue('https://cognis.test/shared/report');
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(ShareMenu, { internalUrl: '/library/artifact/art_report', createLink, allowModeChoice: true });
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    expect(screen.getByRole('combobox', { name: 'Link access and expiration' })).toHaveValue('private');
    expect(screen.queryByRole('combobox', { name: 'Link behavior' })).toBeNull();
    expect(screen.getByText(/Sharing it does not grant access/)).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Copy private link' }));
    expect(createLink).not.toHaveBeenCalled();
    expect(writeText).toHaveBeenCalledWith(new URL('/library/artifact/art_report', location.origin).href);
    expect(screen.getByRole('textbox', { name: 'Private link URL' })).toHaveValue(new URL('/library/artifact/art_report', location.origin).href);
    await fireEvent.change(screen.getByRole('combobox', { name: 'Link access and expiration' }), { target: { value: '7' } });
    expect(screen.queryByRole('textbox', { name: 'Private link URL' })).toBeNull();
    expect(screen.getByText(/Anyone with the link/)).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Create public link' }));
    await waitFor(() => expect(createLink).toHaveBeenCalledWith({ ttl_seconds: 604800, mode: 'view' }));
    await fireEvent.change(screen.getByRole('combobox', { name: 'Link access and expiration' }), { target: { value: 'private' } });
    expect(screen.queryByRole('textbox', { name: 'Public link URL' })).toBeNull();
  });

  it('offers Private sharing without public-link permissions and supports clipboard recovery', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('Clipboard unavailable'));
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(ShareMenu, { internalUrl: '/library/deliverable/dlv_report' });
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Create public link' })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Copy private link' }));
    expect(screen.getByRole('alert')).toHaveTextContent('copy it manually');
    expect(screen.getByRole('textbox', { name: 'Private link URL' })).toBeTruthy();
  });
});

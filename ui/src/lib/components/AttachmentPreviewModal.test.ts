import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AttachmentRef } from '$lib/types/api';
import AttachmentPreviewModal from './AttachmentPreviewModal.svelte';
import { privateHtmlPreviewSource } from '$lib/attachments/html-preview-private';
import { installFind } from '$lib/find';

const mocks = vi.hoisted(() => ({
  textPreview: vi.fn(),
  publicLink: vi.fn(),
  revisions: vi.fn(),
}));

vi.mock('$lib/api/client', () => ({
  api: {
    artifactDocuments: { publicLink: mocks.publicLink },
    artifacts: {
      textPreview: mocks.textPreview,
      revisions: mocks.revisions,
    },
  },
}));

function attachment(filename: string, mimeType: string): AttachmentRef {
  return {
    artifact_id: 'att_preview',
    kind: 'file',
    mime_type: mimeType,
    filename,
    size_bytes: 42,
  };
}

describe('AttachmentPreviewModal', () => {
  beforeEach(() => {
    mocks.textPreview.mockReset();
    mocks.publicLink.mockReset();
    mocks.revisions.mockResolvedValue({ revisions: [] });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('wraps focus through visible find controls rather than the replaced toolbar actions', async () => {
    mocks.textPreview.mockResolvedValue({ content: 'Ready for review.', rich_payload: null, truncated: false });
    const rects = vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
      return (this.closest('[data-find-toolbar][data-find-open] [data-find-actions]')
        ? [] : [{ width: 100, height: 44, top: 0, left: 0, right: 100, bottom: 44 }]) as unknown as DOMRectList;
    });
    const stop = installFind();
    try {
      const onClose = vi.fn();
      render(AttachmentPreviewModal, { attachment: attachment('report.txt', 'text/plain'), kind: 'text', onClose, resolveDownloadUrl: vi.fn() });
      await screen.findByText('Ready for review.');
      await fireEvent.click(screen.getByRole('button', { name: 'Search in document' }));
      const field = await screen.findByRole('searchbox');
      await waitFor(() => expect(field).toHaveFocus());
      const closeSearch = screen.getByRole('button', { name: 'Close search' });
      await fireEvent.keyDown(field, { key: 'Tab', shiftKey: true });
      expect(closeSearch).toHaveFocus();
      await fireEvent.keyDown(closeSearch, { key: 'Tab' });
      expect(field).toHaveFocus();
      await fireEvent.keyDown(field, { key: 'Escape' });
      await waitFor(() => expect(screen.queryByRole('searchbox')).toBeNull());
      expect(onClose).not.toHaveBeenCalled();
    } finally { stop(); rects.mockRestore(); }
  });

  it('shows revision controls outside overflow and switches chat attachment content', async () => {
    mocks.revisions.mockResolvedValue({ revisions: [
      { artifact_id: 'att_preview', filename: 'report.md', mime_type: 'text/markdown', revision: 2, is_latest: true },
      { artifact_id: 'att_older', filename: 'report-old.md', mime_type: 'text/markdown', revision: 1, is_latest: false },
    ] });
    mocks.textPreview.mockImplementation(async id => ({ content: id === 'att_older' ? 'Earlier contents' : 'Latest contents', rich_payload: null, truncated: false }));
    const onClose = vi.fn();
    render(AttachmentPreviewModal, {
      attachment: attachment('report.md', 'text/markdown'), kind: 'markdown',
      onClose, resolveDownloadUrl: vi.fn(),
    });
    const toggle = await screen.findByTestId('artifact-revision-toggle');
    expect(toggle.closest('.compact-header-more')).toBeNull();
    await fireEvent.click(toggle);
    const options = await screen.findAllByTestId('artifact-revision-option');
    await fireEvent.keyDown(toggle, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    await fireEvent.click(toggle);
    await fireEvent.click(options[1]);
    expect(await screen.findByText('Earlier contents')).toBeInTheDocument();
    expect(screen.getByTestId('artifact-revision-toggle')).toHaveTextContent('Revision 1 of 2');
  });

  it('does not let a delayed initial preview overwrite the selected revision', async () => {
    let finishInitial!: (value: { content: string; truncated: boolean; rich_payload: null }) => void;
    mocks.textPreview.mockImplementation(id => id === 'att_preview'
      ? new Promise(resolve => { finishInitial = resolve; })
      : Promise.resolve({ content: 'Selected older revision', truncated: false, rich_payload: null }));
    mocks.revisions.mockResolvedValue({ revisions: [
      { artifact_id: 'att_preview', revision: 2, filename: 'report.md', mime_type: 'text/markdown', is_latest: true },
      { artifact_id: 'att_older', revision: 1, filename: 'report.md', mime_type: 'text/markdown', is_latest: false },
    ] });
    render(AttachmentPreviewModal, {
      attachment: attachment('report.md', 'text/markdown'), kind: 'markdown',
      onClose: vi.fn(), resolveDownloadUrl: vi.fn(),
    });
    await fireEvent.click(await screen.findByTestId('artifact-revision-toggle'));
    await fireEvent.click((await screen.findAllByTestId('artifact-revision-option'))[1]);
    await screen.findByText('Selected older revision');
    finishInitial({ content: 'Stale original contents', truncated: false, rich_payload: null });
    await Promise.resolve();
    await waitFor(() => expect(screen.queryByText('Stale original contents')).not.toBeInTheDocument());
    expect(screen.getByText('Selected older revision')).toBeInTheDocument();
  });

  it('copies the same private artifact link without requesting a public token', async () => {
    mocks.textPreview.mockResolvedValue({ content: 'Attachment', rich_payload: null, truncated: false });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(AttachmentPreviewModal, {
      attachment: attachment('report.txt', 'text/plain'), kind: 'text',
      onClose: vi.fn(), resolveDownloadUrl: vi.fn(),
    });
    await vi.dynamicImportSettled();
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await vi.dynamicImportSettled();
    const dialog = within(await screen.findByRole('dialog', { name: 'Share document' }));
    expect(dialog.getByLabelText('Link access and expiration')).toHaveValue('private');
    await fireEvent.click(dialog.getByRole('button', { name: 'Copy private link' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/library/artifact/att_preview`));
    expect(mocks.publicLink).not.toHaveBeenCalled();
  });

  it('renders Markdown through the sanitized rich viewer', async () => {
    mocks.textPreview.mockResolvedValue({
      content: '# Preview title\n\n<script>alert("x")</script>',
      filename: 'report.md',
      mime_type: 'text/markdown',
      preview_kind: 'markdown',
      rich_payload: {
        metadata: {},
        blocks: [{ type: 'markdown', content: '# Preview title\n\n<script>alert("x")</script>' }],
      },
      size_bytes: 42,
      truncated: false,
    });

    render(AttachmentPreviewModal, {
      attachment: attachment('report.md', 'text/markdown'),
      kind: 'markdown',
      onClose: vi.fn(),
      resolveDownloadUrl: vi.fn(),
    });

    expect(await screen.findByRole('heading', { name: 'Preview title' })).toBeTruthy();
    expect(screen.getByRole('dialog').querySelector('script')).toBeNull();
  });

  it('renders CSV cells as inert code values in a table', async () => {
    mocks.textPreview.mockResolvedValue({
      content: 'name,value\nAlice,=1+1',
      filename: 'data.csv',
      mime_type: 'text/csv',
      preview_kind: 'csv',
      rich_payload: {
        metadata: {},
        blocks: [{
          type: 'table',
          columns: [{ key: 'column_0', label: 'name' }, { key: 'column_1', label: 'value' }],
          rows: [{
            column_0: { type: 'code', value: 'Alice' },
            column_1: { type: 'code', value: '=1+1' },
          }],
        }],
      },
      size_bytes: 42,
      table: { headers: ['name', 'value'], rows: [['Alice', '=1+1']] },
      truncated: false,
    });

    render(AttachmentPreviewModal, {
      attachment: attachment('data.csv', 'text/csv'),
      kind: 'csv',
      onClose: vi.fn(),
      resolveDownloadUrl: vi.fn(),
    });

    await screen.findByRole('table');
    expect(document.querySelector('td code')?.textContent).toBe('Alice');
    expect(screen.getByText('=1+1')).toBeTruthy();
  });

  it('keeps HTML previews inside a sandboxed modal iframe', async () => {
    const controller = new AbortController();
    const url = new URL(`/api/v1/artifacts/view/attachments/att_preview/page.html?exp=9999999999&sig=${'a'.repeat(64)}`, window.location.origin).href;
    const revoke = vi.fn();
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => 'blob:https://cognis.test/modal-preview');
      static revokeObjectURL = revoke;
    });
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<h1>Preview</h1>', {
      headers: { 'content-type': 'text/html' },
    })));
    const view = render(AttachmentPreviewModal, {
      attachment: attachment('page.html', 'text/html'),
      kind: 'html',
      htmlSource: privateHtmlPreviewSource(url, {
        owner: 'modal-preview', epoch: '1', signal: controller.signal,
        assertCurrent() { controller.signal.throwIfAborted(); },
        async validate() { controller.signal.throwIfAborted(); },
      }),
      onClose: vi.fn(),
      resolveDownloadUrl: vi.fn(),
    });

    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull());
    const iframe = document.querySelector('iframe');
    expect(iframe?.getAttribute('src')).toBe('blob:https://cognis.test/modal-preview');
    expect(iframe?.getAttribute('sandbox')).toBe('allow-scripts');
    expect(fetch).toHaveBeenCalledWith(`${url}&raw=1`, expect.objectContaining({
      credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer',
    }));
    expect(screen.getByRole('button', { name: 'Listen to this document' })).toBeVisible();
    view.unmount();
    expect(iframe?.isConnected).toBe(false);
    expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:https://cognis.test/modal-preview');
  });
});

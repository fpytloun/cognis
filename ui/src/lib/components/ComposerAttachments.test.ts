import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ComposerAttachments from './ComposerAttachments.svelte';
// Compile the lazy reader before starting timed interaction assertions.
import './AttachmentPreviewModal.svelte';
import type { AttachmentRef } from '$lib/types/api';

const mocks = vi.hoisted(() => ({ resolve: vi.fn(), textPreview: vi.fn(), signedUrl: vi.fn(), capture: vi.fn(),
  feedbackResolve: vi.fn(), feedbackThreads: vi.fn(), changeThread: vi.fn() }));
vi.mock('$lib/feedback/api', async (original) => {
  const actual = await original<typeof import('$lib/feedback/api')>();
  return { ...actual, feedbackApi: { ...actual.feedbackApi, resolve: mocks.feedbackResolve, threads: mocks.feedbackThreads, changeThread: mocks.changeThread } };
});
vi.mock('$lib/document-coordination', async (original) => {
  const actual = await original<typeof import('$lib/document-coordination')>();
  // Keep the real invalidation surface (used by feedback hosts); only lease capture is scripted.
  return { ...actual, documentCoordination: () => Object.assign(Object.create(actual.documentCoordination()), { capture: mocks.capture }) };
});
vi.mock('$lib/chat-v2/attachment-uploads', async (original) => ({
  ...await original<typeof import('$lib/chat-v2/attachment-uploads')>(),
  attachmentUploads: { resolve: mocks.resolve },
}));
vi.mock('$lib/api/client', () => ({
  api: { artifacts: { textPreview: mocks.textPreview, signedUrl: mocks.signedUrl } },
}));

const image: AttachmentRef = {
  artifact_id: 'local:composer-image', local_id: 'composer-image',
  filename: 'photo.png', mime_type: 'image/png', kind: 'image',
  size_bytes: 42,
  url: 'blob:composer-photo', upload_state: 'uploading',
};

describe('ComposerAttachments previews', () => {
  beforeEach(() => vi.clearAllMocks());

  it('deletes only my comments on an attachment removed before sending', async () => {
    const remove = vi.fn();
    const uploaded: AttachmentRef = { artifact_id: 'att_pending', kind: 'file', filename: 'notes.txt', mime_type: 'text/plain', size_bytes: 4 };
    mocks.textPreview.mockResolvedValue({ content: 'Notes', truncated: false });
    mocks.feedbackResolve.mockResolvedValue({ subject_id: 'subject_pending' });
    const mine = { thread_id: 't1', hidden: false, entries: [{ author: { is_self: true } }] };
    const guest = { thread_id: 't2', hidden: false, entries: [{ author: { is_self: false } }] };
    mocks.feedbackThreads.mockResolvedValue({ items: [mine, guest], next_cursor: null });
    mocks.changeThread.mockResolvedValue({});
    render(ComposerAttachments, { attachments: [uploaded], onremove: remove });
    await fireEvent.click(screen.getByRole('button', { name: 'Preview notes.txt' }));
    await vi.dynamicImportSettled();
    await fireEvent.click(screen.getAllByRole('button', { name: 'Remove notes.txt' })[0]);
    expect(remove).toHaveBeenCalledWith('att_pending');
    await waitFor(() => expect(mocks.changeThread).toHaveBeenCalledTimes(1));
    expect(mocks.feedbackResolve).toHaveBeenCalledWith({ kind: 'document', content_ref: 'att_pending' }, expect.anything());
    expect(mocks.changeThread).toHaveBeenCalledWith(mine, { hidden: true });
  });

  it('does not touch comments for an attachment that was never opened', async () => {
    const remove = vi.fn();
    render(ComposerAttachments, { attachments: [{ artifact_id: 'att_unopened', kind: 'file', filename: 'a.txt', mime_type: 'text/plain', size_bytes: 1 }], onremove: remove });
    await fireEvent.click(screen.getAllByRole('button', { name: 'Remove a.txt' })[0]);
    expect(remove).toHaveBeenCalledWith('att_unopened');
    await Promise.resolve();
    expect(mocks.feedbackResolve).not.toHaveBeenCalled();
  });

  it('enlarges an unsent image without removing or uploading it', async () => {
    const remove = vi.fn();
    render(ComposerAttachments, { attachments: [image], onremove: remove });
    await fireEvent.click(screen.getByRole('button', { name: 'Preview photo.png' }));
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(screen.getByRole('dialog').querySelector('img')?.src).toBe('blob:composer-photo');
    expect(remove).not.toHaveBeenCalled();
    expect(mocks.resolve).not.toHaveBeenCalled();
  });

  it('resolves an uploaded local reference before reading an unsent text document', async () => {
    const attachment: AttachmentRef = {
      artifact_id: 'local:composer-report', local_id: 'composer-report', kind: 'file',
      filename: 'report.txt', mime_type: 'text/plain', upload_state: 'uploaded',
      size_bytes: 42,
    };
    mocks.resolve.mockResolvedValue({ artifact_id: 'att_composer_report', kind: 'file', filename: 'report.txt', mime_type: 'text/plain' });
    mocks.textPreview.mockResolvedValue({ content: 'Unsent report contents', truncated: false });
    render(ComposerAttachments, { attachments: [attachment], onremove: vi.fn() });
    await fireEvent.click(screen.getByRole('button', { name: 'Preview report.txt' }));
    await vi.dynamicImportSettled();
    expect(await screen.findByText('Unsent report contents')).toBeTruthy();
    expect(mocks.resolve).toHaveBeenCalledWith(attachment);
    expect(mocks.textPreview).toHaveBeenCalledWith('att_composer_report');
  });

  it('does not ask the server to preview a still-uploading local document', () => {
    render(ComposerAttachments, { attachments: [{ ...image, filename: 'report.txt', mime_type: 'text/plain' }], onremove: vi.fn() });
    expect(screen.getByRole('button', { name: 'Preview report.txt' })).toBeDisabled();
    expect(mocks.textPreview).not.toHaveBeenCalled();
  });

  it('shows a recoverable error if resolving an uploaded attachment fails', async () => {
    mocks.resolve.mockRejectedValue(new Error('Unavailable'));
    render(ComposerAttachments, { attachments: [{ ...image, filename: 'report.txt', mime_type: 'text/plain', upload_state: 'uploaded' }], onremove: vi.fn() });
    await fireEvent.click(screen.getByRole('button', { name: 'Preview report.txt' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not open attachment'));
  });

  it('does not open a removed attachment after its pending resolution completes', async () => {
    let complete!: (value: AttachmentRef) => void;
    mocks.resolve.mockReturnValue(new Promise<AttachmentRef>(resolve => { complete = resolve; }));
    const attachment = { ...image, filename: 'report.txt', mime_type: 'text/plain', upload_state: 'uploaded' as const };
    const { rerender } = render(ComposerAttachments, { attachments: [attachment], onremove: vi.fn() });
    await fireEvent.click(screen.getByRole('button', { name: 'Preview report.txt' }));
    await rerender({ attachments: [] });
    complete({ ...attachment, artifact_id: 'att_composer_report', local_id: undefined });
    await vi.dynamicImportSettled();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocks.textPreview).not.toHaveBeenCalled();
  });

  it('reports unavailable HTML preview coordination without an unhandled rejection', async () => {
    mocks.capture.mockImplementation(() => { throw new Error('Coordination unavailable'); });
    render(ComposerAttachments, { attachments: [{ ...image, artifact_id: 'att_composer_html', local_id: undefined, filename: 'report.html', mime_type: 'text/html', upload_state: 'uploaded' }], onremove: vi.fn() });
    await fireEvent.click(screen.getByRole('button', { name: 'Preview report.html' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not open attachment'));
    expect(mocks.signedUrl).not.toHaveBeenCalled();
  });
});

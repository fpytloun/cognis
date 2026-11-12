import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import ImageLightbox from './ImageLightbox.svelte';
const mocks = vi.hoisted(() => ({ revisions: vi.fn(), signedUrl: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { artifacts: mocks } }));

describe('ImageLightbox', () => {
  it('discovers history for a chat image without a revision count and changes the image', async () => {
    mocks.revisions.mockResolvedValue({ revisions: [
      { artifact_id: 'art_image', revision: 2, is_latest: true },
      { artifact_id: 'art_older', revision: 1, is_latest: false },
    ] });
    mocks.signedUrl.mockResolvedValue({ url: '/earlier-image.png' });
    const onClose = vi.fn();
    render(ImageLightbox, {
      src: '/image.png', images: [{ src: '/image.png', filename: 'image.png', artifact_id: 'art_image' }], onClose,
    });
    const toggle = await screen.findByTestId('image-revision-toggle');
    expect(toggle).toHaveTextContent('Revision 2 of 2');
    expect(toggle.closest('.compact-header-more')).toBeNull();
    await fireEvent.click(toggle);
    const options = await screen.findAllByTestId('image-revision-option');
    await fireEvent.click(options[1]);
    await waitFor(() => expect(screen.getByRole('img')).toHaveAttribute('src', '/earlier-image.png'));
    expect(screen.getByTestId('image-revision-toggle')).toHaveTextContent('Revision 1 of 2');
    expect(mocks.signedUrl).toHaveBeenCalledWith('art_older', 3600, 'download');
    expect(onClose).not.toHaveBeenCalled();
  });
  it('portals above chat chrome so its image controls remain visible', async () => {
    const onClose = vi.fn();
    render(ImageLightbox, {
      src: '/test-image.png',
      alt: 'Generated portrait',
      filename: 'portrait.png',
      onClose,
    });

    const dialog = screen.getByRole('dialog', { name: 'portrait.png' });
    await waitFor(() => expect(dialog.parentElement).toBe(document.body));
    expect(screen.getByRole('button', { name: 'Download portrait.png' })).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    const close = screen.getByRole('button', { name: 'Close image viewer' });
    expect(close).toBeInTheDocument();
    await fireEvent.click(close);
    expect(onClose).toHaveBeenCalledOnce();
  });
});

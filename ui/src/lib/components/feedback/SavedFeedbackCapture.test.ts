import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import SavedFeedbackCapture from './SavedFeedbackCapture.svelte';
import type { FeedbackLocator, FeedbackSurfaceBinding } from '$lib/feedback/types';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
it('aligns saved CSS-document markup on an unequal raster without changing live interaction', async () => {
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:https://cognis.test/saved-observation');
  const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
  const locator: Extract<FeedbackLocator, { kind: 'html' }> = {
    kind: 'html', element_key: null, selector_path: null, range: null,
    viewport: { width_css_px: 800, height_css_px: 600, scroll_x_css_px: 120, scroll_y_css_px: 900, device_pixel_ratio: 2 },
    render_state_digest: 'a'.repeat(64), capture_id: 'saved-observation',
    shapes: [{ kind: 'rectangle', x: 320, y: 1050, width: 400, height: 300 }],
  };
  const binding: FeedbackSurfaceBinding = {
    context: { kind: 'standalone' }, mode: 'draw', selected_thread_id: null, highlights: [],
    supported_locator_kinds: ['html'], completeness: 'available',
    source: { subject_id: 'saved-document', source_token: 'saved-document-proof',
      source: { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
      context: { kind: 'standalone' }, content_digest: 'a'.repeat(64), lineage_id: null, revision: 1,
      latest_content_ref: null, availability: 'available', title: 'Saved observation', format: 'html',
      capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false },
      canonical_text: null, permitted_locator_kinds: ['html'] },
    resolveTextScope: async () => { throw new Error('Saved pixels do not resolve live text'); },
    onAnchorCandidate: vi.fn(), onSelectThread: vi.fn(), onInteractionChange: vi.fn(),
    registerAdapter: () => () => {},
  };
  const view = render(SavedFeedbackCapture, { captureId: 'saved-observation', locator, threadId: 'saved-thread',
    binding, loadPixels: async () => new Blob(['pixels'], { type: 'image/png' }) });
  const image = await screen.findByRole('img');
  Object.defineProperties(image, { naturalWidth: { value: 1600 }, naturalHeight: { value: 900 } });
  await fireEvent.load(image);
  const overlay = screen.getByLabelText('Annotation highlights');
  expect(overlay).toHaveAttribute('viewBox', '0 0 1 1');
  expect(overlay.querySelector('path')).toHaveAttribute('d', 'M 0.25 0.25 h 0.5 v 0.5 h -0.5 Z');
  expect(binding.onInteractionChange).not.toHaveBeenCalled();
  expect(binding.onAnchorCandidate).not.toHaveBeenCalled();
  view.unmount();
  await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:https://cognis.test/saved-observation'));
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { FeedbackSurfaceBinding } from '$lib/feedback/types';
import Harness from './AnnotationHarness.svelte';

const mocks = vi.hoisted(() => ({ crop: vi.fn(), dimensions: vi.fn(), capture: vi.fn() }));
vi.mock('$lib/feedback/annotations/capture', () => ({ cropCapture: mocks.crop, rasterDimensions: mocks.dimensions, captureScreen: mocks.capture }));
vi.mock('$lib/feedback/annotations/digest', () => ({ textDigest: vi.fn().mockResolvedValue('render-state'), bytesDigest: vi.fn().mockResolvedValue('frozen-pixels') }));
const originalURL = globalThis.URL;
function binding(): FeedbackSurfaceBinding {
  return { source: { subject_id: 'html-subject', source_token: 'html-capture-source', source: { kind: 'document', content_ref: 'art_html_capture' },
    content_digest: 'html-bytes', availability: 'available', permitted_locator_kinds: ['whole', 'html'], context: { kind: 'chat', conversation_id: 'capture-origin' },
    lineage_id: null, revision: 1, latest_content_ref: null, title: 'HTML', format: 'html',
    canonical_text: null, capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false } },
    context: { kind: 'chat', conversation_id: 'capture-origin' }, mode: 'area', selected_thread_id: null, completeness: 'available', highlights: [],
    supported_locator_kinds: ['whole', 'html'], onAnchorCandidate: vi.fn(), onTargetChange: vi.fn(), onSelectThread: vi.fn(), onInteractionChange: vi.fn(),
    registerAdapter: vi.fn(() => () => {}), resolveTextScope: vi.fn() };
}
function raster() {
  const blob = new Blob(['confirmed raster'], { type: 'image/png' });
  Object.defineProperty(blob, 'arrayBuffer', { value: async () => new ArrayBuffer(4) });
  return blob;
}
beforeEach(() => {
  mocks.crop.mockReset().mockResolvedValue(raster());
  mocks.dimensions.mockResolvedValue({ width: 640, height: 400 });
  mocks.capture.mockRejectedValue(new Error('Native capture is unavailable.'));
  globalThis.URL = class extends originalURL {
    static createObjectURL = vi.fn(() => 'blob:https://capture.test/local');
    static revokeObjectURL = vi.fn();
  };
});
afterEach(() => { globalThis.URL = originalURL; });
async function choose() {
  await fireEvent.change(screen.getByLabelText(/Choose.*screenshot/), { target: { files: [new File(['png'], 'screenshot.png', { type: 'image/png' })] } });
  await screen.findByAltText('Unconfirmed screenshot, local only');
}
describe('source-bound local capture confirmation', () => {
  it('never emits a raster until explicit crop confirmation and maps markup into the confirmed viewport', async () => {
    const value = binding(); render(Harness, { binding: value, kind: 'capture' }); await choose();
    expect(value.onAnchorCandidate).not.toHaveBeenCalled();
    expect(screen.getByText('Confirm crop and freeze')).toBeDisabled();
    await fireEvent.input(screen.getByLabelText('Width'), { target: { value: '100' } });
    await fireEvent.input(screen.getByLabelText('Height'), { target: { value: '200' } });
    await fireEvent.click(screen.getByLabelText(/The crop shows the entire intended artifact viewport/));
    await fireEvent.click(screen.getByText('Confirm crop and freeze'));
    await screen.findByAltText('Confirmed frozen HTML observation');
    expect(value.onAnchorCandidate).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByText('Add pin'));
    expect(mocks.crop).toHaveBeenCalledWith(expect.any(File), { x: 0, y: 0, width: 100, height: 200 }, expect.any(AbortSignal));
    expect(value.onAnchorCandidate).not.toHaveBeenCalled();
    expect(value.onTargetChange).toHaveBeenCalledWith(expect.objectContaining({ candidate: expect.objectContaining({
      locator: expect.objectContaining({ kind: 'html', shapes: [{ kind: 'pin', point: { x: 420, y: 330 } }] }),
      capture: expect.objectContaining({ width: 100, height: 200, blob: expect.any(Blob), render_state_digest: 'render-state' }),
    }) }));
    expect(screen.getByText('Unverified user-provided screenshot. Its authenticity is not asserted.')).toBeTruthy();
  });
  it('invalidates pending crops on local discard and context replacement without changing the shell draft', async () => {
    let resolve!: (blob: Blob) => void;
    mocks.crop.mockImplementationOnce(() => new Promise<Blob>(done => { resolve = done; }));
    const first = binding(); const view = render(Harness, { binding: first, kind: 'capture' }); await choose();
    await fireEvent.click(screen.getByLabelText(/The crop shows the entire intended artifact viewport/));
    await fireEvent.click(screen.getByText('Confirm crop and freeze'));
    await fireEvent.click(screen.getByText('Discard local capture'));
    const next: FeedbackSurfaceBinding = { ...binding(), context: { kind: 'chat', conversation_id: 'replacement-origin' } };
    await view.rerender({ binding: next, kind: 'capture' });
    resolve(raster());
    await waitFor(() => expect(screen.queryByAltText('Confirmed frozen HTML observation')).toBeNull());
    expect(first.onAnchorCandidate).not.toHaveBeenCalled(); expect(next.onAnchorCandidate).not.toHaveBeenCalled();
  });
  it('offers bounded raster and whole-source feedback after native capture denial', async () => {
    const value = binding(); render(Harness, { binding: value, kind: 'capture' });
    await fireEvent.click(screen.getByText('Capture current view'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose or paste a PNG or WebP screenshot');
    expect(value.onAnchorCandidate).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByText('Comment on whole source'));
    expect(value.onAnchorCandidate).toHaveBeenCalledWith({ locator: { kind: 'whole' } });
  });
});

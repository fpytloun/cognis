import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import PdfAttachmentViewer from './PdfAttachmentViewer.svelte';
import AnnotationHarness from './feedback/annotations/AnnotationHarness.svelte';
import type { FeedbackSurfaceBinding } from '$lib/feedback/types';
import { tick } from 'svelte';

const mocks = vi.hoisted(() => ({
  getPage: vi.fn(), destroy: vi.fn().mockResolvedValue(undefined), getDocument: vi.fn(),
  terminate: vi.fn(), destroyWorker: vi.fn(), create: vi.fn(),
  textLayerRender: vi.fn().mockResolvedValue(undefined), textLayerCancel: vi.fn(),
}));
vi.mock('$app/paths', () => ({ base: '', assets: '' }));
vi.mock('pdfjs-dist/legacy/build/pdf.mjs', () => ({
  PDFWorker: { create: mocks.create }, getDocument: mocks.getDocument,
  TextLayer: class {
    constructor({ container }: { container: HTMLElement }) {
      const span = document.createElement('span'); span.textContent = 'PDF selectable text'; container.append(span);
    }
    render() { return mocks.textLayerRender(); }
    cancel() { return mocks.textLayerCancel(); }
  },
}));
vi.mock('pdfjs-dist/legacy/build/pdf.worker.mjs?worker&inline', () => ({
  default: class { terminate = mocks.terminate; },
}));

function binding(): FeedbackSurfaceBinding {
  return { source: { subject_id: 'pdf-subject', source_token: 'pdf-source', source: { kind: 'document', content_ref: 'art_pdf_page' },
    content_digest: 'pdf-bytes', availability: 'available', permitted_locator_kinds: ['pdf', 'whole'], context: { kind: 'standalone' },
    lineage_id: null, revision: 1, latest_content_ref: null, title: 'PDF', format: 'pdf',
    canonical_text: null, capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false } },
    context: { kind: 'standalone' }, mode: 'view', selected_thread_id: null, completeness: 'available', highlights: [], supported_locator_kinds: ['pdf', 'whole'],
    onAnchorCandidate: vi.fn(), onSelectThread: vi.fn(), onInteractionChange: vi.fn(), resolveTextScope: vi.fn(), registerAdapter: vi.fn(() => () => {}) };
}
function configure(onResize?: (callback: (entries: unknown[]) => void) => void) {
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: (entries: unknown[]) => void) { onResize?.(callback); }
    observe() { this.callback([{ contentRect: { width: 800, height: 600 } }]); }
    disconnect() {}
  });
  const render = vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() }));
  mocks.getPage.mockResolvedValue({ getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale, scale, rotation: 0 }),
    streamTextContent: vi.fn(), render, cleanup: vi.fn() });
  mocks.getDocument.mockReturnValue({ promise: Promise.resolve({ numPages: 3, getPage: mocks.getPage }), destroy: mocks.destroy });
  mocks.create.mockReturnValue({ destroy: mocks.destroyWorker });
  return render;
}
describe('PdfAttachmentViewer', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });
  it('keeps the same rendered canvas and text layer when comment tools or bindings change', async () => {
    const renderPage = configure(), value = binding();
    const view = render(AnnotationHarness, { binding: value, kind: 'pdf' });
    await screen.findByText('PDF selectable text'); await tick();
    const canvas = view.container.querySelector('canvas'), text = view.container.querySelector('.textLayer span');
    const renders = renderPage.mock.calls.length, loads = mocks.getDocument.mock.calls.length;
    for (const mode of ['comment', 'area', 'draw', 'comment', 'view'] as const) {
      await view.rerender({ binding: { ...value, mode }, kind: 'pdf' });
      await tick();
      expect(view.container.querySelector('canvas')).toBe(canvas);
      expect(view.container.querySelector('.textLayer span')).toBe(text);
      expect(renderPage).toHaveBeenCalledTimes(renders);
      expect(mocks.getDocument).toHaveBeenCalledTimes(loads);
    }
    const draw = { ...value, mode: 'draw' as const };
    await view.rerender({ binding: draw, kind: 'pdf' }); await tick();
    vi.mocked(value.onInteractionChange).mockClear();
    await view.rerender({ binding: { ...draw, selected_thread_id: crypto.randomUUID() }, kind: 'pdf' }); await tick();
    expect(value.onInteractionChange).not.toHaveBeenCalled();
    await view.unmount();
  });
  it('retains proved text geometry through focus and a same-page viewport refit, but not source replacement', async () => {
    let resize!: (entries: unknown[]) => void;
    const renderPage = configure(callback => { resize = callback; });
    const value: FeedbackSurfaceBinding = { ...binding(), mode: 'comment', onTargetChange: vi.fn() };
    const text = 'PDF selectable text', digest = 'a'.repeat(64);
    vi.mocked(value.resolveTextScope).mockResolvedValue({
      scope: { kind: 'pdf_page', page: 1 }, content_digest: value.source.content_digest,
      descriptor: { representation: 'feedback-text-v1', text_digest: digest, length: text.length, available_ranges: [[0, text.length]] },
      text_page: { representation: 'feedback-text-v1', text_digest: digest, start: 0, end: text.length, total_length: text.length, text },
    });
    const previous = Object.getOwnPropertyDescriptor(Range.prototype, 'getClientRects');
    const quote = { left: 495, top: 249, width: 155, height: 8 };
    Object.defineProperty(Range.prototype, 'getClientRects', { configurable: true, value: () => [quote] });
    const view = render(AnnotationHarness, { binding: value, kind: 'pdf' });
    try {
      await screen.findByText(text);
      const node = view.container.querySelector('.textLayer span')!.firstChild!;
      const range = document.createRange(); range.selectNodeContents(node);
      document.getSelection()!.removeAllRanges(); document.getSelection()!.addRange(range);
      await fireEvent(document, new Event('selectionchange'));
      await waitFor(() => expect(value.onTargetChange).toHaveBeenCalledWith(expect.objectContaining({
        candidate: { locator: expect.objectContaining({ kind: 'pdf', shapes: [], range: expect.objectContaining({ start: 0, end: text.length, exact: text }) }) },
      })));
      const target = vi.mocked(value.onTargetChange!).mock.calls.find(([target]) => target)?.[0]!;
      const geometry = () => vi.mocked(value.registerAdapter).mock.calls.at(-1)![1].targetGeometry!(target.candidate.locator);
      expect(geometry()).toEqual({ space: 'client', rects: [quote] });
      const textarea = document.createElement('textarea'); view.container.append(textarea); textarea.focus();
      document.getSelection()!.removeAllRanges();
      resize([{ contentRect: { width: 800, height: 592 } }]);
      await waitFor(() => expect(renderPage).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(geometry()).toEqual({ space: 'client', rects: [quote] }));
      expect(document.activeElement).toBe(textarea);
      const locator = target.candidate.locator;
      if (locator.kind !== 'pdf' || !locator.range) throw new Error('Expected a proved PDF text anchor');
      const adapter = vi.mocked(value.registerAdapter).mock.calls.at(-1)![1];
      expect(adapter.targetGeometry!({ ...locator, range: { ...locator.range, text_digest: 'b'.repeat(64) } })).toBeNull();
      expect(adapter.targetGeometry!({ ...locator, page: 2 })).toBeNull();
      await view.rerender({ binding: { ...value, source: { ...value.source, source_token: crypto.randomUUID() } }, kind: 'pdf' });
      expect(geometry()).toBeNull();
    } finally {
      await view.unmount();
      if (previous) Object.defineProperty(Range.prototype, 'getClientRects', previous);
      else Reflect.deleteProperty(Range.prototype, 'getClientRects');
    }
  });
  it('locates a page only after its requested render generation finishes', async () => {
    const renderPage = configure(), value = binding();
    const view = render(AnnotationHarness, { binding: value, kind: 'pdf' });
    await screen.findByText('PDF selectable text'); await tick();
    const page = view.container.querySelector('canvas')!.parentElement!;
    page.scrollIntoView = vi.fn();
    let finish!: () => void, settled = false;
    renderPage.mockReturnValueOnce({ promise: new Promise<void>(resolve => { finish = resolve; }), cancel: vi.fn() });
    const adapter = vi.mocked(value.registerAdapter).mock.calls.at(-1)![1];
    const located = Promise.resolve(adapter.locate({ kind: 'pdf', page: 2, page_width_pt: 600, page_height_pt: 800, rotation: 0, shapes: [], range: null }))
      .then(result => { settled = true; return result; });
    await waitFor(() => expect(mocks.getPage).toHaveBeenCalledWith(2));
    expect(settled).toBe(false); finish();
    await expect(located).resolves.toMatchObject({ status: 'exact' });
    expect(page.scrollIntoView).toHaveBeenCalledOnce();
    await view.unmount(); await waitFor(() => expect(mocks.terminate).toHaveBeenCalledOnce());
  });
  it('reports unavailable rather than exact when the requested page render fails', async () => {
    const renderPage = configure(), value = binding();
    const view = render(AnnotationHarness, { binding: value, kind: 'pdf' });
    await screen.findByText('PDF selectable text'); await tick();
    renderPage.mockImplementationOnce(() => ({ promise: Promise.reject(new Error('Requested render failed.')), cancel: vi.fn() }));
    const adapter = vi.mocked(value.registerAdapter).mock.calls.at(-1)![1];
    await expect(adapter.locate({ kind: 'pdf', page: 2, page_width_pt: 600, page_height_pt: 800, rotation: 0, shapes: [], range: null }))
      .resolves.toMatchObject({ status: 'unavailable' });
    await view.unmount(); await waitFor(() => expect(mocks.terminate).toHaveBeenCalledOnce());
  });
  it('fits pages, navigates the whole document, and destroys resources on close', async () => {
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: (entries: unknown[]) => void) {}
      observe() { this.callback([{ contentRect: { width: 390, height: 600 } }]); }
      disconnect() {}
    });
    const renderPage = vi.fn().mockReturnValue({ promise: Promise.resolve(), cancel: vi.fn() });
    mocks.getPage.mockResolvedValue({
      getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale, scale, rotation: 0 }),
      streamTextContent: vi.fn(),
      render: renderPage,
      cleanup: vi.fn(),
    });
    mocks.getDocument.mockReturnValue({
      promise: Promise.resolve({ numPages: 3, getPage: mocks.getPage }),
      destroy: mocks.destroy,
    });
    mocks.create.mockReturnValue({ destroy: mocks.destroyWorker });
    const { unmount } = render(PdfAttachmentViewer, { url: '/signed.pdf' });
    await vi.dynamicImportSettled();
    await waitFor(() => expect(mocks.getPage).toHaveBeenCalledWith(1));
    await screen.findByText('PDF selectable text');
    expect(mocks.textLayerRender).toHaveBeenCalled();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(mocks.create).toHaveBeenCalledWith({ port: expect.any(Object) });
    expect(mocks.getDocument).toHaveBeenCalledWith(expect.objectContaining({
      worker: mocks.create.mock.results[0].value,
      cMapUrl: new URL('/pdfjs/cmaps/', window.location.href).href,
      cMapPacked: true,
      standardFontDataUrl: new URL('/pdfjs/standard_fonts/', window.location.href).href,
      wasmUrl: new URL('/pdfjs/wasm/', window.location.href).href,
      useWorkerFetch: true,
    }));
    await fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(mocks.getPage).toHaveBeenCalledWith(2));
    await fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(mocks.getPage).toHaveBeenCalledWith(3));
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    const canvas = document.querySelector('canvas')!;
    expect(parseFloat(canvas.style.width)).toBeLessThanOrEqual(390);
    await unmount();
    expect(mocks.destroy).toHaveBeenCalledOnce();
    expect(mocks.destroyWorker).toHaveBeenCalledOnce();
    expect(mocks.terminate).toHaveBeenCalledOnce();
    expect(mocks.textLayerCancel).toHaveBeenCalled();
  });
  it('uses absolute zoom below fit width, true CSS actual size, and explicit responsive fit modes', async () => {
    let resize!: (entries: unknown[]) => void;
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: (entries: unknown[]) => void) { resize = callback; }
      observe() { resize([{ contentRect: { width: 2000, height: 1500 } }]); }
      disconnect() {}
    });
    vi.stubGlobal('devicePixelRatio', 2);
    mocks.getPage.mockResolvedValue({
      getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale, scale, rotation: 0 }),
      streamTextContent: vi.fn(),
      render: vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() })),
      cleanup: vi.fn(),
    });
    mocks.create.mockReturnValue({ destroy: mocks.destroyWorker });
    mocks.getDocument.mockReturnValue({
      promise: Promise.resolve({ numPages: 2, getPage: mocks.getPage }), destroy: mocks.destroy,
    });
    const { unmount } = render(PdfAttachmentViewer, { url: '/signed.pdf' });
    await vi.dynamicImportSettled();
    const canvas = document.querySelector('canvas')!;
    const percent = screen.getByRole('spinbutton', { name: 'Zoom percentage' });
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(800));
    expect(percent).toHaveValue(100);
    expect(canvas.width).toBe(1600);
    await fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(600));
    expect(percent).toHaveValue(75);
    await fireEvent.change(percent, { target: { value: '25' } });
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(200));
    resize([{ contentRect: { width: 500, height: 600 } }]);
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(200));
    await fireEvent.click(screen.getByRole('button', { name: 'Actual size (100%)' }));
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(800));
    await fireEvent.click(screen.getByRole('button', { name: 'Fit width' }));
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(484));
    resize([{ contentRect: { width: 1000, height: 600 } }]);
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(984));
    await fireEvent.click(screen.getByRole('button', { name: 'Fit page' }));
    await waitFor(() => expect(parseFloat(canvas.style.height)).toBeCloseTo(584));
    await fireEvent.change(percent, { target: { value: '1' } });
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(80));
    expect(percent).toHaveValue(10);
    expect(screen.getByRole('button', { name: 'Zoom out' })).toBeDisabled();
    await fireEvent.change(percent, { target: { value: '500' } });
    await waitFor(() => expect(parseFloat(canvas.style.width)).toBeCloseTo(3200));
    expect(percent).toHaveValue(400);
    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeDisabled();
    await fireEvent.change(percent, { target: { value: '900' } });
    expect(percent).toHaveValue(400);
    await unmount();
  });
  it('contains invalid PDF failures in the preview and releases the worker', async () => {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    mocks.create.mockReturnValue({ destroy: mocks.destroyWorker });
    mocks.getDocument.mockImplementation(() => ({
      promise: Promise.reject(new Error('Invalid PDF')),
      destroy: mocks.destroy,
    }));
    const { unmount } = render(PdfAttachmentViewer, { url: '/invalid.pdf' });
    await vi.dynamicImportSettled();
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('The PDF could not be loaded'));
    await unmount();
    expect(mocks.destroy).toHaveBeenCalledOnce();
    expect(mocks.destroyWorker).toHaveBeenCalledOnce();
    expect(mocks.terminate).toHaveBeenCalledOnce();
  });
  it('keeps the worker alive until transport destruction settles', async () => {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    let finish!: () => void;
    mocks.destroy.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    mocks.create.mockReturnValue({ destroy: mocks.destroyWorker });
    mocks.getDocument.mockReturnValue({
      promise: Promise.resolve({ numPages: 1 }),
      destroy: mocks.destroy,
    });
    const { unmount } = render(PdfAttachmentViewer, { url: '/signed.pdf' });
    await vi.dynamicImportSettled();
    await screen.findByText('Page 1 / 1');
    await unmount();
    expect(mocks.destroy).toHaveBeenCalledOnce();
    expect(mocks.destroyWorker).not.toHaveBeenCalled();
    expect(mocks.terminate).not.toHaveBeenCalled();
    finish();
    await waitFor(() => expect(mocks.terminate).toHaveBeenCalledOnce());
    expect(mocks.destroyWorker).toHaveBeenCalledOnce();
  });
});

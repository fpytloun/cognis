<script lang="ts">
  import DocumentFind from './DocumentFind.svelte';
  import { createDocumentFind, documentMatches, type DocumentFindAdapter } from '$lib/document-find';
  import { onMount } from 'svelte';
  import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
  import { assets, base } from '$app/paths';
  import { tick } from 'svelte';
  import { getFeedbackSurface } from '$lib/feedback/context';
  import VisualAnnotations from './feedback/annotations/VisualAnnotations.svelte';
  import FeedbackIndicator from './feedback/FeedbackIndicator.svelte';
  import ScopedTextSelection from './feedback/annotations/ScopedTextSelection.svelte';
  import { mapShapes } from '$lib/feedback/annotations/shapes';
  import { pdfDisplayPoint, pdfPagePoint } from '$lib/feedback/annotations/geometry';
  import 'pdfjs-dist/web/pdf_viewer.css';
  import { domTextRange, selectText, rangeOffsets } from '$lib/feedback/annotations/text';
  import { nativeSelection, rangeGeometry, elementGeometry, clearNativeSelection } from '$lib/feedback/annotations/native-selection';
  import type { FeedbackLocator } from '$lib/feedback/types';
  import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
  import { preparePdfTextLayer, mapPdfRange } from '$lib/feedback/annotations/pdf-layer';
  import { exposeGeometry } from '$lib/feedback/annotations/expose-target';
  const feedback = getFeedbackSurface();
  const binding = $derived(feedback?.binding);
  let textContainer = $state<HTMLDivElement>();
  let pageBox = $state<HTMLDivElement>();
  let pageWidth = $state(1), pageHeight = $state(1), displayWidth = $state(1), displayHeight = $state(1);
  let rotation = $state<0 | 90 | 180 | 270>(0);
  let textLayer: import('pdfjs-dist').TextLayer | null = null;
  let renderedPage = $state(0);
  let selectedText = $state('');
  let textError = $state('');
  let markup = $state<{ clearSelection: () => void; targetGeometry: (locator: FeedbackLocator) => import('$lib/feedback/types').FeedbackTargetGeometry | null }>();
  let scoped = $state<{ clearSelection: () => void }>();
  let selectionGeneration = 0;
  let canonicalPage: { identity: string; page: number; digest: string; text: string } | null = null;
  async function readPage() {
    const current = binding, page = pageNumber, token = surfaceIdentity(current);
    if (!current || renderedPage !== page) throw new Error('Wait for the PDF page to finish rendering.');
    let result = await current.resolveTextScope({ kind: 'pdf_page', page }, { start: 0, limit: 32768 });
    const descriptor = result.descriptor;
    let text = result.text_page.text;
    while (result.text_page.end < result.text_page.total_length) {
      if (text.length >= 1_048_576) throw new Error('This page is too large to prove a text mapping. Use an explicit area target.');
      result = await current.resolveTextScope({ kind: 'pdf_page', page }, { start: result.text_page.end, limit: 32768 });
      if (!result.text_page.text) throw new Error('Canonical PDF text is incomplete.');
      text += result.text_page.text;
    }
    if (token !== surfaceIdentity(feedback?.binding) || page !== pageNumber) throw new Error('The PDF source changed.');
    canonicalPage = { identity: token, page, digest: descriptor.text_digest, text };
    return { text, descriptor };
  }
  async function mapText(nativeRange: Range) {
    if (!textContainer) throw new Error('The PDF text layer is unavailable.');
    const page = pageNumber, position = rangeOffsets(textContainer, nativeRange);
    if (!position) throw new Error('Select text on one PDF page.');
    const { text, descriptor } = await readPage();
    const mapped = mapPdfRange(textContainer.textContent ?? '', text, position.start, position.end);
    if (!mapped) throw new Error('PDF glyph order does not match canonical page text. Use Mark area or the canonical text fallback.');
    const range = selectText(text, mapped.start, mapped.end, descriptor.available_ranges.map(([start, end]) => ({ start, end })));
    return { candidate: { locator: { kind: 'pdf' as const, page, page_width_pt: pageWidth, page_height_pt: pageHeight, rotation, shapes: [],
      range: { ...range, representation: 'feedback-text-v1' as const, text_digest: descriptor.text_digest } } }, label: nativeRange.toString() };
  }
  function textGeometry(anchor: FeedbackLocator) {
    if (anchor.kind !== 'pdf' || anchor.page !== renderedPage) return null;
    const canonical = canonicalPage;
    if (!anchor.range || !canonical || canonical.identity !== surfaceIdentity(binding) ||
      canonical.page !== anchor.page || canonical.digest !== anchor.range.text_digest) return null;
    const position = mapPdfRange(canonical.text, textContainer?.textContent ?? '', anchor.range.start, anchor.range.end);
    const range = position && textContainer ? domTextRange(textContainer, position.start, position.end) : null;
    return range ? rangeGeometry(range) : null;
  }
  const instance = crypto.randomUUID();
  const highlights = $derived(binding?.highlights.filter(h => h.locator.kind === 'pdf' && h.locator.page === pageNumber && h.category !== 'not_located')
    .map(h => ({ ...h, shapes: h.locator.kind === 'pdf' ? mapShapes(h.locator.shapes, point => pdfDisplayPoint(point, { width: pageWidth, height: pageHeight }, rotation)) : [] })) ?? []);
  $effect(() => {
    if (!binding) return;
    return binding.registerAdapter(instance, {
      clearSelection: () => { selectionGeneration++; markup?.clearSelection(); scoped?.clearSelection(); selectedText = ''; clearNativeSelection(textContainer); },
      targetGeometry: anchor => {
        if (anchor.kind !== 'pdf' || anchor.page !== renderedPage) return null;
        if (anchor.range) return textGeometry(anchor);
        return markup?.targetGeometry?.(anchor) ?? elementGeometry(pageBox);
      },
      exposeTarget: (anchor, visible) => exposeGeometry(pageBox, () => {
        if (anchor.kind !== 'pdf' || anchor.page !== renderedPage) return null;
        return anchor.range ? textGeometry(anchor) : markup?.targetGeometry(anchor) ?? null;
      }, visible),
      locate: async anchor => {
        if (anchor.kind !== 'pdf' || !documentProxy || anchor.page > documentProxy.numPages) return { status: 'not_located' };
        const token = surfaceIdentity(feedback?.binding), requestedPage = anchor.page;
        pageNumber = requestedPage;
        await tick();
        const outcome = currentRender;
        if (renderedPage !== requestedPage && (!outcome || outcome.page !== requestedPage || !await outcome.done)) return { status: 'unavailable', reason: 'The requested PDF page did not finish rendering.' };
        if (surfaceIdentity(feedback?.binding) !== token || renderedPage !== requestedPage || pageNumber !== requestedPage) return { status: 'not_located' };
        pageBox?.scrollIntoView({ block: 'center' });
        if (anchor.range) {
          try {
            await readPage();
            if (surfaceIdentity(feedback?.binding) !== token || renderedPage !== requestedPage) return { status: 'not_located' };
            if (!textGeometry(anchor)) return { status: 'unavailable', reason: 'Page shown, but the exact PDF text range cannot be proved. Use an explicit area target.' };
          } catch { return { status: 'unavailable', reason: 'Page shown; canonical text is unavailable. Use an explicit area target.' }; }
        }
        return { status: 'exact' };
      },
    });
  });

  let { url }: { url: string } = $props();
  let documentProxy = $state<PDFDocumentProxy | null>(null);
  let pageNumber = $state(1);
  let zoom = $state(1);
  let sizing = $state<'auto' | 'page' | 'width' | 'manual'>('auto');
  let effectiveZoom = $state(1);
  let selectedZoom = $derived(sizing === 'manual' ? zoom : effectiveZoom);
  let width = $state(0);
  let height = $state(0);
  let viewport = $state<HTMLDivElement | null>(null);
  let canvas = $state<HTMLCanvasElement | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let renderTask: RenderTask | null = null;
  let generation = 0;
  let currentRender: { page: number; done: Promise<boolean>; resolve: (success: boolean) => void } | null = null;
  const pageTexts = new Map<number, string>();
  let searchVersion = 0, searchQuery = '';
  let matches: { page: number; occurrence: number }[] = [];
  let pageFind: DocumentFindAdapter | null = null;
  let activeMatch = -1;
  function paintSearch(): void {
    pageFind?.clear(); pageFind = null;
    const match = matches[activeMatch];
    if (!textContainer || !match || match.page !== renderedPage) return;
    // PDF.js positions each run as a block; its text offsets still concatenate runs.
    pageFind = createDocumentFind(textContainer, documentMatches, true);
    const count = pageFind.search(searchQuery) as number;
    if (match.occurrence >= count) throw new Error('PDF text unavailable');
    pageFind.select(match.occurrence);
  }
  const finder: DocumentFindAdapter = {
    async search(query) {
      const version = ++searchVersion, pdf = documentProxy;
      matches = []; activeMatch = -1; searchQuery = query; pageFind?.clear(); pageFind = null;
      if (!pdf || !query) return 0;
      const found: typeof matches = [];
      for (let page = 1; page <= pdf.numPages; page++) {
        if (!pageTexts.has(page)) {
          const source = await pdf.getPage(page);
          const text = await source.getTextContent();
          if (version !== searchVersion) return 0;
          pageTexts.set(page, text.items.map(item => 'str' in item ? item.str : '').join(''));
        }
        documentMatches(pageTexts.get(page)!, query).forEach((_, occurrence) => found.push({ page, occurrence }));
      }
      if (version !== searchVersion) return 0;
      matches = found;
      return found.length;
    },
    async select(index) {
      const match = matches[index], version = searchVersion;
      if (!match) return;
      activeMatch = index;
      pageNumber = match.page;
      await tick();
      let outcome = currentRender;
      // A scrollbar/viewport refit may supersede this page's first render.
      // Follow the renderer's existing completion handoff, not a stale cancellation.
      while (version === searchVersion && pageNumber === match.page && renderedPage !== match.page && outcome?.page === match.page) {
        await outcome.done;
        await tick();
        if (outcome === currentRender) break;
        outcome = currentRender;
      }
      if (version !== searchVersion || pageNumber !== match.page || !textContainer) return;
      if (renderedPage !== match.page) throw new Error('PDF page unavailable');
      paintSearch();
    },
    clear() { searchVersion++; matches = []; activeMatch = -1; searchQuery = ''; pageFind?.clear(); pageFind = null; },
  };

  onMount(() => {
    let disposed = false;
    let destroy: (() => Promise<void>) | null = null;
    let worker: Worker | null = null;
    let pdfWorker: import('pdfjs-dist').PDFWorker | null = null;
    const observer = new ResizeObserver(([entry]) => {
      width = Math.max(1, Math.floor(entry.contentRect.width));
      height = Math.max(1, Math.floor(entry.contentRect.height));
    });
    if (viewport) observer.observe(viewport);
    void (async () => {
      try {
        const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
        const { default: PdfWorker } = await import('pdfjs-dist/legacy/build/pdf.worker.mjs?worker&inline');
        const binaryData = __COGNIS_APPLE_BUNDLE__
          ? await import('$lib/native/pdf-binary-data') : null;
        if (disposed) return;
        // Own a real bundled worker, rather than PDF.js's cross-origin module
        // wrapper/fake-worker fallback (blocked by the native host's script CSP).
        worker = new PdfWorker();
        pdfWorker = pdfjs.PDFWorker.create({ port: worker });
        const resources = new URL(`${assets || base}/pdfjs/`, window.location.href).href;
        const task = pdfjs.getDocument({
          url,
          worker: pdfWorker,
          cMapUrl: `${resources}cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${resources}standard_fonts/`,
          wasmUrl: `${resources}wasm/`,
          // Native bytes come from the embedded map; web workers fetch absolute URLs.
          useWorkerFetch: !binaryData,
          // The shipped WASM image decoders require relaxed SIMD, which WebKit
          // does not support. Native builds include PDF.js's JS decoders.
          useWasm: !binaryData,
          ...(binaryData ? { BinaryDataFactory: binaryData.BundledPdfBinaryData } : {}),
        });
        destroy = () => task.destroy();
        const pdf = await task.promise;
        if (!disposed) documentProxy = pdf;
      } catch {
        if (!disposed) error = 'The PDF could not be loaded. It may be encrypted or invalid.';
      } finally { if (!disposed) loading = false; }
    })();
    return () => {
      disposed = true;
      finder.clear();
      generation += 1;
      currentRender?.resolve(false);
      observer.disconnect();
      renderTask?.cancel();
      textLayer?.cancel();
      const releaseWorker = () => {
        pdfWorker?.destroy();
        worker?.terminate();
      };
      // The transport awaits the worker's Terminate acknowledgment before
      // removing fonts/network resources. Keep the port alive until it settles.
      void (destroy?.() ?? Promise.resolve()).then(releaseWorker, releaseWorker);
      if (canvas) { canvas.width = 0; canvas.height = 0; }
    };
  });

  $effect(() => {
    const pdf = documentProxy;
    const target = canvas;
    const availableWidth = width;
    const availableHeight = height;
    const number = pageNumber;
    const magnification = zoom;
    const mode = sizing;
    if (!pdf || !target || !availableWidth || !availableHeight) return;
    const version = ++generation;
    currentRender?.resolve(false);
    let finish!: (success: boolean) => void;
    const outcome = { page: number, done: new Promise<boolean>(resolve => { finish = resolve; }), resolve: (success: boolean) => finish(success) };
    currentRender = outcome;
    // A viewport refit replaces PDF.js nodes, not the proved source/page text.
    renderedPage = 0; selectedText = '';
    error = null;
    const previous = renderTask;
    previous?.cancel();
    textLayer?.cancel();
    void (async () => {
      try {
        // PDF.js cannot render twice into the same canvas concurrently.
        await previous?.promise.catch(() => {});
        const page = await pdf.getPage(number);
        if (version !== generation) return;
        // PDF points are 1/72 inch; CSS pixels are 1/96 inch. Device pixels
        // affect raster sharpness only, never the displayed zoom percentage.
        const actualSize = page.getViewport({ scale: 96 / 72 });
        const fitWidth = Math.max(0.01, (availableWidth - 16) / actualSize.width);
        const fitPage = Math.max(0.01, Math.min(fitWidth, (availableHeight - 16) / actualSize.height));
        const scale = mode === 'manual' ? magnification
          : mode === 'width' ? fitWidth
          : mode === 'page' ? fitPage : Math.min(1, fitPage);
        const view = page.getViewport({ scale: scale * 96 / 72 });
        const unrotated = page.getViewport({ scale: 1, rotation: 0 });
        pageWidth = unrotated.width; pageHeight = unrotated.height;
        rotation = view.rotation as 0 | 90 | 180 | 270;
        displayWidth = view.width; displayHeight = view.height;
        effectiveZoom = scale;
        const pixelRatio = Math.min(window.devicePixelRatio || 1, 2,
          Math.sqrt(4_000_000 / (view.width * view.height)),
          4096 / Math.max(view.width, view.height));
        target.width = Math.max(1, Math.floor(view.width * pixelRatio));
        target.height = Math.max(1, Math.floor(view.height * pixelRatio));
        target.style.width = `${view.width}px`;
        target.style.height = `${view.height}px`;
        renderTask = page.render({
          canvas: target,
          viewport: view,
          transform: [pixelRatio, 0, 0, pixelRatio, 0, 0],
        });
        await renderTask.promise;
        if (version !== generation) return;
        if (textContainer) {
          const { TextLayer } = await import('pdfjs-dist/legacy/build/pdf.mjs');
          if (version !== generation) return;
          preparePdfTextLayer(textContainer, view.scale, page.userUnit);
          textLayer = new TextLayer({ textContentSource: page.streamTextContent(), container: textContainer, viewport: view });
          await textLayer.render();
        }
        page.cleanup();
        if (version === generation) {
          renderedPage = number;
          error = null;
          viewport?.scrollTo?.({ top: 0, left: 0 });
          paintSearch();
        }
      } catch (cause) {
        if (version === generation && !(cause instanceof Error && cause.name === 'RenderingCancelledException')) {
          error = 'This PDF page could not be rendered.';
        }
      } finally { outcome.resolve(version === generation && renderedPage === number); }
    })();
    return () => { generation += 1; outcome.resolve(false); renderTask?.cancel(); textLayer?.cancel(); };
  });

  function setZoom(value: number) {
    if (!Number.isFinite(value)) return;
    zoom = Math.max(0.1, Math.min(4, Math.round(value * 100) / 100));
    sizing = 'manual';
  }

  function enterZoom(input: HTMLInputElement) {
    setZoom(Number(input.value) / 100);
    input.value = String(Math.round(selectedZoom * 100));
  }
</script>

<DocumentFind adapter={finder} identity={url} fill>
<div class="flex h-full min-h-0 min-w-0 flex-col">
  <div class="flex shrink-0 flex-wrap items-center justify-center gap-2 border-b border-separator bg-chrome p-1 text-sm text-foreground">
    <FeedbackIndicator />
    <button class="min-h-11 px-3" disabled={!documentProxy || pageNumber <= 1} onclick={() => { pageNumber -= 1; }}>Previous</button>
    <span aria-live="polite">Page {pageNumber} / {documentProxy?.numPages ?? '…'}</span>
    <button class="min-h-11 px-3" disabled={!documentProxy || pageNumber >= documentProxy.numPages} onclick={() => { pageNumber += 1; }}>Next</button>
    <button class="min-h-11 px-3" aria-label="Zoom out" disabled={!documentProxy || selectedZoom <= 0.1} onclick={() => setZoom(selectedZoom - 0.25)}>−</button>
    <label class="flex items-center gap-1">
      <input aria-label="Zoom percentage" type="number" class="w-16 rounded border border-separator bg-raised px-1 py-2 text-center"
        value={Math.round(selectedZoom * 100)} disabled={!documentProxy}
        onchange={(event) => enterZoom(event.currentTarget)} />
      <span>%</span>
    </label>
    <button class="min-h-11 px-3" aria-label="Zoom in" disabled={!documentProxy || selectedZoom >= 4} onclick={() => setZoom(selectedZoom + 0.25)}>+</button>
    <button class="min-h-11 px-2" aria-label="Actual size (100%)" disabled={!documentProxy} onclick={() => setZoom(1)}>100%</button>
    <button class="min-h-11 px-2" aria-pressed={sizing === 'page'} disabled={!documentProxy} onclick={() => { sizing = 'page'; }}>Fit page</button>
    <button class="min-h-11 px-2" aria-pressed={sizing === 'width'} disabled={!documentProxy} onclick={() => { sizing = 'width'; }}>Fit width</button>
  </div>
  {#if binding && binding.mode === 'comment' && binding.source.capabilities.comment}
    {#if textError}<p role="alert" class="p-2 text-danger">{textError}</p>{/if}
    {#if textError}<details class="shrink-0"><summary class="min-h-11 p-2 text-sm">Select canonical page text instead</summary>
      <ScopedTextSelection bind:this={scoped} {binding} scope={{ kind: 'pdf_page', page: pageNumber }} locator={range => ({ kind: 'pdf', page: pageNumber, page_width_pt: pageWidth, page_height_pt: pageHeight, rotation, shapes: [], range })} />
    </details>{/if}
  {/if}
  <div bind:this={viewport} class="pdf-viewport min-h-0 min-w-0 flex-1 overflow-auto overscroll-contain bg-raised text-foreground">
    {#if loading}<p role="status" class="p-4">Loading PDF…</p>{/if}
    {#if error}<p role="alert" class="p-4 text-danger">{error}</p>{/if}
    <div bind:this={pageBox} class="relative mx-auto my-2" style:width={`${displayWidth}px`} style:height={`${displayHeight}px`}>
      <canvas bind:this={canvas} class="block max-w-none bg-white" aria-label={`PDF page ${pageNumber}`} aria-busy={renderedPage !== pageNumber}></canvas>
        <div bind:this={textContainer} use:nativeSelection={{ binding, instance, map: mapText, error: message => { textError = message; } }} class="textLayer absolute inset-0"></div>
       {#if binding && renderedPage === pageNumber && binding.supported_locator_kinds.includes('pdf')}
          {#key `${url}:${pageNumber}:${rotation}`}<VisualAnnotations bind:this={markup} adapterId={instance} {binding} {highlights}
            displayShapes={locator => locator.kind === 'pdf' ? mapShapes(locator.shapes, point => pdfDisplayPoint(point, { width: pageWidth, height: pageHeight }, rotation)) : []}
            makeLocator={shapes => ({ kind: 'pdf', page: pageNumber,
           page_width_pt: pageWidth, page_height_pt: pageHeight, rotation,
           shapes: mapShapes(shapes, point => pdfPagePoint(point, { width: pageWidth, height: pageHeight }, rotation)), range: null })} />{/key}
      {/if}
    </div>
  </div>
</div>
</DocumentFind>

<style>
  .pdf-viewport { scrollbar-gutter: stable; }
</style>

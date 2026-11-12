import { htmlBridgeScript, readObservation, type HtmlObservation } from '$lib/feedback/annotations/html-bridge';
import type { FeedbackLocator, FeedbackVisibleArea } from '$lib/feedback/types';
import type { DocumentFindAdapter } from '$lib/document-find';
import { documentFindBridgeScript } from './document-find-bridge';

export const INLINE_HTML_POLICY = "default-src 'none'; img-src data: https:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; base-uri 'none'; form-action 'none'";
export const SIGNED_HTML_POLICY = "default-src 'none'; connect-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:; media-src data: blob:";
// Match the artifact store's default maximum; never buffer an unbounded body.
export const HTML_PREVIEW_MAX_BYTES = 50 * 1024 * 1024;

/** A trusted view lifetime, not an authenticated account or credential container. */
export interface HtmlPreviewGuard {
  readonly signal: AbortSignal;
  assertCurrent(): void;
  validate(): Promise<void> | void;
}
export type InlineHtmlPreview = { text: string; guard: HtmlPreviewGuard };
export type SignedHtmlPreview = { url: string; lease: HtmlPreviewGuard; read?: (signal: AbortSignal) => Promise<string> };
export type HtmlPreviewSource = InlineHtmlPreview | { text: string; lease: HtmlPreviewGuard } | SignedHtmlPreview;
export function htmlPreviewGuard(source: HtmlPreviewSource): HtmlPreviewGuard {
  return 'guard' in source ? source.guard : source.lease;
}

/** Teardown removes the browsing context before revocation; revocation alone cannot stop scripts. */
export function mountHtmlPreview(
  container: HTMLElement, source: HtmlPreviewSource, lease: HtmlPreviewGuard,
  report: (message: string | null) => void,
  annotation?: { enabled: () => boolean; tool?: () => string; observe: (observation: HtmlObservation) => void;
    viewport?: (observation: HtmlObservation['viewport']) => void;
    ready?: (locate: ((anchor: Extract<FeedbackLocator, { kind: 'html' }>, visible?: FeedbackVisibleArea) => Promise<boolean>) | null,
      syncMode?: () => void) => void },
  find?: { ready(adapter: DocumentFindAdapter | null): void; open(): void },
): () => void {
  const controller = new AbortController();
  const sourceGuard = htmlPreviewGuard(source);
  const guards = sourceGuard === lease ? [lease] : [sourceGuard, lease];
  async function validate() {
    for (const guard of guards) {
      guard.signal.throwIfAborted(); guard.assertCurrent();
      await guard.validate();
      guard.signal.throwIfAborted(); guard.assertCurrent();
    }
  }
  let frame: HTMLIFrameElement | undefined;
  let objectUrl: string | undefined;
  let stopped = false;
  const instance = crypto.randomUUID();
  let received = 0, windowStart = 0;
  let requestNumber = 0;
  const pending = new Map<number, { resolve: (located: boolean) => void; timer: ReturnType<typeof setTimeout> }>();
  const searches = new Map<number, { resolve: (count: number) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  let findQuery = '';
  function search(query: string, index = -1): Promise<number> {
    if (stopped || guards.some(guard => guard.signal.aborted) || query.length > 4096 || searches.size >= 8) return Promise.reject(new Error('HTML search unavailable'));
    guards.forEach(guard => guard.assertCurrent());
    return new Promise((resolve, reject) => {
      const request = ++requestNumber;
      const timer = setTimeout(() => { searches.delete(request); reject(new Error('HTML search timed out')); }, 1500);
      searches.set(request, { resolve, reject, timer });
      frame?.contentWindow?.postMessage({ type: 'cognis-find', instance, request, query, index }, '*');
    });
  }
  const receive = (event: MessageEvent) => {
    if (stopped || guards.some(guard => guard.signal.aborted) || event.source !== frame?.contentWindow) return;
    const now = performance.now();
    if (now - windowStart > 1000) { received = 0; windowStart = now; }
    if (++received > 20) return;
    if (event.data?.instance === instance && event.data.type === 'cognis-find-open' && Object.keys(event.data).length === 2) {
      find?.open(); return;
    }
    if (event.data?.instance === instance && event.data.type === 'cognis-find-result'
      && Number.isSafeInteger(event.data.request) && Number.isSafeInteger(event.data.count)
      && event.data.count >= -1 && event.data.count <= HTML_PREVIEW_MAX_BYTES && Object.keys(event.data).length === 4) {
      const item = searches.get(event.data.request);
      if (item) {
        clearTimeout(item.timer); searches.delete(event.data.request);
        if (event.data.count < 0) item.reject(new Error('HTML search failed')); else item.resolve(event.data.count);
      }
      return;
    }
    if (event.data && typeof event.data === 'object' && event.data.type === 'cognis-located' && event.data.instance === instance
      && typeof event.data.request === 'number' && typeof event.data.located === 'boolean'
      && Object.keys(event.data).length === 4) {
      const item = pending.get(event.data.request);
      if (item) { clearTimeout(item.timer); pending.delete(event.data.request); item.resolve(event.data.located); }
      return;
    }
    const value = readObservation(event.data, instance);
    if (value) annotation?.viewport?.(value.viewport);
    if (value && annotation?.enabled()) annotation.observe(value);
  };
  const syncMode = () => {
    if (!stopped && !guards.some(guard => guard.signal.aborted) && annotation) {
      frame?.contentWindow?.postMessage({ type: 'cognis-mode', instance, enabled: annotation.enabled(), tool: annotation.tool?.() ?? 'text' }, '*');
    }
  };
  const modeTimer = annotation ? setInterval(syncMode, 250) : undefined;
  if (annotation || find) window.addEventListener('message', receive);
  const timeout = setTimeout(() => {
    stop();
    report('The HTML preview timed out. Reopen it to try again.');
  }, 30_000);
  const stop = () => {
    stopped = true;
    clearTimeout(timeout);
    controller.abort();
    clearInterval(modeTimer);
    window.removeEventListener('message', receive);
    annotation?.ready?.(null);
    find?.ready(null);
    searches.forEach(item => { clearTimeout(item.timer); item.reject(new Error('HTML preview closed')); });
    searches.clear();
    pending.forEach(item => { clearTimeout(item.timer); item.resolve(false); });
    pending.clear();
    frame?.remove();
    frame = undefined;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = undefined;
    guards.forEach(guard => guard.signal.removeEventListener('abort', invalidated));
  };
  const invalidated = () => { stop(); report('This preview is no longer available. Reopen the source to try again.'); };
  guards.forEach(guard => guard.signal.addEventListener('abort', invalidated, { once: true }));
  report(null);
  void (async () => {
    try {
      await validate();
      if (stopped) return;
      if (!('text' in source) && !source.read) throw new Error('A private HTML reader is required.');
      const text = 'text' in source ? source.text : await source.read!(controller.signal);
      await validate();
      if (stopped) return;
      if (new TextEncoder().encode(text).byteLength > HTML_PREVIEW_MAX_BYTES) throw new Error('The HTML preview exceeds the 50 MB limit.');
      const policy = 'text' in source ? INLINE_HTML_POLICY : SIGNED_HTML_POLICY;
      objectUrl = URL.createObjectURL(new Blob([
        `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}">`,
        annotation ? htmlBridgeScript(instance) : '', find ? documentFindBridgeScript(instance) : '', text,
      ], { type: 'text/html;charset=utf-8' }));
      frame = document.createElement('iframe');
      frame.title = 'Artifact preview';
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.className = 'h-full min-h-96 w-full border-0 bg-white';
      frame.src = objectUrl;
      frame.onload = () => { if (!stopped) {
        clearTimeout(timeout); report('');
        find?.ready({
          search: query => { findQuery = query; return search(query); },
          select: async index => { await search(findQuery, index); },
          clear: () => { findQuery = ''; if (!stopped) void search('').catch(() => {}); },
        });
        annotation?.ready?.((anchor, visible) => new Promise(resolve => {
          if (stopped || !anchor.selector_path || anchor.capture_id || pending.size >= 8) { resolve(false); return; }
          const request = ++requestNumber;
          const timer = setTimeout(() => { pending.delete(request); resolve(false); }, 1500);
          pending.set(request, { resolve, timer });
          frame?.contentWindow?.postMessage({ type: 'cognis-locate', instance, request, selector: anchor.selector_path, element_key: anchor.element_key, quote: anchor.range?.exact ?? '', start: anchor.range?.start, end: anchor.range?.end, visible }, '*');
        }), syncMode);
        // The injected bridge has installed its listener by the load event.
        // Publish immediately; the heartbeat remains the revocation backstop.
        syncMode();
      } };
      frame.onerror = () => { stop(); report('The HTML preview could not be displayed.'); };
      container.replaceChildren(frame);
    } catch {
      if (stopped) return;
      stop();
      report('The HTML preview could not be loaded safely. Reopen it to try again.');
    }
  })();
  return stop;
}

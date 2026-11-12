import type { FeedbackViewport } from '$lib/feedback/types';
import { listenNativeSelection } from './native-selection';
import { exposeGeometry } from './expose-target';

type Rect = { left: number; top: number; width: number; height: number };
export interface HtmlObservation {
  selector: string; element_key: string | null; quote: string; viewport: FeedbackViewport;
  selection_kind: 'text' | 'element' | 'none'; start: number; end: number; rects: Rect[];
  scopes: { label: string; selector: string; rects: Rect[] }[];
}
function rects(value: unknown): value is Rect[] {
  return Array.isArray(value) && value.length <= 64 && value.every(rect => rect && typeof rect === 'object'
    && Object.keys(rect).sort().join(',') === 'height,left,top,width'
    && Object.values(rect).every(number => typeof number === 'number' && Number.isFinite(number) && Math.abs(number) <= 10_000_000)
    && rect.width >= 0 && rect.height >= 0);
}
/** Window/instance binding is necessary, not proof that artifact scripts are honest. */
export function readObservation(data: unknown, instance: string): HtmlObservation | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const value = data as Record<string, unknown>;
  if (Object.keys(value).sort().join(',') !== 'element_key,end,instance,quote,rects,scopes,selection_kind,selector,start,type,viewport'
    || value.type !== 'cognis-selection' || value.instance !== instance
    || !['text', 'element', 'none'].includes(String(value.selection_kind))
    || typeof value.selector !== 'string' || value.selector.length > 2048
    || typeof value.quote !== 'string' || new TextEncoder().encode(value.quote).length > 2048
    || !Number.isSafeInteger(value.start) || !Number.isSafeInteger(value.end) || Number(value.start) < 0 || Number(value.end) < Number(value.start)
    || Number(value.end) > 10_000_000 || value.selection_kind === 'text' && (!value.quote || Number(value.end) - Number(value.start) !== value.quote.length)
    || !rects(value.rects) || !Array.isArray(value.scopes) || value.scopes.length > 3
    || !value.scopes.every(scope => scope && typeof scope === 'object' && Object.keys(scope).sort().join(',') === 'label,rects,selector'
      && ['Cell', 'Row', 'Table'].includes(scope.label) && typeof scope.selector === 'string' && scope.selector.length <= 2048 && rects(scope.rects))
    || !(value.element_key === null || typeof value.element_key === 'string' && value.element_key.length <= 256)) return null;
  const viewport = value.viewport as FeedbackViewport | null;
  if (!viewport || Object.keys(viewport).sort().join(',') !== 'device_pixel_ratio,height_css_px,scroll_x_css_px,scroll_y_css_px,width_css_px'
    || !Object.values(viewport).every(n => typeof n === 'number' && Number.isFinite(n))
    || viewport.width_css_px <= 0 || viewport.height_css_px <= 0 || viewport.width_css_px > 32768 || viewport.height_css_px > 32768
    || viewport.scroll_x_css_px < 0 || viewport.scroll_y_css_px < 0 || viewport.scroll_x_css_px > 10_000_000 || viewport.scroll_y_css_px > 10_000_000
    || viewport.device_pixel_ratio <= 0 || viewport.device_pixel_ratio > 16) return null;
  return value as unknown as HtmlObservation;
}

/** Selection-only code in an opaque origin; observations confer no application authority. */
function bridge(instance: string, listen: typeof listenNativeSelection, expose: typeof exposeGeometry) {
  let tool = '', selected: Element | null = null, selectedRange: Range | null = null;
  let highlighted: HTMLElement | null = null, previousOutline = '';
  function clearHighlight() { if (highlighted) highlighted.style.outline = previousOutline; highlighted = null; }
  const viewport = () => ({ width_css_px: innerWidth, height_css_px: innerHeight, scroll_x_css_px: Math.max(0, scrollX), scroll_y_css_px: Math.max(0, scrollY), device_pixel_ratio: devicePixelRatio });
  const boxes = (values: Iterable<DOMRect>) => [...values].slice(0, 64).map(({ left, top, width, height }) => ({ left, top, width, height }));
  function path(element: Element): string {
    const parts: string[] = [];
    let target: Element | null = element;
    for (let depth = 0; target && depth < 16; depth++, target = target.parentElement) {
      const tag = target.localName;
      if (!/^[a-z][a-z0-9-]*$/.test(tag)) return '';
      const peers = target.parentElement ? [...target.parentElement.children].filter(node => node.localName === tag) : [target];
      parts.unshift(`${tag}:nth-of-type(${peers.indexOf(target) + 1})`);
    }
    return target || parts.join(' > ').length > 2048 ? '' : parts.join(' > ');
  }
  function publish() {
    let element = selected, range = selectedRange;
    if (!tool || !element?.isConnected || range && !range.startContainer.isConnected) { element = null; range = null; }
    const quote = range?.toString() ?? '';
    if (new TextEncoder().encode(quote).length > 2048) return;
    let start = 0;
    if (element && range) { const before = document.createRange(); before.selectNodeContents(element); before.setEnd(range.startContainer, range.startOffset); start = before.toString().length; }
    const scopes = !range && element ? [[element.closest('td,th'), 'Cell'], [element.closest('tr'), 'Row'], [element.closest('table'), 'Table']]
      .filter((value): value is [Element, string] => value[0] instanceof Element)
      .map(([node, label]) => ({ label, selector: path(node), rects: boxes([node.getBoundingClientRect()]) })) : [];
    parent.postMessage({ type: 'cognis-selection', instance, selector: element ? path(element) : '', element_key: element?.getAttribute('data-feedback-key')?.slice(0, 256) ?? null,
      quote, selection_kind: element ? range ? 'text' : 'element' : 'none', start, end: start + quote.length,
      rects: range ? boxes(range.getClientRects()) : element ? boxes([element.getBoundingClientRect()]) : [], scopes, viewport: viewport() }, '*');
  }
  function selectText() {
    if (tool !== 'text') return;
    const selection = document.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount !== 1) { selected = null; selectedRange = null; publish(); return; }
    const range = selection.getRangeAt(0).cloneRange();
    const node = range.commonAncestorContainer;
    selected = node instanceof Element ? node : node.parentElement; selectedRange = range;
    publish();
  }
  window.addEventListener('message', event => {
    if (event.source !== parent || event.data?.instance !== instance) return;
    if (event.data.type === 'cognis-mode') {
      const next = event.data.enabled === true ? event.data.tool === 'element' ? 'element' : 'text' : '';
      if (next !== tool) { clearHighlight(); tool = next; selected = null; selectedRange = null; publish(); }
    }
    if (event.data.type === 'cognis-locate' && typeof event.data.selector === 'string' && event.data.selector.length <= 2048
      && typeof event.data.request === 'number' && typeof event.data.quote === 'string' && event.data.quote.length <= 2048) {
      let located = false;
      try {
        const matches = document.querySelectorAll(event.data.selector), node = matches.length === 1 ? matches[0] : null;
        if (node && (!event.data.quote || node.textContent?.includes(event.data.quote))
          && (event.data.element_key === null || node.getAttribute('data-feedback-key') === event.data.element_key)) {
          clearHighlight(); node.scrollIntoView({ block: 'center' });
          if (event.data.quote) {
            const text = node.textContent ?? '', exact = event.data.quote;
            let start = Number(event.data.start), end = Number(event.data.end);
            if (text.slice(start, end) !== exact) {
              start = text.indexOf(exact); end = start + exact.length;
              if (start < 0 || text.indexOf(exact, start + 1) >= 0) throw new Error('Ambiguous observation');
            }
            const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT), range = document.createRange();
            let offset = 0, begun = false;
            for (let current = walker.nextNode(); current; current = walker.nextNode()) {
              const length = current.textContent?.length ?? 0;
              if (!begun && start <= offset + length) { range.setStart(current, start - offset); begun = true; }
              if (begun && end <= offset + length) { range.setEnd(current, end - offset); break; }
              offset += length;
            }
            if (range.toString() !== exact) throw new Error('Observation changed');
            const selection = document.getSelection(); selection?.removeAllRanges(); selection?.addRange(range); selectedRange = range;
          } else { highlighted = node as HTMLElement; previousOutline = highlighted.style.outline; highlighted.style.outline = '3px solid #eab308'; }
          selected = node; if (!event.data.quote) selectedRange = null;
          const visible = event.data.visible;
          if (visible && Object.keys(visible).sort().join(',') === 'height,left,top,width'
            && Object.values(visible).every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0)
            && visible.width <= innerWidth && visible.height <= innerHeight) {
            expose(node as HTMLElement, () => ({ space: 'client', rects: boxes(selectedRange ? selectedRange.getClientRects() : [node.getBoundingClientRect()]) }), visible);
          }
          publish(); located = true;
        }
      } catch { /* Observations are best effort, never interpreted as markup or commands. */ }
      parent.postMessage({ type: 'cognis-located', instance, request: event.data.request, located }, '*');
    }
  });
  listen(document, document, selectText);
  document.addEventListener('click', event => {
    if (!(event.target instanceof Element)) return;
    const automaticElement = tool === 'text' && !event.target.closest('a,button,input,select,textarea,[contenteditable]')
      && (!event.target.textContent?.trim() || ['IMG', 'SVG', 'CANVAS', 'VIDEO'].includes(event.target.tagName))
      && !document.getSelection()?.toString().trim();
    if (tool !== 'element' && !automaticElement) {
      // Opaque Blob documents cannot navigate their own Blob URL with a fragment.
      const href = event.target.closest('a')?.getAttribute('href');
      if (href?.startsWith('#') && href.length > 1) {
        try {
          const destination = document.getElementById(decodeURIComponent(href.slice(1)));
          if (destination) { event.preventDefault(); destination.scrollIntoView({ block: 'start' }); }
        } catch { /* Malformed fragments retain the browser's normal handling. */ }
      }
      return;
    }
    event.preventDefault(); event.stopImmediatePropagation();
    selected = event.target.closest('td,th') ?? event.target; selectedRange = null; publish();
  }, true);
  window.addEventListener('scroll', publish, { passive: true });
  window.addEventListener('resize', publish);
}
export function htmlBridgeScript(instance: string): string {
  return `<script>(${bridge.toString()})(${JSON.stringify(instance).replaceAll('<', '\\u003c')}, ${listenNativeSelection.toString()}, ${exposeGeometry.toString()})</script>`;
}

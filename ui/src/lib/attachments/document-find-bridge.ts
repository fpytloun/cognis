import { createDocumentFind, documentMatches } from '$lib/document-find';

/** Opaque document observations can only update the local find UI, never application authority. */
function bridge(instance: string, makeFind: typeof createDocumentFind, matchText: typeof documentMatches) {
  let finder: ReturnType<typeof makeFind> | null = null;
  window.addEventListener('message', event => {
    const data = event.data;
    if (event.source !== parent || data?.instance !== instance || data.type !== 'cognis-find'
      || !Number.isSafeInteger(data.request) || typeof data.query !== 'string' || data.query.length > 4096
      || !Number.isSafeInteger(data.index) || data.index < -1) return;
    let count = -1;
    try {
      finder ??= makeFind(document.body, matchText);
      count = finder.search(data.query) as number;
      if (data.index >= 0 && data.index < count) finder.select(data.index);
    } catch { /* Report failure without trusting or exposing artifact content. */ }
    parent.postMessage({ type: 'cognis-find-result', instance, request: data.request, count }, '*');
  });
  window.addEventListener('keydown', event => {
    if (!event.altKey && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      parent.postMessage({ type: 'cognis-find-open', instance }, '*');
    }
  });
}

export function documentFindBridgeScript(instance: string): string {
  return `<script>(${bridge.toString()})(${JSON.stringify(instance)}, ${createDocumentFind.toString()}, ${documentMatches.toString()})</script>`;
}

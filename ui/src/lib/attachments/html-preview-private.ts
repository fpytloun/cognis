import { apiUrl } from '$lib/config';
import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
import { api } from '$lib/api/client';
import { HTML_PREVIEW_MAX_BYTES, type SignedHtmlPreview } from './html-preview';
export type { SignedHtmlPreview } from './html-preview';

/** Private-only authorization. Never import this module in a standalone entry graph. */
export async function resolveHtmlPreviewUrl(id: string, signal?: AbortSignal): Promise<SignedHtmlPreview> {
  const lease = documentCoordination().capture();
  await lease.validate();
  const combined = signal ? AbortSignal.any([signal, lease.signal]) : lease.signal;
  combined.throwIfAborted();
  const result = await api.artifacts.signedUrl(id, 3600, 'view', combined);
  await lease.validate(); combined.throwIfAborted();
  let url = signedHtmlUrl(result.url);
  if (new URL(url).pathname.startsWith('/api/v1/artifacts/virtual/deliverables/view/')) {
    const exported = await api.artifacts.signedUrl(id, 3600, 'download', combined);
    await lease.validate(); combined.throwIfAborted();
    const exportUrl = signedHtmlUrl(exported.url);
    const expected = new URL(url).pathname.replace('/virtual/deliverables/view/', '/virtual/deliverables/');
    if (new URL(exportUrl).pathname !== expected) throw new Error('The HTML export URL is not valid.');
    url = exportUrl;
  }
  return privateHtmlPreviewSource(url, lease, combined);
}
/** Adapts an already authorized private URL; never used by public source consumers. */
export function privateHtmlPreviewSource(value: string, lease: DocumentLease, signal?: AbortSignal): SignedHtmlPreview {
  const url = signedHtmlUrl(value);
  const guard = !signal || signal === lease.signal ? lease : {
    signal: AbortSignal.any([lease.signal, signal]),
    assertCurrent() { this.signal.throwIfAborted(); lease.assertCurrent(); },
    async validate() { await lease.validate(); this.signal.throwIfAborted(); lease.assertCurrent(); },
  };
  return { url, lease: guard, read: async readSignal => {
    const current = AbortSignal.any([readSignal, guard.signal]);
    await guard.validate(); current.throwIfAborted();
    const text = await readHtml(url, current);
    await guard.validate(); current.throwIfAborted();
    return text;
  } };
}
export function signedHtmlUrl(value: string): string {
  const base = new URL(apiUrl('/') || '/', window.location.origin);
  const url = new URL(value, base);
  if (!['https:', 'http:'].includes(url.protocol) || url.origin !== base.origin
    || url.username || url.password || url.hash
    || !/^\/api\/v1\/artifacts\/(?:view\/[^/]+\/[^/]+\/.+|virtual\/deliverables\/(?:view\/)?dlv_[a-zA-Z0-9]+\/.+)$/.test(url.pathname)
    || [...url.searchParams.keys()].sort().join(',') !== 'exp,sig'
    || !/^\d+$/.test(url.searchParams.get('exp') ?? '')
    || !/^[a-f0-9]{64}$/.test(url.searchParams.get('sig') ?? '')) throw new Error('The HTML preview URL is not valid.');
  return url.href;
}
async function readHtml(url: string, signal: AbortSignal): Promise<string> {
  const sourceUrl = new URL(signedHtmlUrl(url));
  if (sourceUrl.pathname.startsWith('/api/v1/artifacts/view/')) sourceUrl.searchParams.set('raw', '1');
  const response = await fetch(sourceUrl.href, { signal, credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', cache: 'no-store' });
  if (!response.ok) throw new Error('The HTML preview could not be loaded. Reopen it to try again.');
  const type = response.headers.get('content-type') ?? '';
  if (type.split(';')[0].trim().toLowerCase() !== 'text/html') throw new Error('The preview response is not HTML.');
  if (Number(response.headers.get('content-length')) > HTML_PREVIEW_MAX_BYTES) throw new Error('The HTML preview exceeds the 50 MB limit.');
  if (!response.body) throw new Error('The HTML preview is empty.');
  const reader = response.body.getReader(), chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > HTML_PREVIEW_MAX_BYTES) throw new Error('The HTML preview exceeds the 50 MB limit.');
      chunks.push(value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const charset = /charset\s*=\s*["']?([^;"'\s]+)/i.exec(type)?.[1] ?? 'utf-8';
    return new TextDecoder(charset).decode(bytes);
  } finally {
    await reader.cancel().catch(() => {}); reader.releaseLock();
  }
}

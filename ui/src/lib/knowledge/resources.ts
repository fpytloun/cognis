import type { KnowledgebaseDocumentModel } from '$lib/types/api';

export type KnowledgeResourceResolution =
  | { kind: 'external'; href: string }
  | { kind: 'document'; docId: string; href: string; fragment?: string }
  | { kind: 'resource'; href: string; path: string }
  | { kind: 'unavailable'; reason: string };

function splitReference(href: string): { pathname: string; fragment?: string } | null {
  const hashIndex = href.indexOf('#');
  const withoutHash = hashIndex >= 0 ? href.slice(0, hashIndex) : href;
  const queryIndex = withoutHash.indexOf('?');
  const pathname = queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash;
  if (!pathname) return null;
  if (hashIndex < 0) return { pathname };
  try {
    return { pathname, fragment: decodeURIComponent(href.slice(hashIndex + 1)) || undefined };
  } catch {
    return null;
  }
}

function normalizePath(path: string): string | null {
  const parts: string[] = [];
  for (const raw of path.split('/')) {
    let part: string;
    try { part = decodeURIComponent(raw); } catch { return null; }
    if (!part || part === '.') continue;
    if (part === '..') {
      if (parts.length === 0) return null;
      parts.pop();
    } else parts.push(part);
  }
  return parts.join('/');
}

export function resolveKnowledgeResource(
  href: string,
  kbId: string,
  source: KnowledgebaseDocumentModel,
  documents: KnowledgebaseDocumentModel[]
): KnowledgeResourceResolution {
  if (/^(https?:|mailto:|tel:|#|\?)/i.test(href)) return { kind: 'external', href };
  const reference = splitReference(href);
  if (!reference) return { kind: 'unavailable', reason: 'Invalid resource reference' };
  const alias = '/knowledge/resources/';
  const sourceDir = (source.source_path ?? '').split('/').slice(0, -1).join('/');
  const candidate = reference.pathname.startsWith(alias) ? reference.pathname.slice(alias.length)
    : reference.pathname.startsWith('/') ? null : `${sourceDir}/${reference.pathname}`;
  if (candidate === null) return { kind: 'unavailable', reason: 'Unsupported application path' };
  const normalized = normalizePath(candidate);
  if (!normalized) return { kind: 'unavailable', reason: 'Resource path escapes the knowledgebase root' };
  const attached = documents.find((doc) => doc.source_path === normalized);
  if (attached) {
    return {
      kind: 'document',
      docId: attached.doc_id,
      href: `/knowledge/${encodeURIComponent(kbId)}?tab=browse&document=${encodeURIComponent(attached.doc_id)}${reference.fragment ? `#${encodeURIComponent(reference.fragment)}` : ''}`,
      fragment: reference.fragment
    };
  }
  return {
    kind: 'resource',
    path: normalized,
    href: `/api/v1/knowledgebases/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(source.doc_id)}/resources/knowledge/resources/${normalized.split('/').map(encodeURIComponent).join('/')}`
  };
}

export function rewriteKnowledgeResourceHtml(
  html: string,
  kbId: string,
  source: KnowledgebaseDocumentModel,
  documents: KnowledgebaseDocumentModel[]
): string {
  const rewritten = html.replace(/\b(href|src)="([^"]+)"/g, (full, attr: string, encoded: string) => {
    const value = encoded.replaceAll('&amp;', '&');
    const resolution = resolveKnowledgeResource(value, kbId, source, documents);
    if (resolution.kind === 'external') return full;
    if (resolution.kind === 'unavailable') {
      return `${attr}="#" data-kb-resource-unavailable="${resolution.reason}"`;
    }
    if (resolution.kind === 'document') {
      const fragment = resolution.fragment
        ? ` data-kb-document-fragment="${encodeURIComponent(resolution.fragment)}"`
        : '';
      return `${attr}="${resolution.href}" data-kb-document-id="${resolution.docId}"${fragment}`;
    }
    return `${attr}="${resolution.href}"`;
  });
  return rewritten.replace(
    /<img\b[^>]*data-kb-resource-unavailable="[^"]*"[^>]*>/g,
    '<span class="kb-resource-unavailable" role="status">Resource unavailable</span>'
  );
}

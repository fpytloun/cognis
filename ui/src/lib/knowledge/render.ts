/** Classifies a document for the read-only Browse viewer. Generic — no source-specific parsing. */

export type DocumentRenderKind = 'markdown' | 'json' | 'yaml' | 'xml' | 'code' | 'plain' | 'binary';

const CODE_EXTENSIONS = new Set([
  'ts', 'tsx', 'js', 'jsx', 'py', 'go', 'rs', 'java', 'kt', 'c', 'cpp', 'h', 'hpp', 'cs', 'rb', 'php', 'sh', 'bash', 'sql', 'css', 'scss', 'svelte', 'vue', 'html', 'htm'
]);

function extensionFromName(name: string | null | undefined): string {
  if (!name) return '';
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

export function classifyDocument(
  mimeType: string | null | undefined,
  displayName: string | null | undefined
): DocumentRenderKind {
  const ext = extensionFromName(displayName);
  const mime = (mimeType ?? '').toLowerCase();

  if (mime.includes('pdf') || mime.includes('word') || mime === 'application/octet-stream') {
    return 'binary';
  }
  if (ext === 'md' || ext === 'markdown' || mime.includes('markdown')) return 'markdown';
  if (ext === 'json' || mime.includes('json')) return 'json';
  if (ext === 'yaml' || ext === 'yml' || mime.includes('yaml')) return 'yaml';
  if (ext === 'xml' || mime.includes('xml')) return 'xml';
  if (CODE_EXTENSIONS.has(ext)) return 'code';
  if (mime.startsWith('text/') || ext === 'txt' || ext === 'log' || ext === 'csv' || ext === 'tsv' || ext === 'rst') {
    return 'plain';
  }
  return 'binary';
}

export function languageForHighlight(kind: DocumentRenderKind, displayName: string | null | undefined): string {
  if (kind === 'json') return 'json';
  if (kind === 'yaml') return 'yaml';
  if (kind === 'xml') return 'xml';
  if (kind === 'code') return extensionFromName(displayName) || 'plaintext';
  return 'plaintext';
}

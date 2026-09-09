import type { AttachmentRef } from '$lib/types/api';

const TEXT_EXTENSIONS = new Set([
  'c', 'conf', 'cpp', 'css', 'csv', 'go', 'h', 'hpp', 'html', 'ini', 'java',
  'js', 'json', 'jsonl', 'log', 'md', 'py', 'rb', 'rs', 'sh', 'sql', 'svelte',
  'toml', 'ts', 'tsx', 'txt', 'xml', 'yaml', 'yml',
]);

const LANGUAGE_BY_EXTENSION: Record<string, string> = {
  c: 'c', cpp: 'cpp', css: 'css', go: 'go', h: 'c', hpp: 'cpp', html: 'xml',
  java: 'java', js: 'javascript', json: 'json', jsonl: 'json', md: 'markdown',
  py: 'python', rb: 'ruby', rs: 'rust', sh: 'bash', sql: 'sql', svelte: 'xml',
  toml: 'ini', ts: 'typescript', tsx: 'typescript', xml: 'xml', yaml: 'yaml',
  yml: 'yaml',
};

function extension(filename: string): string {
  return filename.split('.').pop()?.toLowerCase() ?? '';
}

export function previewKind(attachment: AttachmentRef): 'text' | 'video' | 'html' | null {
  const mimeType = attachment.mime_type?.split(';', 1)[0]?.trim().toLowerCase() ?? '';
  if (mimeType === 'text/html') return 'html';
  if (mimeType.startsWith('video/')) return 'video';
  if (
    mimeType.startsWith('text/')
    || ['application/json', 'application/xml', 'application/yaml', 'application/toml', 'application/javascript', 'application/sql', 'application/x-ndjson', 'application/x-sh'].includes(mimeType)
    || TEXT_EXTENSIONS.has(extension(attachment.filename))
  ) return 'text';
  return null;
}

export function previewLanguage(filename: string, mimeType: string | null | undefined): string | null {
  const ext = extension(filename);
  if (LANGUAGE_BY_EXTENSION[ext]) return LANGUAGE_BY_EXTENSION[ext];
  const normalized = mimeType?.split(';', 1)[0]?.trim().toLowerCase();
  if (normalized === 'application/json') return 'json';
  if (normalized === 'application/xml') return 'xml';
  if (normalized === 'application/yaml') return 'yaml';
  if (normalized === 'application/sql') return 'sql';
  if (normalized === 'application/x-sh') return 'bash';
  return null;
}

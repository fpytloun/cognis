import DOMPurify from 'dompurify';
import hljs from 'highlight.js/lib/common';
import { marked, Renderer } from 'marked';

marked.setOptions({
  breaks: true,
  gfm: true
});

const forbiddenAttributes = ['onerror', 'onclick', 'onload', 'onmouseover'];
const forbiddenTags = ['iframe', 'script', 'style'];
const genericEnclosingFenceLanguages = new Set(['', 'md', 'markdown', 'plain', 'plaintext', 'text', 'txt']);

interface FenceLine {
  indent: string;
  char: '`' | '~';
  length: number;
  suffix: string;
}

function parseFenceOpener(line: string): FenceLine | null {
  const match = line.match(/^([ \t]{0,3})(`{3,}|~{3,})(.*)$/);
  if (!match) return null;

  const marker = match[2];
  const char = marker[0] as '`' | '~';
  const suffix = match[3] ?? '';
  if (char === '`' && suffix.includes('`')) return null;

  return {
    indent: match[1],
    char,
    length: marker.length,
    suffix,
  };
}

function parseClosingFence(line: string, char: '`' | '~', minLength: number): FenceLine | null {
  const match = line.match(/^([ \t]{0,3})(`{3,}|~{3,})[ \t]*$/);
  if (!match) return null;

  const marker = match[2];
  if (marker[0] !== char || marker.length < minLength) return null;

  return {
    indent: match[1],
    char,
    length: marker.length,
    suffix: '',
  };
}

function fenceLanguage(fence: FenceLine): string {
  return fence.suffix.trim().split(/\s+/)[0]?.toLowerCase() ?? '';
}

function leadingFenceRunLength(line: string, char: '`' | '~'): number {
  const match = line.match(/^([ \t]{0,3})(`{3,}|~{3,})/);
  if (!match || match[2][0] !== char) return 0;
  return match[2].length;
}

function normalizeEnclosingFence(markdown: string): string {
  if (!markdown.includes('```') && !markdown.includes('~~~')) return markdown;

  const lines = markdown.split('\n');
  const firstLineIndex = lines.findIndex((line) => line.trim() !== '');
  if (firstLineIndex === -1) return markdown;

  let lastLineIndex = lines.length - 1;
  while (lastLineIndex >= firstLineIndex && lines[lastLineIndex].trim() === '') {
    lastLineIndex -= 1;
  }

  const opener = parseFenceOpener(lines[firstLineIndex]);
  if (!opener) return markdown;

  if (!genericEnclosingFenceLanguages.has(fenceLanguage(opener))) return markdown;

  let maxInnerFenceLength = 0;
  let hasNestedLanguageFence = false;
  for (let i = firstLineIndex + 1; i < lastLineIndex; i += 1) {
    maxInnerFenceLength = Math.max(maxInnerFenceLength, leadingFenceRunLength(lines[i], opener.char));
    const nestedOpener = parseFenceOpener(lines[i]);
    if (
      nestedOpener
      && nestedOpener.char === opener.char
      && nestedOpener.length >= opener.length
      && fenceLanguage(nestedOpener) !== ''
    ) {
      hasNestedLanguageFence = true;
    }
  }

  if (maxInnerFenceLength < opener.length) return markdown;

  const closer = parseClosingFence(lines[lastLineIndex], opener.char, opener.length);
  if (!closer && !hasNestedLanguageFence) return markdown;

  const normalizedLength = Math.max(opener.length, closer?.length ?? 0, maxInnerFenceLength) + 1;
  const normalizedMarker = opener.char.repeat(normalizedLength);
  lines[firstLineIndex] = `${opener.indent}${normalizedMarker}${opener.suffix}`;
  if (closer) {
    lines[lastLineIndex] = `${closer.indent}${normalizedMarker}`;
  }

  return lines.join('\n');
}

function isOutgoingHref(href: string | null | undefined): boolean {
  if (!href) return false;
  const trimmed = href.trim().toLowerCase();
  return trimmed.startsWith('http://') || trimmed.startsWith('https://') || trimmed.startsWith('mailto:');
}

function markOutgoingLinks(html: string): string {
  return html.replace(/^<a /, '<a target="_blank" rel="noopener noreferrer" ');
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function languageClass(lang: string): string {
  const normalized = lang.trim().split(/\s+/)[0]?.toLowerCase() ?? '';
  return normalized.replace(/[^a-z0-9_-]/g, '');
}

function applyOutgoingLinkTargets(html: string): string {
  if (typeof document === 'undefined') return html;
  const template = document.createElement('template');
  template.innerHTML = html;
  template.content.querySelectorAll('a[href]').forEach((link) => {
    if (isOutgoingHref(link.getAttribute('href'))) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    }
  });
  return template.innerHTML;
}

function createLinkRenderer(): Renderer {
  const renderer = new Renderer();
  const baseLink = renderer.link.bind(renderer);

  renderer.code = (token) => {
    const lang = languageClass(token.lang ?? '');
    try {
      if (lang && hljs.getLanguage(lang)) {
        const highlighted = hljs.highlight(token.text, { language: lang, ignoreIllegals: true }).value;
        return `<pre><code class="hljs language-${lang}">${highlighted}</code></pre>`;
      }
    } catch {
      // Fall through to escaped plain text if highlight.js cannot parse it.
    }

    const className = lang ? ` class="language-${lang}"` : '';
    return `<pre><code${className}>${escapeHtml(token.text)}</code></pre>`;
  };

  renderer.link = (token) => {
    const html = baseLink(token);
    return isOutgoingHref(token.href) && typeof html === 'string' ? markOutgoingLinks(html) : html;
  };

  return renderer;
}

export function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    FORBID_ATTR: forbiddenAttributes,
    FORBID_TAGS: forbiddenTags,
    USE_PROFILES: { html: true }
  });
}

export function renderMarkdown(markdown: string): string {
  const parsed = marked.parse(normalizeEnclosingFence(markdown), { async: false, renderer: createLinkRenderer() });
  return applyOutgoingLinkTargets(sanitizeHtml(typeof parsed === 'string' ? parsed : ''));
}

function createDocsRenderer(): Renderer {
  const renderer = createLinkRenderer();
  const baseCode = renderer.code.bind(renderer);
  const baseTable = renderer.table.bind(renderer);

  renderer.code = (...args) => `<div class="markdown-code-wrap">${baseCode(...args)}</div>`;
  renderer.table = (...args) => `<div class="markdown-table-wrap">${baseTable(...args)}</div>`;

  return renderer;
}

export function renderDocsMarkdown(markdown: string): string {
  const parsed = marked.parse(normalizeEnclosingFence(markdown), {
    async: false,
    renderer: createDocsRenderer()
  });
  return applyOutgoingLinkTargets(sanitizeHtml(typeof parsed === 'string' ? parsed : ''));
}

/**
 * Streaming markdown renderer.
 *
 * Splits incoming content at block boundaries (blank lines and fenced code
 * blocks) and memoizes per-block HTML. Re-parsing during streaming only hits
 * the tail block. For a 2000-token assistant reply this turns an O(n^2)
 * parse+sanitize into O(n) at steady state.
 *
 * Usage:
 *   const streamer = createMarkdownStreamer();
 *   const html = streamer.render(content);
 *   // On message_complete, finalize to force-flush the tail.
 *   const finalHtml = streamer.finalize(content);
 */
export interface MarkdownStreamer {
  render(content: string): string;
  finalize(content: string): string;
  reset(): void;
}

export function createMarkdownStreamer(): MarkdownStreamer {
  // Map of block text -> rendered HTML fragment (stable blocks only).
  const cache = new Map<string, string>();
  let stableHtml = '';
  let stableLen = 0;

  function splitBlocks(text: string): { stable: string[]; tail: string } {
    // Walk chars and keep track of fenced code state so we don't split mid-fence.
    const blocks: string[] = [];
    let inFence = false;
    let fenceChar: '`' | '~' = '`';
    let fenceLength = 0;
    let fenceAllowsNestedFallback = false;
    let nestedFenceDepth = 0;

    const lines = text.split('\n');
    let charIdx = 0;
    let blockStart = 0;

    for (let li = 0; li < lines.length; li++) {
      const line = lines[li];
      if (!inFence) {
        const opener = parseFenceOpener(line);
        if (opener) {
          inFence = true;
          fenceChar = opener.char;
          fenceLength = opener.length;
          fenceAllowsNestedFallback = genericEnclosingFenceLanguages.has(fenceLanguage(opener));
          nestedFenceDepth = 0;
        }
      } else if (parseClosingFence(line, fenceChar, fenceLength)) {
        if (nestedFenceDepth > 0) {
          nestedFenceDepth -= 1;
        } else {
          inFence = false;
          fenceLength = 0;
          fenceAllowsNestedFallback = false;
        }
      } else if (fenceAllowsNestedFallback) {
        const nestedOpener = parseFenceOpener(line);
        if (nestedOpener && nestedOpener.char === fenceChar && nestedOpener.length >= fenceLength) {
          nestedFenceDepth += 1;
        }
      }
      const isBlockBreak = !inFence && line.trim() === '';
      if (isBlockBreak && blockStart < charIdx) {
        const block = text.slice(blockStart, charIdx);
        if (block.trim()) blocks.push(block);
        blockStart = charIdx + line.length + 1;
      }
      charIdx += line.length + 1;
    }

    // Anything after the last block break is the tail. If we're not in a
    // fence and the last char was a blank line, tail is empty.
    const tail = blockStart < text.length ? text.slice(blockStart) : '';

    return { stable: blocks, tail };
  }

  function parseSanitize(chunk: string): string {
    const parsed = marked.parse(normalizeEnclosingFence(chunk), { async: false, renderer: createLinkRenderer() });
    return applyOutgoingLinkTargets(sanitizeHtml(typeof parsed === 'string' ? parsed : ''));
  }

  function render(content: string): string {
    if (!content) {
      stableHtml = '';
      stableLen = 0;
      return '';
    }

    const { stable, tail } = splitBlocks(content);

    // Cache hit: re-use memoized HTML for each stable block.
    let pieces = '';
    for (const block of stable) {
      let html = cache.get(block);
      if (html === undefined) {
        html = parseSanitize(block);
        cache.set(block, html);
      }
      pieces += html;
    }
    stableHtml = pieces;
    stableLen = stable.reduce((acc, b) => acc + b.length, 0);

    const tailHtml = tail.trim() ? parseSanitize(tail) : '';
    return pieces + tailHtml;
  }

  function finalize(content: string): string {
    // Force a full parse of the tail; cache stable blocks for future calls.
    const { stable, tail } = splitBlocks(content);
    let pieces = '';
    for (const block of stable) {
      let html = cache.get(block);
      if (html === undefined) {
        html = parseSanitize(block);
        cache.set(block, html);
      }
      pieces += html;
    }
    if (tail.trim()) {
      // Don't cache trailing fragment (may still grow with follow-up edits).
      pieces += parseSanitize(tail);
    }
    stableHtml = pieces;
    stableLen = content.length;
    return pieces;
  }

  function reset(): void {
    cache.clear();
    stableHtml = '';
    stableLen = 0;
  }

  return { render, finalize, reset };
}

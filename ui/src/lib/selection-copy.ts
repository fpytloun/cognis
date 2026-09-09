import DOMPurify from 'dompurify';

const COPY_EXCLUDE_SELECTOR = [
  '[data-copy-exclude]',
  'button',
  'input',
  'select',
  'textarea',
  'option',
  'svg',
  'script',
  'style',
  'canvas',
  'audio',
  'video',
  '[role="button"]',
  '[role="menu"]',
  '[role="navigation"]',
  '[contenteditable]:not([contenteditable="false"])',
].join(',');

const EDITOR_SELECTOR = [
  'input',
  'textarea',
  'select',
  '[contenteditable]:not([contenteditable="false"])',
].join(',');

const INTERACTIVE_COPY_TARGET_SELECTOR = [
  'button',
  '[role="button"]',
  '[data-copy-exclude]',
].join(',');

const ALLOWED_TAGS = [
  'a',
  'b',
  'blockquote',
  'br',
  'code',
  'del',
  'div',
  'em',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'i',
  'li',
  'ol',
  'p',
  'pre',
  's',
  'section',
  'span',
  'strong',
  'sub',
  'sup',
  'table',
  'tbody',
  'td',
  'tfoot',
  'th',
  'thead',
  'tr',
  'u',
  'ul',
];

const BLOCK_TAGS = new Set([
  'BLOCKQUOTE',
  'DIV',
  'H1',
  'H2',
  'H3',
  'H4',
  'H5',
  'H6',
  'HR',
  'OL',
  'P',
  'PRE',
  'SECTION',
  'TABLE',
  'UL',
]);

const PRESERVED_START = '\uE000';
const PRESERVED_END = '\uE001';

export interface SelectionCopyPayload {
  html: string;
  markdown: string;
}

function closestElement(node: Node | null): Element | null {
  if (node instanceof Element) return node;
  return node?.parentElement ?? null;
}

function isEditorNode(node: Node | null): boolean {
  return closestElement(node)?.closest(EDITOR_SELECTOR) !== null;
}

function isInteractiveCopyTarget(node: Node | null): boolean {
  return closestElement(node)?.closest(INTERACTIVE_COPY_TARGET_SELECTOR) !== null;
}

function cleanUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, document.baseURI);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function prepareFragment(fragment: DocumentFragment): void {
  for (const element of Array.from(fragment.querySelectorAll('[class*="whitespace-pre"]'))) {
    if (element.tagName === 'PRE') continue;
    const pre = document.createElement('pre');
    pre.dataset.copyTextPreWrap = '';
    pre.append(...Array.from(element.childNodes));
    element.replaceWith(pre);
  }

  for (const element of Array.from(fragment.querySelectorAll(COPY_EXCLUDE_SELECTOR))) {
    element.remove();
  }

  for (const image of Array.from(fragment.querySelectorAll('img'))) {
    const alt = image.getAttribute('alt')?.trim();
    const link = image.closest('a');
    if (link && !link.textContent?.trim()) link.removeAttribute('href');
    image.replaceWith(alt ? document.createTextNode(alt) : document.createTextNode(''));
  }

  for (const link of Array.from(fragment.querySelectorAll('a'))) {
    const href = cleanUrl(link.getAttribute('href'));
    if (href) link.setAttribute('href', href);
    else link.removeAttribute('href');
  }
}

function sanitizeFragment(fragment: DocumentFragment): string {
  const wrapper = document.createElement('div');
  wrapper.append(fragment.cloneNode(true));
  return DOMPurify.sanitize(wrapper.innerHTML, {
    ALLOWED_TAGS,
    ALLOWED_ATTR: ['href', 'colspan', 'rowspan', 'scope'],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
}

function normalizeInline(value: string): string {
  return value.replace(/\s+/g, ' ');
}

function escapeMarkdown(value: string): string {
  return value
    .replace(/([\\`*_[\]<>#~])/g, '\\$1')
    .replace(/^(\s*)([-+]|\d+[.)]) (?=\S)/gm, '$1\\$2 ')
    .replace(/^(\s*)(-{3,})\s*$/gm, '$1\\$2');
}

function preservesWhitespace(node: Node): boolean {
  return node.parentElement?.closest('[data-copy-text-pre-wrap]') !== null;
}

function indentLines(value: string, prefix: string): string {
  return value
    .split('\n')
    .map((line) => (line ? `${prefix}${line}` : line))
    .join('\n');
}

function listItemMarkdown(element: Element, depth: number, ordered: boolean): string {
  const marker = ordered ? '1. ' : '- ';
  let body = '';
  const nested: string[] = [];

  for (const child of Array.from(element.childNodes)) {
    if (child instanceof Element && (child.tagName === 'UL' || child.tagName === 'OL')) {
      const value = nodeToMarkdown(child, depth + 1).trimEnd();
      if (value) nested.push(value);
    } else {
      body += nodeToMarkdown(child, depth);
    }
  }

  const line = `${'  '.repeat(depth)}${marker}${body.trim()}`;
  return nested.length > 0 ? `${line}\n${nested.join('\n')}\n` : `${line}\n`;
}

function tableMarkdown(element: Element): string {
  return Array.from(element.querySelectorAll('tr'))
    .map((row) =>
      Array.from(row.querySelectorAll(':scope > th, :scope > td'))
        .map((cell) => childrenToMarkdown(cell, 0).trim().replace(/\s*\n\s*/g, ' '))
        .join('\t'),
    )
    .filter(Boolean)
    .join('\n');
}

function childrenToMarkdown(node: Node, depth: number): string {
  return Array.from(node.childNodes)
    .map((child) => nodeToMarkdown(child, depth))
    .join('');
}

function longestBacktickRun(value: string): number {
  return Math.max(0, ...Array.from(value.matchAll(/`+/g), (match) => match[0].length));
}

function nodeToMarkdown(node: Node, depth: number): string {
  if (node.nodeType === Node.TEXT_NODE) {
    const value = node.textContent ?? '';
    return preservesWhitespace(node) ? value : escapeMarkdown(normalizeInline(value));
  }
  if (!(node instanceof Element)) return '';

  const tag = node.tagName;
  if (tag === 'BR') return '\n';
  if (tag === 'HR') return '\n---\n\n';
  if (tag === 'STRONG' || tag === 'B') return `**${childrenToMarkdown(node, depth).trim()}**`;
  if (tag === 'EM' || tag === 'I') return `_${childrenToMarkdown(node, depth).trim()}_`;
  if (tag === 'DEL' || tag === 'S') return `~~${childrenToMarkdown(node, depth).trim()}~~`;
  if (tag === 'CODE' && node.parentElement?.tagName !== 'PRE') {
    const code = node.textContent ?? '';
    const delimiter = '`'.repeat(Math.max(1, longestBacktickRun(code) + 1));
    const padding =
      code.startsWith('`')
      || code.endsWith('`')
      || (code.trim().length > 0 && code.startsWith(' ') && code.endsWith(' '))
        ? ' '
        : '';
    return `${delimiter}${padding}${code}${padding}${delimiter}`;
  }
  if (tag === 'PRE') {
    if (node.hasAttribute('data-copy-text-pre-wrap')) {
      return `\n${PRESERVED_START}${escapeMarkdown(node.textContent ?? '')}${PRESERVED_END}\n\n`;
    }
    const code = node.textContent?.replace(/\n$/, '') ?? '';
    const fence = '`'.repeat(Math.max(3, longestBacktickRun(code) + 1));
    return `\n${fence}\n${PRESERVED_START}${code}${PRESERVED_END}\n${fence}\n\n`;
  }
  if (/^H[1-6]$/.test(tag)) {
    return `\n${'#'.repeat(Number(tag[1]))} ${childrenToMarkdown(node, depth).trim()}\n\n`;
  }
  if (tag === 'A') {
    const label = childrenToMarkdown(node, depth).trim();
    const href = cleanUrl(node.getAttribute('href'));
    return href && href !== label ? `[${label}](${href})` : label;
  }
  if (tag === 'BLOCKQUOTE') {
    const value = childrenToMarkdown(node, depth).trim();
    return `\n${indentLines(value, '> ')}\n\n`;
  }
  if (tag === 'UL' || tag === 'OL') {
    return Array.from(node.children)
      .filter((child) => child.tagName === 'LI')
      .map((child) => listItemMarkdown(child, depth, tag === 'OL'))
      .join('');
  }
  if (tag === 'LI') return listItemMarkdown(node, depth, false);
  if (tag === 'TABLE') return `\n${tableMarkdown(node)}\n\n`;

  const value = childrenToMarkdown(node, depth);
  return BLOCK_TAGS.has(tag) ? `\n${value.trim()}\n\n` : value;
}

function normalizeMarkdown(value: string): string {
  const preserved: string[] = [];
  const protectedValue = value.replace(
    new RegExp(`${PRESERVED_START}([\\s\\S]*?)${PRESERVED_END}`, 'g'),
    (_match, content: string) => {
      const index = preserved.push(content) - 1;
      return `${PRESERVED_START}${index}${PRESERVED_END}`;
    },
  );
  return protectedValue
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
    .replace(
      new RegExp(`${PRESERVED_START}(\\d+)${PRESERVED_END}`, 'g'),
      (_match, index: string) => preserved[Number(index)] ?? '',
    );
}

export function serializeSelectionRange(range: Range): SelectionCopyPayload {
  let fragment = range.cloneContents();
  const sourceContext = closestElement(range.commonAncestorContainer);
  const preformattedContext = sourceContext?.closest('pre, [class*="whitespace-pre"]');
  if (preformattedContext && !fragment.querySelector('pre, [class*="whitespace-pre"]')) {
    const wrapper = document.createElement('pre');
    if (preformattedContext.tagName !== 'PRE') wrapper.dataset.copyTextPreWrap = '';
    wrapper.append(fragment);
    fragment = document.createDocumentFragment();
    fragment.append(wrapper);
  }
  prepareFragment(fragment);
  const markdown = normalizeMarkdown(childrenToMarkdown(fragment, 0));
  const html = sanitizeFragment(fragment);
  return { html, markdown };
}

export function handleSelectionCopy(
  event: ClipboardEvent,
  selection: Selection | null = document.getSelection(),
): boolean {
  if (!event.clipboardData || !selection || selection.isCollapsed || selection.rangeCount !== 1) return false;
  if (
    event.defaultPrevented
    || isEditorNode(event.target as Node | null)
    || isInteractiveCopyTarget(event.target as Node | null)
  ) return false;
  if (isEditorNode(selection.anchorNode) || isEditorNode(selection.focusNode)) return false;

  try {
    const payload = serializeSelectionRange(selection.getRangeAt(0));
    if (!payload.html && !payload.markdown) return false;
    event.clipboardData.setData('text/html', payload.html);
    event.clipboardData.setData('text/plain', payload.markdown);
    try {
      event.clipboardData.setData('text/markdown', payload.markdown);
    } catch {
      // text/markdown is optional. The standard HTML and plain payloads remain valid.
    }
    event.preventDefault();
    return true;
  } catch {
    return false;
  }
}

export function installSelectionCopy(
  target: Pick<Document, 'addEventListener' | 'removeEventListener'> = document,
): () => void {
  const listener = (event: Event): void => {
    handleSelectionCopy(event as ClipboardEvent);
  };
  target.addEventListener('copy', listener);
  return () => target.removeEventListener('copy', listener);
}

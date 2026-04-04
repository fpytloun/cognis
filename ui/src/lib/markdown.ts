import DOMPurify from 'dompurify';
import { marked, Renderer } from 'marked';

marked.setOptions({
  breaks: true,
  gfm: true
});

const forbiddenAttributes = ['onerror', 'onclick', 'onload', 'onmouseover'];
const forbiddenTags = ['iframe', 'script', 'style'];

export function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    FORBID_ATTR: forbiddenAttributes,
    FORBID_TAGS: forbiddenTags,
    USE_PROFILES: { html: true }
  });
}

export function renderMarkdown(markdown: string): string {
  const parsed = marked.parse(markdown, { async: false });
  return sanitizeHtml(typeof parsed === 'string' ? parsed : '');
}

function createDocsRenderer(): Renderer {
  const renderer = new Renderer();
  const baseCode = renderer.code.bind(renderer);
  const baseTable = renderer.table.bind(renderer);

  renderer.code = (...args) => `<div class="markdown-code-wrap">${baseCode(...args)}</div>`;
  renderer.table = (...args) => `<div class="markdown-table-wrap">${baseTable(...args)}</div>`;

  return renderer;
}

export function renderDocsMarkdown(markdown: string): string {
  const parsed = marked.parse(markdown, {
    async: false,
    renderer: createDocsRenderer(),
  });
  return sanitizeHtml(typeof parsed === 'string' ? parsed : '');
}

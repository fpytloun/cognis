import DOMPurify from 'dompurify';
import { marked } from 'marked';

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

import { mount } from 'svelte';
import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
import './standalone.css';

interface StandalonePayload {
  content: string;
  instanceId: string;
  payload: unknown;
  title: string;
}

/**
 * Rich deliverables default to dark regardless of OS preference until
 * Cognis ships app-wide theming: there is no per-deliverable theme
 * toggle, so this used to resolve `system`/a stored choice via
 * `prefers-color-scheme` -- which left the standalone page stuck light on
 * any light-OS machine (or stuck on a stale stored choice) with no way
 * back to dark. `data-resolved-theme="light"` is still supported by the
 * CSS for when app-wide theming lands; this bootstrap just never sets it.
 */
function bootstrapTheme(): void {
  document.documentElement.dataset.resolvedTheme = 'dark';
  document.documentElement.style.colorScheme = 'dark';
}

function requiredElement<T extends Element>(selector: string, type: { new (): T }): T {
  const element = document.querySelector(selector);
  if (!(element instanceof type)) throw new Error(`Missing standalone mount element: ${selector}`);
  return element;
}

function parsePayload(template: HTMLTemplateElement): StandalonePayload {
  const raw = template.content.textContent ?? '';
  const parsed: unknown = JSON.parse(raw);
  if (!parsed || typeof parsed !== 'object') throw new Error('Invalid standalone payload');
  const candidate = parsed as Partial<StandalonePayload>;
  if (
    typeof candidate.content !== 'string'
    || typeof candidate.instanceId !== 'string'
    || typeof candidate.title !== 'string'
  ) {
    throw new Error('Invalid standalone payload fields');
  }
  return {
    content: candidate.content,
    instanceId: candidate.instanceId,
    payload: candidate.payload,
    title: candidate.title,
  };
}

bootstrapTheme();

const template = requiredElement('#cognis-deliverable-payload', HTMLTemplateElement);
const target = requiredElement('#cognis-deliverable-root', HTMLDivElement);
const data = parsePayload(template);
const mediaBase = template.dataset.mediaBase ?? '';
const mediaUrlFor = (mediaKey: string): string => mediaBase
  ? `${mediaBase}/${encodeURIComponent(mediaKey)}`
  : '';

document.addEventListener('click', (event) => {
  const link = event.target instanceof Element
    ? event.target.closest<HTMLAnchorElement>('a[target="_blank"]')
    : null;
  if (link) link.target = '_self';
}, true);

mount(RichDeliverable, {
  target,
  props: {
    content: data.content,
    instanceId: data.instanceId,
    mediaUrlFor,
    payload: data.payload,
    pdfUrl: template.dataset.pdfUrl ?? '',
    // No standaloneUrl: "Open standalone page" would just reopen this same
    // page. RichDeliverable also gates that button to surface="embedded"
    // as belt-and-suspenders, but there is no reason to even resolve/pass
    // the URL from here.
    surface: 'standalone',
    title: data.title,
  },
});

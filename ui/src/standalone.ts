import { mount } from 'svelte';
import StandaloneViewer from './StandaloneViewer.svelte';
import './standalone.css';
import { standaloneCloseHandler } from '$lib/standalone-navigation';
import { normalizeThemeFamily } from '$lib/theme/catalog';

interface StandalonePayload {
  content: string;
  instanceId: string;
  embeddedDocumentUrl?: string;
  payload: unknown;
  title: string;
  media?: Record<string, string>;
  themeFamily?: unknown;
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
    embeddedDocumentUrl: typeof candidate.embeddedDocumentUrl === 'string' ? candidate.embeddedDocumentUrl : '',
    payload: candidate.payload,
    title: candidate.title,
    media: candidate.media,
    themeFamily: candidate.themeFamily,
  };
}

const template = requiredElement('#cognis-deliverable-payload', HTMLTemplateElement);
const target = requiredElement('#cognis-deliverable-root', HTMLDivElement);
const data = parsePayload(template);
const mediaBase = template.dataset.mediaBase ?? '';
const offline = template.dataset.offline === 'true';
const mediaUrlFor = (mediaKey: string): string => offline
  ? data.media?.[mediaKey] ?? ''
  : mediaBase
  ? `${mediaBase}/${encodeURIComponent(mediaKey)}`
  : '';

document.addEventListener('click', (event) => {
  const link = event.target instanceof Element
    ? event.target.closest<HTMLAnchorElement>('a[target="_blank"]')
    : null;
  if (link) link.target = '_self';
}, true);

mount(StandaloneViewer, {
  target,
  props: { feedbackShareToken: offline ? '' : template.dataset.feedbackShareToken ?? '', viewerProps: {
    content: data.content,
    instanceId: data.instanceId,
    embeddedDocumentUrl: data.embeddedDocumentUrl,
    mediaUrlFor,
    payload: data.payload,
    pdfUrl: template.dataset.pdfUrl ?? '',
    htmlUrl: template.dataset.htmlUrl ?? '',
    markdownUrl: template.dataset.markdownUrl ?? '',
    // No standaloneUrl: "Open standalone page" would just reopen this same
    // page. RichDeliverable also gates that button to surface="embedded"
    // as belt-and-suspenders, but there is no reason to even resolve/pass
    // the URL from here.
    surface: 'standalone',
    exportMode: offline,
    publicationThemeFamily: data.themeFamily == null ? null : normalizeThemeFamily(data.themeFamily),
    closePreview: offline ? null : standaloneCloseHandler(window, document.referrer),
    expiredLink: template.dataset.shareExpired === 'true',
    title: data.title,
  } },
});

import { canAttemptPwaAuxiliaryWindow } from '$lib/stores/pwa';
import { isTouch } from '$lib/stores/viewport';
import { openNativeResourceWindow } from '$lib/native/host';

/** Only authenticated deliverable readers can opt into the isolated app shell. */
export function deliverableWindowUrl(internalUrl: string): URL | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(internalUrl, window.location.origin);
  if (url.origin !== window.location.origin || !/^\/library\/deliverable\/[^/]+\/?$/.test(url.pathname)) return null;
  url.searchParams.set('window', '1');
  return url;
}

export function canOpenDeliverableWindow(): boolean {
  return canAttemptPwaAuxiliaryWindow() && !isTouch();
}

export function openDeliverableWindow(internalUrl: string): void {
  const url = deliverableWindowUrl(internalUrl);
  if (!url || !canOpenDeliverableWindow()) return;
  const id = url.pathname.split('/').filter(Boolean).at(-1)!;
  if (openNativeResourceWindow('deliverable', id)) return;
  const width = Math.min(1280, Math.max(720, Math.round(window.screen.availWidth * 0.8)));
  const height = Math.min(1100, Math.max(760, Math.round(window.screen.availHeight * 0.86)));
  const left = Math.max(0, Math.round(window.screenX + (window.outerWidth - width) / 2));
  const top = Math.max(0, Math.round(window.screenY + (window.outerHeight - height) / 2));
  // noopener can return null on success; do not treat that as a blocked popup.
  window.open(url.href, `cognis-deliverable-${url.pathname.split('/').filter(Boolean).at(-1)}`,
    `popup=yes,noopener,noreferrer,width=${width},height=${height},left=${left},top=${top},menubar=no,toolbar=no,location=no,status=no,scrollbars=yes,resizable=yes`);
}

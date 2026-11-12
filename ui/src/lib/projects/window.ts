import { openNativeResourceWindow } from '$lib/native/host';

export function openProjectWindow(projectId: string, currentUrl: URL): void {
  if (openNativeResourceWindow('project', projectId)) return;
  const url = new URL(currentUrl);
  url.searchParams.set('window', '1');
  const width = Math.min(1280, Math.max(720, Math.round(window.screen.availWidth * 0.8)));
  const height = Math.min(1100, Math.max(760, Math.round(window.screen.availHeight * 0.86)));
  const left = Math.max(0, Math.round(window.screenX + (window.outerWidth - width) / 2));
  const top = Math.max(0, Math.round(window.screenY + (window.outerHeight - height) / 2));
  // noopener may return null on success; do not open a second fallback window.
  window.open(url.href, `cognis-project-${projectId}`,
    `popup=yes,noopener,noreferrer,width=${width},height=${height},left=${left},top=${top},menubar=no,toolbar=no,location=no,status=no,scrollbars=yes,resizable=yes`);
}

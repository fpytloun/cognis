/** Save original attachment bytes without navigating the PWA. */
export function isAppleMobile(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

export async function prepareAttachmentFile(
  url: string,
  filename: string,
  signal: AbortSignal,
): Promise<File> {
  const response = await fetch(url, { signal, credentials: 'omit', referrerPolicy: 'no-referrer' });
  if (!response.ok) throw new Error('The file could not be loaded. Please try again.');
  const blob = await response.blob();
  return new File([blob], filename, { type: blob.type || 'application/octet-stream' });
}

export function canShareFile(file: File): boolean {
  return typeof navigator.share === 'function'
    && typeof navigator.canShare === 'function'
    && navigator.canShare({ files: [file] });
}

export function downloadFile(file: File): void {
  downloadBlob(file, file.name);
}

/** Explicit downloads stay in the current app window, including WKWebView. */
export function downloadUrl(url: string, filename = ''): void {
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.referrerPolicy = 'no-referrer';
  document.body.append(anchor);
  try { anchor.click(); } finally { anchor.remove(); }
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try { downloadUrl(url, filename); } finally {
    // Safari/WKWebView may consume the blob after this handler returns.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

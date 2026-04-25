const publicEnv = import.meta.env as Record<string, string | undefined>;

export type LinkedServiceTarget = 'intaris' | 'mnemory';

function browserOrigin(): string | null {
  return typeof window === 'undefined' ? null : window.location.origin;
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/$/, '');
}

function makeAbsoluteUrl(base: string, path: string): string {
  return new URL(path, `${trimTrailingSlash(base)}/`).toString();
}

function deriveLocalServiceUrl(port: number): string | null {
  const origin = browserOrigin();
  if (!origin) {
    return null;
  }

  const url = new URL(origin);
  if (url.hostname !== 'localhost' && url.hostname !== '127.0.0.1') {
    return null;
  }

  url.port = String(port);
  return trimTrailingSlash(url.toString());
}

export function getApiBaseUrl(): string {
  return trimTrailingSlash(publicEnv.PUBLIC_COGNIS_API_URL || '');
}

export function apiUrl(path: string): string {
  const baseUrl = getApiBaseUrl();
  if (!baseUrl) {
    return path;
  }

  return makeAbsoluteUrl(baseUrl, path);
}

export function getWebSocketUrl(): string {
  const baseUrl = getApiBaseUrl();
  if (baseUrl) {
    const url = new URL('/api/ws', `${baseUrl}/`);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    return url.toString();
  }

  const origin = browserOrigin() ?? 'http://localhost:5173';
  const url = new URL('/api/ws', origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

export function getIntarisUiUrl(): string {
  return trimTrailingSlash(publicEnv.PUBLIC_INTARIS_UI_URL || deriveLocalServiceUrl(8060) || '/intaris');
}

export function getMnemoryUiUrl(): string {
  return trimTrailingSlash(publicEnv.PUBLIC_MNEMORY_UI_URL || deriveLocalServiceUrl(8050) || '/mnemory');
}

export function getLinkedServiceUiUrl(target: LinkedServiceTarget): string {
  return target === 'intaris' ? getIntarisUiUrl() : getMnemoryUiUrl();
}

export function buildLinkedServiceUrl(target: LinkedServiceTarget, params: Record<string, string>): string {
  const baseUrl = getLinkedServiceUiUrl(target);
  const url = new URL(baseUrl, browserOrigin() ?? 'http://localhost:5173');
  for (const [key, value] of Object.entries(params)) {
    if (value) {
      url.searchParams.set(key, value);
    }
  }
  return url.toString();
}

export function openUrlInNewTab(url: string): void {
  window.open(url, '_blank', 'noopener,noreferrer');
}

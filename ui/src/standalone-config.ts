/** Hosted standalone documents always use their own origin, never SPA/native settings. */
export function apiUrl(path: string): string {
  return new URL(path, window.location.origin).href;
}

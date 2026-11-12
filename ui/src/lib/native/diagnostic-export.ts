import { nativeConfiguration, postNative } from './host';

/** Diagnostics retain the bounded JSON bridge supported by existing app bundles. */
export function saveNativeChatDiagnostic(filename: string, json: string): boolean {
  const config = nativeConfiguration();
  if (!config || config.surface === 'rail' || !filename.endsWith('.json')
    || !/^cognis-chat-diagnostic(?:-[A-Za-z0-9_-]{1,128})?\.json$/.test(filename)
    || new TextEncoder().encode(json).length > 8 * 1024 * 1024) return false;
  try {
    if (JSON.parse(json)?.schema !== 'cognis-chat-client-diagnostic-v1') return false;
  } catch { return false; }
  return postNative('save-diagnostic', { filename, json });
}

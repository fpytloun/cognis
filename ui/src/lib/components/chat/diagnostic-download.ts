import type { ChatV2ClientState } from '$lib/chat-v2/sync-engine';
import type { TimelineItem } from '$lib/chat-v2/types';
import { captureClientDiagnostic, clientDiagnosticFilename } from '$lib/chat-v2/client-diagnostic';
import { isNativeHost } from '$lib/native/host';
import { saveNativeChatDiagnostic } from '$lib/native/diagnostic-export';
import { downloadBlob } from '$lib/attachments/save';

/** Called synchronously inside the user's click after this optional module loads. */
export function downloadChatDiagnostic(
  clientState: ChatV2ClientState,
  visibleItems: TimelineItem[],
  build: string,
): void {
  const capture = captureClientDiagnostic(clientState, visibleItems, {
    build,
    standalone: window.matchMedia('(display-mode: standalone)').matches,
    online: navigator.onLine,
  });
  const json = JSON.stringify(capture, null, 2);
  const filename = clientDiagnosticFilename(capture.scope.conversation_id);
  if (isNativeHost()) {
    if (!saveNativeChatDiagnostic(filename, json)) throw new Error('Native diagnostic save is unavailable');
    return;
  }
  downloadBlob(new Blob([json], { type: 'application/json' }), filename);
}

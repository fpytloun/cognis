import type { ChatIntentOptions } from './chat-selection-intent';
import { chatSelectionIntent } from './chat-selection-intent';
import { documentCoordination } from '$lib/document-coordination';
interface Options extends ChatIntentOptions { text: string; error?: (message: string) => void }
/** Keep renderer provenance and annotation implementation out of chat startup. */
export function chatTextFeedback(root: HTMLElement, initial: Options) {
  let options = initial, generation = 0, alive = true;
  let adapter: Awaited<ReturnType<typeof import('./chat-text-adapter')['mountChatTextAdapter']>> | undefined;
  let loading = false;
  const lease = documentCoordination().capture();
  const intent = chatSelectionIntent(root, initial);
  async function ready() {
    if (!options.enabled || !options.binding || loading || adapter) return;
    loading = true;
    const version = generation;
    try {
      const module = await import('./chat-text-adapter');
      if (alive && version === generation && options.binding && options.enabled) {
        lease.assertCurrent();
        adapter = module.mountChatTextAdapter(root, options.text, options.binding, message => options.error?.(message));
        // Adapter listeners must exist before delayed intent is handed over.
        intent.update(options);
      }
    } catch {
      if (alive && version === generation) options.error?.('Feedback could not load. Try selecting the text again.');
    } finally {
      loading = false;
      if (alive && version !== generation) void ready();
    }
  }
  void ready();
  return {
    resume() { intent.resume(); },
    update(next: Options) {
      if (next.key !== options.key || next.text !== options.text || !next.enabled) {
        generation++; adapter?.destroy(); adapter = undefined;
        options.error?.('');
      }
      options = next;
      adapter?.update(next.binding);
      // Defer replay until the lazy adapter is mounted.
      intent.update({ ...next, binding: adapter ? next.binding : null });
      void ready();
    },
    destroy() { alive = false; generation++; intent.destroy(); adapter?.destroy(); },
  };
}

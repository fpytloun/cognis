import type { ChatIntentOptions } from './chat-selection-intent';
import { ownsChatSelection, registerChatSelectionRoot } from './chat-selection-roots';
import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
interface Options extends ChatIntentOptions { text?: string; error?: (message: string) => void }
/** Only native intent or an explicitly opened binding loads annotation code. */
export function lazyChatFeedback(root: HTMLElement, initial: Options) {
  let options = initial, alive = true, loading = false;
  let lease: DocumentLease | undefined;
  let discardedSelection: Range | undefined;
  let action: { update: (value: Options & { text: string }) => void; resume: (event?: Event) => void; destroy: () => void } | undefined;
  const unregister = registerChatSelectionRoot(root);
  async function load(event?: Event) {
    if (!alive || !options.enabled || loading || action) return;
    loading = true;
    const key = options.key;
    try {
      const currentLease = lease ??= documentCoordination().capture();
      currentLease.assertCurrent();
      const selection = root.ownerDocument.getSelection();
      const range = selection?.rangeCount === 1 ? selection.getRangeAt(0).cloneRange() : null;
      const target = event?.target instanceof Element ? event.target : null;
      const tableIntent = options.text === undefined && target?.closest('[data-feedback-rich-block]')?.querySelector('table')
        && ['pointerover', 'focusin', 'pointerdown'].includes(event?.type ?? '');
      const create = options.text === undefined
        ? (await import('./chat-selection-intent')).chatSelectionIntent
        : (await import('./chat-text-feedback')).chatTextFeedback;
      if (!alive || options.key !== key || !options.enabled) return;
      currentLease.assertCurrent();
      action = create(root, { ...options, text: options.text ?? '' });
      const current = root.ownerDocument.getSelection();
      const unchanged = range && current?.rangeCount === 1
        && current.getRangeAt(0).startContainer === range.startContainer && current.getRangeAt(0).startOffset === range.startOffset
        && current.getRangeAt(0).endContainer === range.endContainer && current.getRangeAt(0).endOffset === range.endOffset;
      if (unchanged && event || tableIntent && target?.isConnected && ownsChatSelection(root, target)) action.resume(tableIntent ? event : undefined);
    } catch {
      if (alive && options.key === key) options.error?.('Feedback could not load. Select the text again to retry.');
    } finally {
      loading = false;
      // A current explicit open must not be lost behind an older source's import.
      // Do not replay the discarded source's selection or table gesture.
      if (alive && options.key !== key && options.enabled && options.binding) void load();
    }
  }
  function observe(event: Event) {
    if (action || !options.enabled || !options.surface?.available) return;
    const selection = root.ownerDocument.getSelection();
    if (event.type === 'selectionchange' && discardedSelection && selection?.rangeCount === 1) {
      const range = selection.getRangeAt(0);
      if (range.startContainer === discardedSelection.startContainer && range.startOffset === discardedSelection.startOffset
        && range.endContainer === discardedSelection.endContainer && range.endOffset === discardedSelection.endOffset) return;
    }
    discardedSelection = undefined;
    const native = selection && !selection.isCollapsed && selection.rangeCount === 1
      && ownsChatSelection(root, selection.anchorNode) && ownsChatSelection(root, selection.focusNode);
    const target = event.target instanceof Element ? event.target : null;
    const table = target?.closest('[data-feedback-rich-block]')?.querySelector('table');
    const tableIntent = options.text === undefined && table && ownsChatSelection(root, target)
      && (event.type !== 'pointerdown' || (event as PointerEvent).pointerType === 'touch');
    if (native || tableIntent) void load(event);
  }
  const events = ['pointerup', 'keyup', 'pointerover', 'focusin', 'pointerdown'];
  root.ownerDocument.addEventListener('selectionchange', observe);
  events.forEach(event => root.addEventListener(event, observe));
  if (options.binding) void load();
  return {
    update(next: Options) {
      if (next.key !== options.key || !next.enabled) {
        const selection = root.ownerDocument.getSelection();
        discardedSelection = selection?.rangeCount === 1 ? selection.getRangeAt(0).cloneRange() : undefined;
        action?.destroy(); action = undefined; lease = undefined;
      }
      options = next; action?.update({ ...next, text: next.text ?? '' });
      if (next.binding) void load();
    },
    destroy() {
      alive = false; action?.destroy(); unregister();
      root.ownerDocument.removeEventListener('selectionchange', observe);
      events.forEach(event => root.removeEventListener(event, observe));
    },
  };
}

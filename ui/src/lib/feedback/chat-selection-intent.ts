import type { FeedbackSurfaceProvider } from './context';
import type { FeedbackSurfaceBinding } from './types';
import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
import { surfaceIdentity } from './annotations/surface-identity';
import { ownsChatSelection, registerChatSelectionRoot } from './chat-selection-roots';

function same(a: Range, b: Range) {
  return a.startContainer === b.startContainer && a.endContainer === b.endContainer
    && a.startOffset === b.startOffset && a.endOffset === b.endOffset;
}
export interface ChatIntentOptions {
  surface: FeedbackSurfaceProvider | undefined;
  binding: FeedbackSurfaceBinding | null | undefined;
  key: string;
  enabled: boolean;
}
/** Non-intercepting intent only. The existing native adapter owns selection mapping and drafts. */
export function chatSelectionIntent(root: HTMLElement, initial: ChatIntentOptions) {
  const unregister = registerChatSelectionRoot(root);
  let options = initial, pending: Range | null = null, requested = false, alive = true, generation = 0;
  let lease: DocumentLease | undefined;
  function selected(): Range | null {
    const selection = root.ownerDocument.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount !== 1
      || !ownsChatSelection(root, selection.anchorNode) || !ownsChatSelection(root, selection.focusNode)) return null;
    const range = selection.getRangeAt(0);
    if (!range.toString().trim()) return null;
    return range;
  }
  function activate() {
    if ((requested && !options.binding) || !options.enabled || !options.surface?.available || options.binding) return;
    try { lease ??= documentCoordination().capture(); lease.assertCurrent(); }
    catch { pending = null; return; }
    requested = true;
    if (options.surface.prepare) options.surface.prepare();
    else options.surface.commenting?.(true);
  }
  function observe() {
    if (!options.enabled) return;
    const range = selected();
    if (!range) { pending = null; generation++; return; }
    if (options.binding) return;
    pending = range.cloneRange(); activate();
  }
  function table(event: Event) {
    const element = event.target instanceof Element ? event.target : null;
    if (event.type === 'pointerdown' && (event as PointerEvent).pointerType !== 'touch') return;
    const block = element?.closest('[data-feedback-rich-block]');
    if (block && root.contains(block) && ownsChatSelection(root, element) && block.querySelector('table')) activate();
  }
  function replay() {
    const range = pending, version = generation;
    if (!range || !options.binding) return;
    pending = null;
    queueMicrotask(() => {
      const current = selected();
      if (!alive || generation !== version || !root.isConnected || !current || !same(range, current)) return;
      try { lease?.assertCurrent(); } catch { return; }
      if (!options.binding?.source.capabilities.read || !options.binding.source.capabilities.comment) return;
      // Native adapters already listen to pointerup; no gesture or selection is synthesized/replaced.
      range.startContainer.parentElement?.dispatchEvent(new Event('pointerup', { bubbles: true }));
    });
  }
  root.ownerDocument.addEventListener('selectionchange', observe);
  root.addEventListener('pointerup', observe); root.addEventListener('keyup', observe);
  root.addEventListener('pointerover', table); root.addEventListener('focusin', table); root.addEventListener('pointerdown', table);
  return {
    resume(event?: Event) { observe(); if (event) table(event); },
    update(next: ChatIntentOptions) {
      if (next.key !== options.key || !next.enabled || next.surface !== options.surface
        || options.binding && surfaceIdentity(next.binding) !== surfaceIdentity(options.binding)) {
        generation++; pending = null; requested = false; lease = undefined;
      }
      options = next; replay();
    },
    destroy() {
      alive = false; generation++; unregister();
      root.ownerDocument.removeEventListener('selectionchange', observe);
      root.removeEventListener('pointerup', observe); root.removeEventListener('keyup', observe);
      root.removeEventListener('pointerover', table); root.removeEventListener('focusin', table); root.removeEventListener('pointerdown', table);
    },
  };
}

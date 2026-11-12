import type { FeedbackAnchorCandidate, FeedbackSurfaceBinding, FeedbackTargetGeometry, FeedbackTargetView } from '../types';
import { surfaceIdentity } from './surface-identity';

export function rangeGeometry(range: Range): FeedbackTargetGeometry | null {
  if (!range.startContainer.isConnected || !range.getClientRects) return null;
  return { space: 'client', rects: Array.from(range.getClientRects()).filter(rect => rect.width && rect.height)
    .map(({ left, top, width, height }) => ({ left, top, width, height })) };
}
export function elementGeometry(element: Element | undefined | null): FeedbackTargetGeometry | null {
  if (!element?.isConnected) return null;
  const { left, top, width, height } = element.getBoundingClientRect();
  return { space: 'client', rects: width && height ? [{ left, top, width, height }] : [] };
}
export function targetView(binding: FeedbackSurfaceBinding, instance: string, candidate: FeedbackAnchorCandidate,
  label: string, geometry: () => FeedbackTargetGeometry | null): FeedbackTargetView {
  return { candidate, label, geometry, surface_id: surfaceIdentity(binding), instance_id: instance };
}
interface NativeSelectionOptions {
  binding: FeedbackSurfaceBinding | null | undefined;
  instance: string;
  map: (range: Range) => Promise<{ candidate: FeedbackAnchorCandidate; label: string }> | { candidate: FeedbackAnchorCandidate; label: string };
  error: (message: string) => void;
}
const clearers = new WeakMap<HTMLElement, () => void>();
export function clearNativeSelection(root: HTMLElement | undefined): void { if (root) clearers.get(root)?.(); }
/** Self-contained so opaque HTML can use the same native event lifecycle. */
export function listenNativeSelection(document: Document, root: EventTarget, observe: () => void) {
  // While a mouse button is held the selection is still being dragged. Publishing it then mounts
  // comment UI under the pointer, which the drag extends into, so read once the drag ends.
  // Touch selection handles do not hold a pointer, so their selectionchange events still apply.
  let dragging = false;
  const down = (event: PointerEvent) => { if (event.pointerType === 'mouse' && event.button === 0) dragging = true; };
  const up = () => { if (!dragging) return; dragging = false; observe(); };
  const change = () => { if (!dragging) observe(); };
  document.addEventListener('pointerdown', down, true);
  document.addEventListener('pointerup', up, true);
  document.addEventListener('pointercancel', up, true);
  document.addEventListener('selectionchange', change);
  root.addEventListener('pointerup', observe);
  root.addEventListener('keyup', observe);
  return () => {
    document.removeEventListener('pointerdown', down, true);
    document.removeEventListener('pointerup', up, true);
    document.removeEventListener('pointercancel', up, true);
    document.removeEventListener('selectionchange', change);
    root.removeEventListener('pointerup', observe); root.removeEventListener('keyup', observe);
  };
}
/** Observes native mouse, keyboard and touch-handle changes without replacing selection. */
export function nativeSelection(root: HTMLElement, initial: NativeSelectionOptions) {
  let options = initial, generation = 0, alive = true;
  let key = identity(initial), queued = false;
  // Only retract a text target this observer published; a click or cell pick in comment mode
  // collapses the selection and must keep its own target.
  let published = false;
  function retract(binding: NonNullable<NativeSelectionOptions['binding']>) {
    if (!published) return;
    published = false;
    binding.onTargetChange?.(null); binding.onInteractionChange(false);
  }
  function identity(value: NativeSelectionOptions) {
    return JSON.stringify([surfaceIdentity(value.binding), value.binding?.source.content_digest, value.binding?.source.capabilities,
      value.binding?.source.availability]);
  }
  async function read() {
    const current = options, binding = current.binding, version = ++generation;
    // Native text selection is always available; comment mode only adds click and drag targeting.
    if (!binding || binding.mode === 'area' || binding.mode === 'draw'
      || !binding.source.capabilities.read || !binding.source.capabilities.comment || binding.source.availability === 'unavailable') return;
    const selection = root.ownerDocument.getSelection();
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed
      || !root.contains(selection.anchorNode) || !root.contains(selection.focusNode)) {
      retract(binding); return;
    }
    const range = selection.getRangeAt(0).cloneRange();
    if (!range.toString().trim()) { retract(binding); return; }
    binding.onInteractionChange(true);
    try {
      const mapped = await current.map(range);
      if (!alive || generation !== version || identity(options) !== key) return;
      current.error('');
      published = true;
      binding.onTargetChange?.(targetView(binding, current.instance, mapped.candidate, mapped.label, () => rangeGeometry(range)));
    } catch (caught) {
      if (alive && generation === version) {
        if (published) retract(binding); else binding.onInteractionChange(false);
        current.error(caught instanceof Error ? caught.message : 'This text cannot be mapped precisely.');
      }
    }
  }
  function observe() {
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; if (alive) void read(); });
  }
  clearers.set(root, () => {
    generation++; published = false;
    const selection = root.ownerDocument.getSelection();
    if (selection && root.contains(selection.anchorNode)) selection.removeAllRanges();
    options.binding?.onTargetChange?.(null); options.binding?.onInteractionChange(false);
  });
  const stop = listenNativeSelection(root.ownerDocument, root, observe);
  return {
    update(next: NativeSelectionOptions) {
      const nextKey = identity(next);
      if (nextKey !== key) { generation++; options.binding?.onTargetChange?.(null); }
      options = next; key = nextKey;
    },
    destroy() {
      alive = false; generation++; clearers.delete(root);
      stop();
    },
  };
}

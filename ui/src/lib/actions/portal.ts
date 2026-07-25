/**
 * Svelte action: move the decorated node to be a direct child of
 * `document.body` while mounted, restoring it to its original DOM
 * position (or removing it) on destroy.
 *
 * Why this is needed: a `position: fixed` overlay only escapes an
 * ancestor's layout box when no ancestor establishes a new containing
 * block (`transform`, `filter`, `perspective`, `will-change`, `contain`).
 * But layout is not the only thing that can trap it — an ancestor that
 * establishes a new **stacking context** (`isolation: isolate`, `opacity
 * < 1`, `position` + `z-index`, etc.) still confines the overlay's paint
 * order to that ancestor's local stacking context, no matter how large
 * its own `z-index` is. A fullscreen modal rendered deep inside the app
 * (e.g. a rich deliverable's "full view" opened from within the chat
 * message list, whose root establishes `isolation: isolate`) can then be
 * painted underneath sibling app chrome such as the sidebar or the
 * message composer, even though it visually spans the whole viewport.
 *
 * Moving the overlay node to be a direct child of `document.body`
 * sidesteps the entire class of bug: it is no longer a descendant of any
 * component-local stacking context, so its own `z-index` is compared
 * directly against the true root stacking order.
 */
export function portal(node: HTMLElement): { destroy(): void } {
  document.body.appendChild(node);
  return {
    destroy(): void {
      // Every current caller applies `use:portal` to the root of an
      // `{#if}`-gated block (the full-view modal, the narrow TOC drawer),
      // so `destroy()` only ever runs when that block is going away for
      // good -- either the condition flipped false or the whole component
      // unmounted. There is no case where moving the node back to its
      // original position is useful: that position is itself part of the
      // block being torn down. Restoring there previously left a stale
      // duplicate node behind whenever the original parent was still
      // attached to the document at destroy time (e.g. a component
      // unmounted via a testing-library `unmount()` call that does not
      // synchronously remove its render container), which showed up as
      // duplicate accessible elements across a mount/unmount/remount
      // cycle. Just detach the node from wherever it currently lives.
      node.remove();
    },
  };
}

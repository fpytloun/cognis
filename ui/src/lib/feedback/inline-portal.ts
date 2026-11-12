interface InlinePortalOptions {
  inline: boolean;
  active: boolean;
  host: () => HTMLElement | null;
}

/** Keep native diff editors in source flow without moving ownership or draft state. */
export function inlinePortal(node: HTMLElement, initial: InlinePortalOptions) {
  let options = initial;
  const anchor = document.createComment('feedback editor');
  node.before(anchor);
  let frame = 0;
  function reconcile() {
    const host = options.inline ? options.host() : null;
    const visible = options.active && (!options.inline || !!host);
    node.hidden = !visible;
    node.inert = !visible;
    const parent = host ?? anchor.parentNode;
    if (parent && node.parentNode !== parent) parent.insertBefore(node, host ? null : anchor);
  }
  const observer = new MutationObserver(() => {
    if (!frame) frame = requestAnimationFrame(() => { frame = 0; reconcile(); });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  reconcile();
  return {
    update(next: InlinePortalOptions) { options = next; reconcile(); },
    destroy() {
      observer.disconnect();
      cancelAnimationFrame(frame);
      anchor.remove();
      node.remove();
    },
  };
}

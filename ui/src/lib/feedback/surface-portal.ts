import { portal } from '$lib/actions/portal';

/** Suspend portaled controls with their retained hidden/inert source owner. */
export function surfacePortal(node: HTMLElement, onActive: (active: boolean) => void) {
  if (typeof document === 'undefined') return;
  const anchor = document.createComment('feedback overlay owner');
  node.before(anchor);
  const owners: HTMLElement[] = [];
  for (let owner = anchor.parentElement; owner; owner = owner.parentElement) owners.push(owner);
  const mounted = portal(node);
  let active: boolean | undefined;
  const update = () => {
    const next = anchor.isConnected && owners.every(owner => {
      const style = getComputedStyle(owner);
      return !owner.hidden && !owner.hasAttribute('inert') && owner.getAttribute('aria-hidden') !== 'true'
        && style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse';
    });
    if (next !== active) { active = next; onActive(next); }
  };
  const observer = new MutationObserver(update);
  for (const owner of owners) observer.observe(owner, {
    attributes: true, childList: true, attributeFilter: ['hidden', 'inert', 'aria-hidden', 'class', 'style'],
  });
  window.addEventListener('resize', update);
  update();
  return { destroy() {
    observer.disconnect(); window.removeEventListener('resize', update);
    mounted?.destroy(); anchor.remove();
  } };
}

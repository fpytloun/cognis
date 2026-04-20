/**
 * Svelte action: right-side tooltip for collapsed sidebar icons.
 *
 * The sidebar's \`overflow-hidden\` aside plus the inner \`overflow-y-auto\`
 * scroll container together clip any absolutely-positioned children, which
 * means inline \`absolute left-full\` tooltips could not escape the sidebar
 * and instead forced a horizontal scrollbar on the scroll container (once
 * the browser promoted \`overflow-x: visible\` to \`auto\` because
 * \`overflow-y\` was set).
 *
 * Render the tooltip as a \`position: fixed\` element appended to
 * \`document.body\`, positioned at \`rect.right + 12px\` of the trigger.
 * Fixed-position elements are laid out relative to the viewport and are
 * unaffected by \`overflow: hidden\` ancestors, so the tooltip visibly
 * crosses the sidebar edge without producing any scrollbar.
 */
export function sidebarTooltip(
  node: HTMLElement,
  label: string,
): { update(value: string): void; destroy(): void } {
  let current = label;
  let tip: HTMLSpanElement | null = null;

  function ensureTip(): HTMLSpanElement {
    if (tip) return tip;
    const el = document.createElement('span');
    el.setAttribute('role', 'tooltip');
    el.setAttribute('aria-hidden', 'true');
    el.className =
      'pointer-events-none fixed z-[100] whitespace-nowrap rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200 shadow-lg transition-opacity duration-100';
    el.style.opacity = '0';
    document.body.appendChild(el);
    tip = el;
    return el;
  }

  function show(): void {
    const el = ensureTip();
    el.textContent = current;
    // Measure first so vertical centring against the trigger is accurate.
    const rect = node.getBoundingClientRect();
    el.style.left = `${rect.right + 12}px`;
    el.style.top = `${rect.top + rect.height / 2}px`;
    // Shift up by half the tooltip's own height to centre vertically.
    const tipRect = el.getBoundingClientRect();
    el.style.top = `${rect.top + rect.height / 2 - tipRect.height / 2}px`;
    requestAnimationFrame(() => {
      if (tip) tip.style.opacity = '1';
    });
  }

  function hide(): void {
    if (!tip) return;
    tip.style.opacity = '0';
  }

  function destroyTip(): void {
    if (tip && tip.parentNode) tip.parentNode.removeChild(tip);
    tip = null;
  }

  node.addEventListener('mouseenter', show);
  node.addEventListener('mouseleave', hide);
  node.addEventListener('focusin', show);
  node.addEventListener('focusout', hide);

  return {
    update(value: string): void {
      current = value;
      if (tip) tip.textContent = value;
    },
    destroy(): void {
      node.removeEventListener('mouseenter', show);
      node.removeEventListener('mouseleave', hide);
      node.removeEventListener('focusin', show);
      node.removeEventListener('focusout', hide);
      destroyTip();
    },
  };
}

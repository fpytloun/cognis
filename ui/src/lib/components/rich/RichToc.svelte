<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import { portal } from '$lib/actions/portal';
  import type { TocItem } from './publication';
  import { nestTocItems } from './publication';
  import RichTocList from './RichTocList.svelte';

  export let items: TocItem[] = [];
  export let onNavigate: (item: TocItem) => void;
  export let open = false;
  export let onClose: (() => void) | undefined = undefined;

  $: nodes = nestTocItems(items);
  let activeAnchor = '';
  let panel: HTMLElement;
  let closeButton: HTMLButtonElement;
  let restoreFocus: HTMLElement | null = null;
  let observer: IntersectionObserver | null = null;
  // "Narrow" covers everything up to a very large/wide screen, not just
  // phones -- tablets (including iPad Pro landscape) and typical laptop/
  // desktop windows all get the hamburger-triggered drawer instead of a
  // persistent sidebar, which would otherwise eat too much of the reading
  // column. Kept in sync with the `min-width: 1440px` breakpoint in
  // RichDeliverable.svelte that switches `.rich-document.has-toc` between a
  // block layout (narrow, this component renders the drawer) and a grid
  // layout (wide, this component renders the sticky sidebar).
  let narrowQuery: MediaQueryList | null = null;
  let narrowChangeHandler: ((event: MediaQueryListEvent) => void) | null = null;
  let isNarrow = false;
  let wasOpen = false;
  let closeInProgress = false;

  function focusCloseButton() {
    closeButton?.focus({ preventScroll: true });
  }

  async function focusDrawer() {
    await tick();
    if (open && isNarrow) focusCloseButton();
    window.setTimeout(() => {
      if (open && isNarrow) focusCloseButton();
    }, 200);
  }

  async function closeDrawer(options: { restoreTrigger?: boolean } = {}) {
    if (closeInProgress || !open) return;
    closeInProgress = true;
    open = false;
    await tick();
    const target = restoreFocus;
    restoreFocus = null;
    if (options.restoreTrigger !== false && target?.isConnected) {
      target.focus({ preventScroll: true });
    }
    onClose?.();
    closeInProgress = false;
  }

  function navigate(item: TocItem) {
    activeAnchor = item.anchor;
    if (isNarrow) void closeDrawer({ restoreTrigger: false });
    onNavigate(item);
  }

  function handleKeydown(event: KeyboardEvent) {
    if (!open || !isNarrow) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      void closeDrawer();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(panel?.querySelectorAll<HTMLElement>('button:not([disabled])') ?? [])
      .filter((node) => node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function keepFocusInDrawer(event: FocusEvent) {
    if (!open || !isNarrow || !panel || panel.contains(event.target as Node)) return;
    focusCloseButton();
  }

  function handleOpenChange(nextOpen: boolean) {
    if (nextOpen === wasOpen) return;
    wasOpen = nextOpen;
    if (nextOpen) {
      restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      void focusDrawer();
    }
  }
  $: handleOpenChange(open);

  onMount(() => {
    document.addEventListener('focusin', keepFocusInDrawer);
    if (typeof window.matchMedia === 'function') {
      narrowQuery = window.matchMedia('(max-width: 1439.98px)');
      isNarrow = narrowQuery.matches;
      if (open && isNarrow) {
        restoreFocus ??= document.activeElement instanceof HTMLElement ? document.activeElement : null;
        void focusDrawer();
      }
      narrowChangeHandler = (event: MediaQueryListEvent) => {
        isNarrow = event.matches;
        if (!isNarrow && open) void closeDrawer({ restoreTrigger: false });
      };
      narrowQuery.addEventListener('change', narrowChangeHandler);
    }
    if ('IntersectionObserver' in window) {
      observer = new IntersectionObserver(
        (entries) => {
          const visible = entries.filter((entry) => entry.isIntersecting)
            .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top);
          if (visible[0]?.target.id) activeAnchor = visible[0].target.id;
        },
        { rootMargin: '-12% 0px -72% 0px', threshold: [0, 1] }
      );
      for (const item of items) {
        const heading = document.getElementById(item.anchor);
        if (heading) observer.observe(heading);
      }
    }
  });

  onDestroy(() => {
    observer?.disconnect();
    document.removeEventListener('focusin', keepFocusInDrawer);
    if (narrowQuery && narrowChangeHandler) {
      narrowQuery.removeEventListener('change', narrowChangeHandler);
    }
  });
</script>

{#if !isNarrow}
  <!-- Very large/wide screens (>=1440px): a floating sticky sidebar column,
       scroll-spy highlighted via activeAnchor (see RichTocList). -->
  <aside class="rich-toc" aria-label="Table of contents" data-testid="rich-deliverable-toc">
    <nav aria-label="Table of contents">
      <RichTocList {nodes} {activeAnchor} onNavigate={navigate} />
    </nav>
  </aside>
{:else}
  <!-- Narrow (<1440px), the default -- phones, tablets (incl. iPad Pro
       landscape), and typical laptop/desktop windows: a hamburger-triggered
       drawer. The closed root stays
       in document flow so the timeline retains one stable TOC identity.
       The open overlay is portaled to document.body -- like the
       full-view modal (.rich-full) -- because .rich-deliverable
       establishes `isolation: isolate`, which traps a `position: fixed`
       overlay's paint order inside that local stacking context no matter
       how large its own z-index is, regardless of the drawer's own
       z-index. Without the portal, the drawer visibly mis-layers behind
       the app's own chrome (verified via rendered screenshots). -->
  {#if open}
    <div class="rich-toc-drawer-root" data-testid="rich-deliverable-toc" use:portal>
      <button
        class="rich-toc-backdrop"
        type="button"
        aria-label="Close table of contents"
        data-testid="rich-toc-backdrop"
        on:click={() => closeDrawer()}
      ></button>
      <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
      <nav
        bind:this={panel}
        class="rich-toc-drawer open"
        aria-label="Table of contents"
        aria-modal="true"
        role="dialog"
        tabindex="-1"
        on:keydown={handleKeydown}
      >
        <header>
          <strong>Contents</strong>
          <button bind:this={closeButton} type="button" aria-label="Close table of contents" on:click={() => closeDrawer()}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </header>
        <RichTocList {nodes} {activeAnchor} onNavigate={navigate} />
      </nav>
    </div>
  {:else}
    <div class="rich-toc-drawer-root" data-testid="rich-deliverable-toc">
      <nav class="rich-toc-drawer" aria-label="Table of contents">
        <header><strong>Contents</strong></header>
        <RichTocList {nodes} {activeAnchor} onNavigate={navigate} />
      </nav>
    </div>
  {/if}
{/if}

<style>
  .rich-toc { position: sticky; top: .85rem; align-self: start; z-index: 2; min-width: 0; }
  nav { max-height: calc(100vh - 1.7rem); overflow: auto; border-left: 1px solid var(--rich-line); padding: .25rem 0 .25rem .65rem; }
  svg { width: 1.25rem; height: 1.25rem; fill: none; stroke: currentColor; stroke-linecap: round; stroke-width: 1.8; }

  /* The drawer root is a plain pass-through container (no box of its own)
     once portaled to document.body; its fixed-position children below
     establish their own layer. z-index values sit just above .rich-full
     (2147483000, see RichDeliverable.svelte) because the drawer can be
     opened from the full-view modal's own TOC trigger and must still
     layer above that modal's panel, not underneath it. */
  .rich-toc-drawer-root { display: contents; }

  .rich-toc-backdrop {
    position: fixed;
    inset: 0;
    z-index: 2147483010;
    display: block;
    border: 0;
    background: rgb(2 6 23 / .58);
    padding: 0;
  }

  .rich-toc-drawer {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: 2147483020;
    display: flex;
    flex-direction: column;
    width: min(22rem, calc(100% - 2.5rem));
    max-height: none;
    overflow: auto;
    visibility: hidden;
    transform: translateX(102%);
    border: 0;
    border-left: 1px solid var(--rich-line);
    background: var(--rich-surface-solid);
    backdrop-filter: blur(20px);
    padding: .7rem 1rem max(1rem, env(safe-area-inset-bottom));
    box-shadow: -24px 0 60px rgb(0 0 0 / .45);
    transition: transform 180ms ease, visibility 180ms;
  }

  .rich-toc-drawer.open {
    visibility: visible;
    transform: translateX(0);
  }

  .rich-toc-drawer header {
    position: sticky;
    top: -.7rem;
    z-index: 1;
    display: flex;
    flex: none;
    min-height: 3.5rem;
    align-items: center;
    justify-content: space-between;
    background: var(--rich-surface-solid);
    padding: .35rem 0 .55rem;
  }

  .rich-toc-drawer header strong {
    color: var(--rich-text);
    font-size: .9rem;
  }

  .rich-toc-drawer header button {
    display: inline-grid;
    width: 2.75rem;
    height: 2.75rem;
    place-items: center;
    border: 0;
    border-radius: 999px;
    background: color-mix(in srgb, var(--rich-surface-raised) 90%, var(--rich-line));
    color: var(--rich-text-secondary);
  }

  @media (prefers-reduced-motion: reduce) {
    .rich-toc-drawer {
      transition: none;
    }
  }
</style>

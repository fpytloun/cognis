<script lang="ts">
  import type { Snippet } from 'svelte';
  import Ellipsis from 'lucide-svelte/icons/ellipsis';
  import { anchoredMenu } from '$lib/actions/anchored-menu';
  import { isCompactViewport } from '$lib/stores/viewport';
  import './rich/document-actions.css';

  /**
   * Secondary viewer header actions (download, share). Inline on wider screens; folded into one
   * ⋯ menu on phones so the document title keeps its room. `children(true)` renders them as
   * labelled menu rows.
   */
  let { children }: { children: Snippet<[boolean]> } = $props();
  let more = $state<HTMLDetailsElement>();
  // Touch browsers can blur the summary before the item's click lands; dismiss on outside press.
  function dismiss(event: PointerEvent) {
    if (more?.open && event.target instanceof Node && !more.contains(event.target)) more.open = false;
  }
  function escape(event: KeyboardEvent) {
    if (event.key !== 'Escape' || !more?.open) return;
    event.stopPropagation();
    more.open = false;
    more.querySelector('summary')?.focus();
  }
</script>

<svelte:window onpointerdown={dismiss} onkeydown={escape} />
{#if $isCompactViewport}
  <details class="compact-header-more" bind:this={more}>
    <summary class="document-icon-action" aria-label="More actions" title="More actions"><Ellipsis aria-hidden="true" /></summary>
    <div class="compact-header-menu" use:anchoredMenu={{ preferredSide: 'below' }}>
      {@render children(true)}
    </div>
  </details>
{:else}
  {@render children(false)}
{/if}

<style>
  details { position: relative; flex: none; }
  summary { list-style: none; }
  summary::-webkit-details-marker { display: none; }
  .compact-header-menu {
    position: absolute; right: 0; top: calc(100% + .4rem); z-index: 40; display: flex; flex-direction: column;
    width: max-content; min-width: 12rem; max-width: calc(100vw - 2rem); padding: .35rem;
    border: 1px solid rgb(var(--color-control-border)); border-radius: .75rem;
    background: rgb(var(--color-elevated)); color: rgb(var(--color-foreground)); box-shadow: 0 8px 24px #0003;
  }
  .compact-header-menu :global(:is(button, a)) {
    display: flex; align-items: center; justify-content: flex-start; gap: .6rem; width: 100%; height: auto; min-height: 44px;
    padding: .4rem .6rem; border: 0; border-radius: .4rem; background: transparent; color: inherit; font-size: 14px;
    text-align: left; text-decoration: none;
  }
  .compact-header-menu :global(:is(button, a):hover) { background: rgb(var(--color-surface-hover)); }
  .compact-header-menu :global(svg) { width: 18px; height: 18px; flex: none; }
  .compact-header-menu > :global(span) { display: flex; width: 100%; }
</style>

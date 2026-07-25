<script lang="ts">
  import type { TocItem, TocNode } from './publication';

  export let nodes: TocNode[] = [];
  export let activeAnchor = '';
  export let onNavigate: (item: TocItem) => void;
</script>

<ol>
  {#each nodes as node}
    <li data-level={node.item.level}>
      <button
        type="button"
        class:active={activeAnchor === node.item.anchor}
        aria-current={activeAnchor === node.item.anchor ? 'location' : undefined}
        on:click={() => onNavigate(node.item)}
      >
        {node.item.label}
      </button>
      {#if node.children.length > 0}
        <svelte:self nodes={node.children} {activeAnchor} {onNavigate} />
      {/if}
    </li>
  {/each}
</ol>

<style>
  ol { display: grid; gap: .1rem; margin: 0; padding: 0; list-style: none; }
  :global(li > ol) { margin-top: .1rem; padding-left: .85rem; border-left: 1px solid var(--rich-line); }
  button { width: 100%; min-height: 2.15rem; border: 0; border-radius: .45rem; background: transparent; color: var(--rich-muted); padding: .34rem .45rem; font-size: .76rem; font-weight: 650; line-height: 1.3; text-align: left; }
  li[data-level="3"] > button { font-size: .73rem; }
  li[data-level="4"] > button { font-size: .7rem; font-weight: 560; }
  button:hover, button:focus-visible, button.active { background: color-mix(in srgb, var(--rich-accent) 12%, transparent); color: var(--rich-text); outline: none; }
  button.active { box-shadow: inset 2px 0 var(--rich-accent); }
  /* Kept in sync with RichToc's drawer breakpoint (min-width: 1440px in
     RichDeliverable.svelte): this list renders inside the drawer at every
     width below that, including tablets, so its touch targets should stay
     comfortably tappable there too, not just on phones. */
  @media (max-width: 1439.98px) {
    button { min-height: 2.75rem; font-size: .88rem; }
    li[data-level="3"] > button { font-size: .84rem; }
    li[data-level="4"] > button { font-size: .8rem; }
  }
</style>

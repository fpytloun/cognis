<script lang="ts">
  import type { RichBlock, RichMediaUrlFor } from '$lib/rich-deliverable';
  import RichBlockView from './RichBlockView.svelte';

  export let blocks: RichBlock[] = [];
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';
</script>

<div class="rich-block-list">
  {#each blocks as block}
    {#if typeof block.__legacy_anchor === 'string'}
      <span class="rich-legacy-anchor" id={block.__legacy_anchor} aria-hidden="true"></span>
    {/if}
    {#if block.type === 'markdown'}
      <div class="rich-block-anchor">
        <RichBlockView {block} {sources} {mediaUrlFor} />
      </div>
    {:else}
      <div class="rich-block-anchor" id={typeof block.__publication_anchor === 'string' ? block.__publication_anchor : undefined} tabindex="-1">
        <RichBlockView {block} {sources} {mediaUrlFor} />
      </div>
    {/if}
  {/each}
</div>

<style>
  .rich-legacy-anchor {
    display: block;
    height: 0;
    scroll-margin-top: 1rem;
  }

  /* Subtle, staggered entrance for each top-level block as the list first
     paints (initial render or a fresh client-side insert, e.g. a new
     deliverable streaming into a chat message). Nested block lists (grid
     cells, disclosure panels) restart their own stagger from 0, which reads
     naturally since each nested reveal is scoped to its own container. */
  @media (prefers-reduced-motion: no-preference) {
    .rich-block-anchor {
      animation: rich-block-reveal 420ms cubic-bezier(0.16, 1, 0.3, 1) both;
    }

    /* Stagger only the first handful of blocks; long documents shouldn't
       feel slow to finish revealing, so anything past the 6th block settles
       at the same capped delay. */
    .rich-block-anchor:nth-child(1) { animation-delay: 0ms; }
    .rich-block-anchor:nth-child(2) { animation-delay: 45ms; }
    .rich-block-anchor:nth-child(3) { animation-delay: 90ms; }
    .rich-block-anchor:nth-child(4) { animation-delay: 130ms; }
    .rich-block-anchor:nth-child(5) { animation-delay: 165ms; }
    .rich-block-anchor:nth-child(n + 6) { animation-delay: 195ms; }
  }

  @keyframes rich-block-reveal {
    from {
      opacity: 0;
      transform: translateY(5px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>

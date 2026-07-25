<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { blockChildren, blockText, blockTitle, type RichBlock, type RichMediaUrlFor } from '$lib/rich-deliverable';
  import RichBlockList from '../RichBlockList.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let type = 'section';
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: children = blockChildren(block);
</script>

<section class="rich-panel" data-rich-block-type={type}>
  {#if blockTitle(block) || blockText(block, 'subtitle')}
    <header class="rich-section-heading">
      {#if blockText(block, 'eyebrow')}<span>{@html renderInlineMarkdown(blockText(block, 'eyebrow'))}</span>{/if}
      {#if blockTitle(block)}<h3>{@html renderInlineMarkdown(blockTitle(block))}</h3>{/if}
      {#if blockText(block, 'subtitle')}<p>{@html renderInlineMarkdown(blockText(block, 'subtitle'))}</p>{/if}
    </header>
  {/if}
  <div class="rich-stack"><RichBlockList blocks={children} {sources} {mediaUrlFor} /></div>
</section>
